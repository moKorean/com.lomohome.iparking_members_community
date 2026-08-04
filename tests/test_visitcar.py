"""Tests for the `visitcar` driver, its device, and the one Flow action.

Three subjects, and what they have in common is that every failure they guard against is
**silent on a hub**:

* **Pairing keys `data.id` on `lot_id`.** `data` is immutable after pairing, so a wrong key
  is not something a later version fixes — it is something every user pays for by deleting
  and re-adding every device. Nothing about a wrong key looks wrong at pairing time.
* **The device keeps the last name it read.** So a lot that stopped answering looks exactly
  like a healthy one unless the two-failure transition actually fires.
* **A misparsed Flow `date`.** `05-08-2026` is 5 August read day-first and 8 May read
  month-first; both are real dates, the wire format is `yyyyMMdd` either way, and the guest
  finds out at a closed gate. The success notification echoing the resolved date is the only
  thing standing between that and a silent wrong-day registration, which is why several tests
  here assert on notification *text*.

No event-loop plugin is installed, so each test drives `asyncio.run` itself.
"""

import asyncio
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import (
    LOT_ID,
    OAUTH_URL,
    PARK_NAME,
    PARK_SEQ,
    STOR_SEQ,
    FakeNotifications,
    login_ok,
    lots_ok,
)

from iparking_lib import const, i18n
from iparking_lib.const import (
    CAPABILITY_PARK_NAME,
    CAPABILITY_TODAY_COUNT,
    FLOW_REGISTER_VISITOR,
    FLOW_REGISTER_VISITOR_TODAY,
    MAX_FAVORITES,
    MAX_POLL_FAILURES,
    POLL_BACKOFF_S,
    STORE_LOT_ID,
    STORE_PARK_NAME,
    STORE_PARK_SEQ,
    STORE_STOR_SEQ,
    favorite_name_setting,
    favorite_plate_setting,
    quick_capability,
)
from iparking_lib.iparking import codes, dates
from iparking_lib.iparking.client import (
    HistoryRow,
    IparkingApiError,
    Lot,
    NotPermittedError,
    RegisterResult,
    RegisterUncertain,
)
from iparking_lib.visitcar import device as device_mod
from iparking_lib.visitcar import driver as driver_mod

ROOT = Path(__file__).resolve().parents[1]

SECOND_LOT_ID = "1160009002"
SECOND_PARK_SEQ = 9002
SECOND_PARK_NAME = "예시동 샘플아파트[출입통제B]"
SECOND_STOR_SEQ = 100002

PLATE = "12가4567"
MASKED = "12가****"


# --- stubs --------------------------------------------------------------------


class _StubApi:
    """The shared session, scripted. Faithful where it matters, absent everywhere else.

    `register` resolves its date through the **real** `dates.resolve_visit_date`, because
    every date assertion in this file is about which day the app ends up telling the user
    about — a stub that invented its own `api_date` would agree with itself and prove
    nothing.
    """

    def __init__(self, *, lots=(), lot_rows=None, outcome=codes.OUTCOME_OK, error=None,
                 history_rows=(), history_error=None):
        self._lots = list(lots)
        self._lot_rows = lot_rows
        self._outcome = outcome
        self._error = error
        # A callable is allowed so a test can make the answer depend on the (mocked) clock —
        # that is how the KST midnight rollover is driven without a second stub.
        self._history_rows = history_rows
        self._history_error = history_error
        self.lot_calls = []
        self.registers = []
        self.history_calls = []

    async def history(self, *, park_seq, stor_seq, start_date=None, end_date=None,
                      car_number="", **_kwargs):
        self.history_calls.append(
            {"park_seq": park_seq, "stor_seq": stor_seq,
             "start_date": start_date, "end_date": end_date, "car_number": car_number}
        )
        if self._history_error is not None:
            raise self._history_error
        rows = self._history_rows
        return list(rows() if callable(rows) else rows)

    async def enumerate_lots(self):
        return list(self._lots)

    async def parking_lots(self, stor_seq):
        self.lot_calls.append(stor_seq)
        script = self._lot_rows
        if isinstance(script, list) and script and isinstance(script[0], (list, tuple)):
            rows = script.pop(0) if len(script) > 1 else script[0]
        else:
            rows = script
        if isinstance(rows, Exception):
            raise rows
        return list(rows or ())

    async def register(self, *, car_number, park_seq, stor_seq, visit_date=None,
                       memo="", mobile=""):
        self.registers.append(
            {"car_number": car_number, "park_seq": park_seq, "stor_seq": stor_seq,
             "visit_date": visit_date}
        )
        if self._error is not None:
            raise self._error
        api_date = dates.resolve_visit_date(visit_date or dates.today_kst())
        return RegisterResult(self._outcome, api_date)


class _Session:
    """The pair/repair session `pairing.install` wires handlers onto."""

    def __init__(self):
        self.handlers = {}

    def set_handler(self, name, fn):
        self.handlers[name] = fn

    async def call(self, name, *args, **kwargs):
        return await self.handlers[name](*args, **kwargs)


def _lot(*, lot_id=LOT_ID, park_seq=PARK_SEQ, park_name=PARK_NAME, stor_seq=STOR_SEQ,
         can_register=True):
    return Lot(lot_id=lot_id, park_seq=park_seq, park_name=park_name, stor_seq=stor_seq,
               can_register=can_register)


def _row(*, lot_id=LOT_ID, park_seq=PARK_SEQ, park_name=PARK_NAME):
    return {"park_seq": park_seq, "lot_id": lot_id, "park_name": park_name}


def _store(**overrides):
    store = {
        STORE_STOR_SEQ: STOR_SEQ,
        STORE_PARK_SEQ: PARK_SEQ,
        STORE_LOT_ID: LOT_ID,
        STORE_PARK_NAME: PARK_NAME,
    }
    store.update(overrides)
    return store


@pytest.fixture
def make_driver(make_homey):
    def _make(**kwargs):
        homey = make_homey(**kwargs)
        instance = driver_mod.VisitCarDriver(homey=homey)
        asyncio.run(instance.on_init())
        return instance, homey

    return _make


@pytest.fixture
def make_device(make_homey):
    """A booted `VisitCarDevice_` whose poll task has already been dismantled.

    Boot runs for real — `on_init` reads the store, sheds the leftover 주차장명 sensor if this
    device was paired before v0.1.4, reconciles the tile buttons, starts the poll task, and the
    task acquires the shared session and counts today's registrations — and is then torn down, so
    every later poll in a test is one this file drives explicitly. `_api` survives the teardown,
    which is what makes `asyncio.run(dev._poll_once())` a shipping code path rather than a
    re-implementation of one.

    `capabilities` defaults to the one capability `driver.compose.json` declares.
    """

    def _make(*, api=None, store=None, capabilities=(CAPABILITY_TODAY_COUNT,), ticks=30,
              notifications=None, settings=None, sdk_spelling="snake", sdk_awaitable=False,
              add_capability_error=None):
        homey = make_homey(api=api, notifications=notifications)
        dev = device_mod.VisitCarDevice_(
            settings=settings,
            sdk_spelling=sdk_spelling,
            sdk_awaitable=sdk_awaitable,
            add_capability_error=add_capability_error,
            homey=homey,
            store=_store() if store is None else store,
            capabilities=list(capabilities),
            name=PARK_NAME,
        )

        async def _boot():
            await dev.on_init()
            for _ in range(ticks):
                await asyncio.sleep(0)
            await dev._teardown()

        asyncio.run(_boot())
        # The teardown is only how the test regains control; a device that reached its poll
        # loop is the state every assertion below is written against.
        dev._closing = False
        # And the handle is dropped: that task belonged to the boot loop, which `asyncio.run` has
        # since closed, so a later `on_uninit` gathering it would fail on the loop rather than on
        # anything this app does. Tests that care about teardown drive it in one loop instead.
        dev._poll_task = None
        return dev, homey

    return _make


# --- pairing: one device per lot, keyed on lot_id -----------------------------


def test_pairing_yields_one_device_per_lot(make_driver):
    """Every authorization entry × every lot, with no "which store?" question to ask — which
    is why `build_devices` takes only the session."""
    instance, _homey = make_driver()
    api = _StubApi(lots=[
        _lot(),
        _lot(lot_id=SECOND_LOT_ID, park_seq=SECOND_PARK_SEQ, park_name=SECOND_PARK_NAME,
             stor_seq=SECOND_STOR_SEQ),
    ])

    devices = asyncio.run(instance._build_devices(api))

    assert [d["name"] for d in devices] == [PARK_NAME, SECOND_PARK_NAME]


def test_data_id_is_the_lot_id_and_nothing_else(make_driver):
    """`data` is immutable after pairing. `lot_id` is the vendor's globally-qualified id;
    bare `park_seq` uniqueness across stores was never established, and keying on it would
    force every user to re-pair to correct it."""
    instance, _homey = make_driver()

    devices = asyncio.run(instance._build_devices(api=_StubApi(lots=[_lot()])))

    assert devices[0]["data"] == {"id": LOT_ID}
    assert devices[0]["data"]["id"] != str(PARK_SEQ)


def test_stor_seq_and_park_seq_live_in_the_mutable_store(make_driver):
    instance, _homey = make_driver()

    devices = asyncio.run(instance._build_devices(api=_StubApi(lots=[_lot()])))

    assert devices[0]["store"] == {
        STORE_STOR_SEQ: STOR_SEQ,
        STORE_PARK_SEQ: PARK_SEQ,
        STORE_LOT_ID: LOT_ID,
        STORE_PARK_NAME: PARK_NAME,
    }


def test_a_store_that_may_not_register_still_pairs(make_driver):
    """`invitation_register_authorization_yn != "Y"` gates the *write*, checked live on every
    attempt because the building office can grant it later. Refusing to pair would remove a
    working 주차장명 sensor to prevent a write that is already prevented."""
    instance, _homey = make_driver()

    devices = asyncio.run(instance._build_devices(api=_StubApi(lots=[_lot(can_register=False)])))

    assert devices[0]["data"] == {"id": LOT_ID}


def test_an_account_with_no_authorization_entries_refuses_with_its_own_message(make_api):
    """Through the real client, because the refusal is its behaviour: an empty
    `invitation_authorization_list` is "no building is enrolled on this account", which only
    the building office can fix, and flattening it into "no devices found" tells the user
    nothing."""
    api, _stub, _logs = make_api({OAUTH_URL: login_ok(stores=())})

    with pytest.raises(IparkingApiError) as caught:
        asyncio.run(api.enumerate_lots())

    assert caught.value.key == "no_stores"
    assert "관리사무소" in str(caught.value)


def test_an_account_with_stores_but_no_lots_refuses_with_a_different_message(make_driver):
    """A distinct sentence from the one above, because the remedy is different: the account
    *is* enrolled, so the user is pointed at the vendor's own site rather than the office."""
    instance, _homey = make_driver()

    with pytest.raises(Exception, match="주차장을 찾지 못했습니다"):
        asyncio.run(instance._build_devices(api=_StubApi(lots=[])))


def test_the_pair_view_gets_check_session_and_list_devices(make_driver):
    """The start view only asks whether an account is saved — login lives in app settings, so
    pairing has no credential form of its own."""
    instance, _homey = make_driver()
    session = _Session()

    asyncio.run(instance.on_pair(session))

    assert sorted(session.handlers) == ["check_session", "list_devices"]


def test_the_repair_view_gets_the_login_handler(make_driver):
    instance, _homey = make_driver()
    session = _Session()

    asyncio.run(instance.on_repair(session))

    assert list(session.handlers) == ["login"]


def test_the_driver_shims_export_a_class():
    """`drivers/visitcar/{driver,device}.py` insert `parents[2]` — the app root — because
    Homey imports them by path and does not put the app directory on `sys.path`. Off-by-one
    there presents as a driver that simply never appears on the hub, with no error anyone
    sees, so the arithmetic is asserted rather than eyeballed."""
    for name in ("driver", "device"):
        path = ROOT / "drivers/visitcar" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_shim_{name}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert isinstance(module.homey_export, type)


# --- the Flow card ------------------------------------------------------------


def test_both_flow_cards_are_bound_at_driver_init(make_driver):
    instance, homey = make_driver()

    assert homey.flow.cards[("action", FLOW_REGISTER_VISITOR)].listener \
        == instance._on_register
    assert homey.flow.cards[("action", FLOW_REGISTER_VISITOR_TODAY)].listener \
        == instance._on_register_today


def test_a_flow_binding_failure_does_not_take_driver_init_down(make_driver):
    """A card that fails to bind must not cost the sensor too — the 주차장명 capability is the
    requirement the maintainer stated first, and it needs no Flow card at all. Each card is
    bound in its own `try`, so the log names which one failed rather than just that one did."""
    instance, _homey = make_driver(flow_spelling="none")

    for card_id in (FLOW_REGISTER_VISITOR, FLOW_REGISTER_VISITOR_TODAY):
        assert any(f"flow card registration failed: {card_id}" in line
                   for line in instance.logs)


def test_the_run_listener_tolerates_the_extra_keywords_homey_passes(make_device,
                                                                   make_driver):
    """Homey passes extras such as `manual` alongside `(args, state)`. A handler without
    `**kwargs` errors the card out at run time — i.e. only ever in a user's own Flow."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications())
    instance, _driver_homey = make_driver()

    ok = asyncio.run(instance._on_register(
        {"device": dev, "car_number": PLATE, "visit_date": None}, None, manual=True
    ))

    assert ok is True
    assert api.registers[0]["car_number"] == PLATE


def test_the_run_listener_refuses_a_card_with_no_device_selected(make_driver):
    instance, _homey = make_driver()

    with pytest.raises(Exception, match="주차장 기기를 선택"):
        asyncio.run(instance._on_register({"car_number": PLATE}, None))


# --- the second card: plate only, always today --------------------------------


def test_the_today_card_registers_for_today_in_kst(make_device, make_driver):
    """The card the maintainer asked for: nothing to get wrong. `register` sees `None` — the
    same value the dated card's empty field produces — so the day is resolved by
    `dates.today_kst()` on the one path, and the notification echoes it like every other run."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)
    instance, _driver_homey = make_driver()

    ok = asyncio.run(instance._on_register_today(
        {"device": dev, "car_number": PLATE}, None, manual=True
    ))

    assert ok is True
    assert api.registers[0]["visit_date"] is None
    assert dates.format_kst_human(dates.today_api()) in notifications.excerpts[0]
    assert PLATE in notifications.excerpts[0]


def test_both_cards_route_through_the_same_register_path(make_device, make_driver):
    """The invariants on this path — zero retries on `POST /invitations`, the echoed resolved
    date, `RegisterUncertain` as its own outcome — hold once because they are written once. So
    the second card is asserted to be the *same* call with a different `visit_date`, not a
    second implementation that currently agrees."""
    dev, _homey = make_device(api=_StubApi(), notifications=FakeNotifications())
    instance, _driver_homey = make_driver()
    calls = []

    async def _spy(*, car_number, visit_date=""):
        calls.append({"car_number": car_number, "visit_date": visit_date})
        return True

    dev.flow_register = _spy
    in_9_days = (dates.now_kst().date() + timedelta(days=9)).strftime("%d-%m-%Y")

    asyncio.run(instance._on_register(
        {"device": dev, "car_number": PLATE, "visit_date": in_9_days}, None
    ))
    asyncio.run(instance._on_register_today({"device": dev, "car_number": PLATE}, None))

    assert calls == [
        {"car_number": PLATE, "visit_date": in_9_days},
        {"car_number": PLATE, "visit_date": ""},
    ]


def test_the_today_card_ignores_a_date_it_is_somehow_handed(make_device, make_driver):
    """Its definition has no `visit_date` arg, so this cannot happen through the editor — but
    the listener reads no date at all, and that is the property worth pinning: "today" is not a
    default here that some other value could override."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications())
    instance, _driver_homey = make_driver()
    other_day = (dates.now_kst().date() + timedelta(days=20)).strftime("%d-%m-%Y")

    asyncio.run(instance._on_register_today(
        {"device": dev, "car_number": PLATE, "visit_date": other_day}, None
    ))

    assert api.registers[0]["visit_date"] is None


def test_the_today_card_refuses_a_run_with_no_device_selected(make_driver):
    instance, _homey = make_driver()

    with pytest.raises(Exception, match="주차장 기기를 선택"):
        asyncio.run(instance._on_register_today({"car_number": PLATE}, None))


def test_a_plate_alone_registers_for_today(make_device):
    """Mode one of the two the maintainer asked for. The empty `visit_date` reaches
    `register` as `None` so the default goes through the same parse path and the same window
    check a supplied date does."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)

    asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert api.registers[0]["visit_date"] is None
    assert dates.format_kst_human(dates.today_api()) in notifications.excerpts[0]


def test_a_date_and_a_plate_register_for_that_date(make_device):
    """Mode two, and this is what `visit_date`'s `required: false` buys: the same card, one
    field further filled in — no second card, no mode switch."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)
    target = dates.now_kst().date() + timedelta(days=15)
    raw = target.strftime("%d-%m-%Y")

    asyncio.run(dev.flow_register(car_number=PLATE, visit_date=raw))

    assert api.registers[0]["visit_date"] == raw
    assert dates.format_kst_human(target) in notifications.excerpts[0]
    assert dates.format_kst_human(dates.today_api()) not in notifications.excerpts[0]


def test_the_success_notification_always_echoes_the_resolved_date(make_device):
    """The mitigation for the whole `dd-mm-yyyy` / `mm-dd-yyyy` problem. `05-08-2026` is two
    real dates and the wire form is `yyyyMMdd` either way, so the echo is the only thing that
    makes a misparse visible before a guest is standing at a gate."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)
    in_10_days = dates.now_kst().date() + timedelta(days=10)

    asyncio.run(dev.flow_register(
        car_number=PLATE,
        visit_date=in_10_days.strftime("%d-%m-%Y"),
    ))

    excerpt = notifications.excerpts[0]
    assert PLATE in excerpt
    assert dates.format_kst_human(in_10_days) in excerpt


def test_an_ambiguous_date_says_it_was_read_day_first(make_device):
    """Both leading fields ≤ 12, so the resolution is policy rather than evidence. Roughly
    60 % of a year's dates land here, which is why the hint exists instead of a silent
    choice."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)
    today = dates.now_kst().date()
    # Ambiguous means both leading fields of a 2-2-4 input could be a month, i.e. the day of
    # the month is ≤ 12. Inside the 80-day window there is always such a date within 12 days.
    ambiguous = next(
        today + timedelta(days=n) for n in range(1, 40) if (today + timedelta(days=n)).day <= 12
    )

    asyncio.run(dev.flow_register(car_number=PLATE,
                                  visit_date=ambiguous.strftime("%d-%m-%Y")))

    hint = i18n.translate("date_ambiguous", "ko", date=dates.format_kst_human(ambiguous))
    assert hint in notifications.excerpts[0]


def test_the_raw_visit_date_is_logged_so_the_on_device_format_can_be_settled(make_device):
    """§3.6's open question is answerable from one ordinary Flow run **only** if this line
    exists. A calendar date is not personal data; the plate on the same line is, and it is
    masked."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications())
    in_5_days = dates.now_kst().date() + timedelta(days=5)
    raw = in_5_days.strftime("%d-%m-%Y")

    asyncio.run(dev.flow_register(car_number=PLATE, visit_date=raw))

    assert any(f"visit_date raw={raw!r}" in line for line in dev.logs)


def test_no_log_line_carries_an_unmasked_plate(make_device):
    """Criterion 14. Diagnostic output gets pasted into issues, and the plate on this path is
    a *guest's*."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications())

    asyncio.run(dev.flow_register(car_number="12가 4567", visit_date=""))

    assert any(MASKED in line for line in dev.logs)
    assert not any(PLATE in line for line in dev.logs)


def test_the_notification_shows_the_full_plate(make_device):
    """The counterpart of the test above, and the asymmetry is deliberate: masking protects
    log lines that get shared, while a notification is this Flow's answer to the person who
    wrote it — `12가****` there would make it useless."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)

    asyncio.run(dev.flow_register(car_number="12가 4567", visit_date=""))

    assert PLATE in notifications.excerpts[0]


def test_already_registered_notifies_and_does_not_raise(make_device):
    """A Flow must not read a benign duplicate as a failed action — re-entering a registered
    plate is the most likely real outcome of a first use."""
    api = _StubApi(outcome=codes.OUTCOME_ALREADY_REGISTERED)
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)

    ok = asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert ok is True
    assert i18n.translate("flow_already_registered", "ko", plate=PLATE,
                          date=dates.format_kst_human(dates.today_api())) \
        == notifications.excerpts[0]


def test_register_uncertain_is_notified_distinctly_and_never_retried(make_device):
    """The one outcome that must not invite a retry: a retry is what turns one uncertain
    write into two real registrations at a building. Raised (an unknown outcome is not a
    success) but only after the notification that carries the do-not-retry text."""
    uncertain = RegisterUncertain(
        f"{MASKED} 20260805 등록 결과를 확인할 수 없습니다. 다시 등록하지 마시고"
    )
    api = _StubApi(error=uncertain)
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)

    with pytest.raises(RegisterUncertain):
        asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert len(api.registers) == 1
    assert "다시 등록하지 마시고" in notifications.excerpts[0]


def test_a_refused_write_is_rendered_in_the_viewers_language(make_device):
    """Every error this app raises carries an i18n key rather than prose, so the Flow card
    says exactly what the settings page says."""
    api = _StubApi(error=NotPermittedError("권한 없음"))
    dev, _homey = make_device(api=api, notifications=FakeNotifications())

    with pytest.raises(Exception) as caught:
        asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert str(caught.value) == i18n.translate("not_permitted", "ko")


def test_an_explicit_server_failure_is_reported_as_one(make_device):
    api = _StubApi(outcome=codes.OUTCOME_FAILED)
    dev, _homey = make_device(api=api, notifications=FakeNotifications())

    with pytest.raises(Exception) as caught:
        asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert str(caught.value) == i18n.translate(codes.OUTCOME_FAILED, "ko")


@pytest.mark.parametrize("spelling", ["snake", "camel"])
@pytest.mark.parametrize("shape", ["kwarg", "dict", "positional"])
@pytest.mark.parametrize("awaitable", [False, True])
def test_the_notification_helper_tolerates_every_plausible_sdk_contract(
    make_device, spelling, shape, awaitable
):
    """The Python SDK's notification surface is not pinned anywhere readable. The fake raises
    `TypeError` on the shapes it does not implement, so this is a real search over contracts
    rather than a fake that would have accepted whatever was tried first."""
    notifications = FakeNotifications(spelling=spelling, shape=shape, awaitable=awaitable)
    dev, _homey = make_device(api=_StubApi(), notifications=notifications)

    asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert notifications.excerpts == [
        i18n.translate("flow_registered", "ko", plate=PLATE,
                       date=dates.format_kst_human(dates.today_api()))
    ]


def test_a_permissive_runtime_gets_a_plain_string_not_a_dict(make_device):
    """The regression guard for the blank-timeline bug, and the reason it went unnoticed.

    The real hub takes one positional argument of any type and stores it in `excerpt` verbatim.
    Under that contract the dict shape does not raise, so the old ordering posted
    `excerpt={'excerpt': …}` — every row in the user's timeline was blank while the log said
    the notification had been posted. "It did not raise" was the only success signal, and it
    was not one.

    Ordering is what fixes it, so ordering is what this pins: with a runtime that would accept
    either, the plain string has to win.
    """
    notifications = FakeNotifications(spelling="snake", shape="permissive")
    dev, _homey = make_device(api=_StubApi(), notifications=notifications)

    asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert len(notifications.excerpts) == 1
    posted = notifications.excerpts[0]
    assert isinstance(posted, str), (
        f"the timeline renders blank unless this is a string: {posted!r}"
    )
    assert posted == i18n.translate(
        "flow_registered", "ko", plate=PLATE, date=dates.format_kst_human(dates.today_api())
    )


def test_a_missing_notifications_manager_does_not_fail_the_registration(make_device):
    """A registration that succeeded must never be reported as failed because a notification
    could not be posted — the write has already happened at the building."""
    api = _StubApi()
    dev, _homey = make_device(api=api)

    ok = asyncio.run(dev.flow_register(car_number=PLATE, visit_date=""))

    assert ok is True
    assert any("no notifications manager" in line for line in dev.logs)


def test_a_failing_notification_does_not_fail_the_registration(make_device):
    dev, _homey = make_device(
        api=_StubApi(), notifications=FakeNotifications(error=RuntimeError("timeline full"))
    )

    assert asyncio.run(dev.flow_register(car_number=PLATE, visit_date="")) is True
    assert any("notification failed" in line for line in dev.logs)


# --- 오늘 등록: the sensor that replaced 주차장명 ------------------------------
#
# The swap, and the poll it justifies. `iparking_park_name` was the lot's name: constant, and the
# same string Homey already shows as the device's name, so the tile printed it twice and 24
# requests/day went to re-confirm it. `iparking_today_count` changes whenever anybody registers a
# car — including on the vendor's website, where this app cannot see it happen. Polling a constant
# was waste; polling a count is what makes it true.
#
# Every failure guarded here is silent on a hub:
#
# * **Counting `CANCEL` rows** would read 6 where the honest answer is 1, and a plausible-looking
#   number is not something a user can catch.
# * **A cached date window** would survive KST midnight and hold yesterday's count all day.
# * **A leftover `park_name` capability** would sit on every already-paired tile until its owner
#   re-paired, which is the cost this shed exists to avoid.


def _hist(seq: int, *, plate: str = PLATE, date: str = "", status: str = "RESERVE") -> HistoryRow:
    """One 등록 내역 row. `date` defaults to today in KST, which is what the count is about."""
    return HistoryRow(
        invt_seq=seq,
        car_number=plate,
        invitation_date=date or dates.today_api(),
        status=status,
        park_name=PARK_NAME,
    )


def test_the_count_is_todays_registered_vehicles(make_device):
    """The value the maintainer asked for, from the poll that keeps it fresh."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1), _hist(2, plate=PLATE_2)]))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 2


def test_cancelled_rows_are_not_counted(make_device):
    """**The counting rule, and the one that was measured wrong.** 취소 does not delete a row, it
    flips `inot_status` and the row stays — so a day's rows are frequently mostly cancellations.
    On the maintainer's own account this exact shape counted 6 where the honest answer was 1."""
    rows = [_hist(1, status="CANCEL"), _hist(2, status="CANCEL"), _hist(3, status="CANCEL"),
            _hist(4, status="CANCEL"), _hist(5, status="CANCEL"), _hist(6, status="RESERVE")]
    dev, _homey = make_device(api=_StubApi(history_rows=rows))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1


@pytest.mark.parametrize("status", ["RESERVE", "IN", "OUT"])
def test_every_active_status_counts_as_registered(make_device, status):
    """A car that has already entered or left was still registered today. The predicate is
    `const.ACTIVE_STATUSES`, reused rather than re-spelled, so it cannot drift from the one the
    register path's recovery re-query trusts."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1, status=status)]))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1
    assert set(const.ACTIVE_STATUSES) == {"RESERVE", "IN", "OUT"}


def test_a_row_for_another_day_is_not_counted_even_if_the_server_sends_it(make_device):
    """The window is asserted client-side as well as requested. The vendor's filtering rules were
    never characterised, and a bare number on a tile cannot reveal that it quietly covered three
    months — which is the same reason the recovery re-query re-checks the plate it filtered on."""
    dev, _homey = make_device(
        api=_StubApi(history_rows=[_hist(1), _hist(2, date="20260101"), _hist(3, date="20991231")])
    )

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1


def test_a_poll_asks_for_a_single_day_window(make_device):
    """One day, so the response stays small — and `startDate == endDate == today`, from
    `dates.today_api()` rather than from anything cached."""
    api = _StubApi(history_rows=[_hist(1)])
    dev, _homey = make_device(api=api)

    call = api.history_calls[-1]
    assert call["start_date"] == call["end_date"] == dates.today_api()
    assert call["park_seq"] == PARK_SEQ
    assert call["stor_seq"] == STOR_SEQ


def test_a_poll_is_one_request_per_tick(make_device):
    """24 requests/day/device. Politeness enforced by arithmetic rather than asserted."""
    api = _StubApi(history_rows=[_hist(1)])
    dev, _homey = make_device(api=api)
    before = len(api.history_calls)

    asyncio.run(dev._poll_once())

    assert len(api.history_calls) == before + 1


def test_the_device_reaches_its_poll_loop(make_device):
    """The boot sequence is guarded as one block, so a failure in it cannot escape before the
    loop is entered — a device that never reaches the loop polls never again until the app
    restarts, silently."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]))

    assert any("next poll in" in line for line in dev.logs)


def test_the_count_rolls_over_at_kst_midnight(make_device, monkeypatch):
    """**The window is recomputed on every tick, never cached at `on_init`.**

    Yesterday holds three registrations and today holds one. A device that captured its window at
    boot would keep reporting 3 until the app happened to restart — wrong for a whole day, and
    indistinguishable on the tile from a correct answer. Nothing here changes except the clock."""
    yesterday, today = "20260804", "20260805"
    clock = {"now": yesterday}
    monkeypatch.setattr(dates, "today_api", lambda: clock["now"])
    rows = [_hist(1, date=yesterday), _hist(2, date=yesterday), _hist(3, date=yesterday),
            _hist(4, date=today)]
    api = _StubApi(history_rows=rows)
    dev, _homey = make_device(api=api)
    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 3

    clock["now"] = today                      # midnight in KST
    asyncio.run(dev._poll_once())

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1
    assert api.history_calls[-1]["start_date"] == today


def test_one_failure_keeps_the_device_available_and_two_do_not(make_device):
    """Two rather than one: a single dropped request against a cloud API addressed over plain
    HTTP is ordinary; two in a row is a pattern. And it has to be *said*, because the capability
    keeps the last count it read — so a lot that stopped answering otherwise looks exactly like a
    lot with no visitors today, which is the most ordinary reading of all."""
    assert MAX_POLL_FAILURES == 2
    api = _StubApi(history_rows=[_hist(1)])
    dev, _homey = make_device(api=api)
    api._history_error = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(dev._poll_once())
    assert dev.availability[-1] == ("available", None)

    with pytest.raises(RuntimeError):
        asyncio.run(dev._poll_once())
    assert dev.availability[-1] == ("unavailable", "오늘 등록 현황을 가져올 수 없습니다")
    # The last known count is kept rather than blanked: it was true when it was read, and the
    # unavailability is what says not to trust it now.
    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1


def test_recovery_makes_the_device_available_again(make_device):
    api = _StubApi(history_rows=[_hist(1)])
    dev, _homey = make_device(api=api)
    api._history_error = RuntimeError("boom")
    for _ in range(MAX_POLL_FAILURES):
        with pytest.raises(RuntimeError):
            asyncio.run(dev._poll_once())
    api._history_error = None

    asyncio.run(dev._poll_once())

    assert dev._failures == 0
    assert dev.availability[-1] == ("available", None)


def test_set_is_guarded_by_get_capabilities(make_device):
    """The real SDK raises on a capability the device does not have, so the app filters
    against `get_capabilities()` itself — which is what lets a capability list be edited in
    `driver.compose.json` without every paired device throwing, and what makes the 주차장명
    removal safe on a device that has already shed it."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]), capabilities=())

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) is None
    assert not any("set iparking_today_count failed" in line for line in dev.logs)


# --- updating the count without spending a request ----------------------------


def test_a_settings_page_history_read_updates_the_tile_for_free(make_device):
    """**Zero extra requests.** The rows are already in the handler's hand, so feeding them to
    the device is what makes the tile correct the instant a user registers or cancels on the
    settings page — that page re-reads the table after both actions, which is why neither
    handler needs a refresh of its own."""
    api = _StubApi(history_rows=[_hist(1)])
    dev, _homey = make_device(api=api)
    before = len(api.history_calls)

    asyncio.run(dev.note_history(PARK_SEQ, STOR_SEQ, [_hist(1), _hist(2, plate=PLATE_2)]))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 2
    assert len(api.history_calls) == before


def test_another_lots_history_read_does_not_touch_this_devices_count(make_device):
    """One device per parking lot, and a multi-lot account is the ordinary case. A device that
    counted every lot's rows would show its neighbour's visitors."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]))

    asyncio.run(dev.note_history(SECOND_PARK_SEQ, SECOND_STOR_SEQ,
                                 [_hist(1), _hist(2), _hist(3)]))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1


def test_a_three_month_history_read_still_only_counts_today(make_device):
    """The settings table's default window is three months. Feeding it here must not turn a wide
    fetch into a count of it — the filtering is `count_registered_on`'s, not the caller's."""
    dev, _homey = make_device(api=_StubApi(history_rows=[]))

    asyncio.run(dev.note_history(PARK_SEQ, STOR_SEQ, [
        _hist(1), _hist(2, date="20260601"), _hist(3, date="20260713"),
        _hist(4, date="20260714", status="CANCEL"),
    ]))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1


def test_a_register_for_today_refreshes_the_count_immediately(make_device):
    """The tile should be right the moment the user acts, not up to an hour later. A re-read
    rather than an increment: incrementing would put a number on the tile that no server ever
    confirmed, and reporting what the vendor says is registered is this capability's whole job."""
    rows = [_hist(1)]
    api = _StubApi(history_rows=rows)
    dev, _homey = make_device(api=api, notifications=FakeNotifications())
    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1
    rows.append(_hist(2, plate=PLATE_2))          # the write the register is about to make

    asyncio.run(dev.flow_register(car_number=PLATE_2, visit_date=""))

    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 2


def test_a_register_for_a_future_date_spends_no_request_on_the_count(make_device):
    """Next Tuesday's registration changes no count, so it must not pay a request to discover
    that. The check is on the date the write actually used, not on the argument."""
    api = _StubApi(history_rows=[_hist(1)])
    dev, _homey = make_device(api=api, notifications=FakeNotifications())
    before = len(api.history_calls)
    future = (dates.now_kst() + timedelta(days=9)).strftime("%Y-%m-%d")

    asyncio.run(dev.flow_register(car_number=PLATE_2, visit_date=future))

    assert len(api.history_calls) == before
    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1


def test_a_failed_count_refresh_does_not_fail_a_registration_that_succeeded(make_device):
    """The single most important ordering in this method. The write has already happened and the
    user has already been notified; a tile refresh that cannot read is a stale number, not a
    failed registration, and reporting it as one is the thing this app must never do."""
    api = _StubApi(history_rows=[_hist(1)])
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications)
    api._history_error = RuntimeError("count read failed")

    assert asyncio.run(dev.flow_register(car_number=PLATE_2, visit_date="")) is True
    assert len(notifications.excerpts) == 1


# --- the poll task's own lifecycle -------------------------------------------


def test_a_dead_poll_task_is_restarted_and_the_handle_is_reassigned(make_device):
    """Both halves were bugs in the sibling app first. Without the reassignment a later
    `on_uninit` cancels the dead original, the restarted loop outlives the device, and it
    keeps writing capability values to a torn-down Device."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]))

    async def _kill_and_observe():
        async def _boom():
            raise RuntimeError("poll loop crashed")

        dead = asyncio.create_task(_boom())
        await asyncio.gather(dead, return_exceptions=True)
        dev._on_poll_task_done(dead)
        restarted = dev._poll_task
        dev._closing = True
        restarted.cancel()
        await asyncio.gather(restarted, return_exceptions=True)
        return dead, restarted

    dead, restarted = asyncio.run(_kill_and_observe())

    assert restarted is not dead
    assert any(f"restarting in {POLL_BACKOFF_S[0]}s" in line for line in dev.logs)


def test_a_dismantled_task_is_not_restarted(make_device):
    """`on_uninit` cancels the task and the cancellation lands on the bare sleep outside the
    try, so a dismantled device also reaches the done-callback. `task.cancelled()` is the only
    thing separating the two cases."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]))

    async def _cancel_and_observe():
        async def _forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(_forever())
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        dev._poll_task = task
        dev._on_poll_task_done(task)
        return task, dev._poll_task

    cancelled, current = asyncio.run(_cancel_and_observe())

    assert current is cancelled
    assert not any("restarting in" in line for line in dev.logs)


def test_the_restart_backoff_walks_its_steps(make_device, monkeypatch):
    """A crash loop costs 5 s once and 300 s thereafter, so a genuine bug leaves a readable
    log rather than a flood."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]))
    monkeypatch.setattr(device_mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(dev, "_run", _noop)

    async def _walk():
        seen = []
        for _ in range(len(POLL_BACKOFF_S) + 2):
            await dev._restart_poll()
            seen.append(dev._restart_delay)
        return seen

    seen = asyncio.run(_walk())

    assert seen[: len(POLL_BACKOFF_S) - 1] == list(POLL_BACKOFF_S[1:])
    assert seen[-1] == POLL_BACKOFF_S[-1]


def test_teardown_awaits_both_tasks_so_nothing_outlives_the_device(make_homey):
    """The poll loop **and** the deferred settings write. Either one landing after Homey considers
    the device gone is a write against a torn-down object."""
    dev = device_mod.VisitCarDevice_(homey=make_homey(api=_StubApi(history_rows=[])),
                                    store=_store(), capabilities=[CAPABILITY_TODAY_COUNT])

    async def _boot_and_uninit():
        await dev.on_init()
        for _ in range(30):
            await asyncio.sleep(0)
        poll = dev._poll_task
        await dev.on_uninit()
        return poll

    poll = asyncio.run(_boot_and_uninit())

    assert poll.done()
    assert dev._closing is True
    assert dev._normalize_task is None


async def _no_sleep(seconds, *args, **kwargs):
    return None


async def _noop(*args, **kwargs):
    return None


# --- the 주차장명 sensor that is gone -----------------------------------------


def test_an_already_paired_device_sheds_the_park_name_sensor_at_init(make_device):
    """No re-pair. An existing device is re-created from its store on every app start, carrying
    the capability list Homey has on file — so `on_init` is the only place that can take a
    removed capability away, and `remove_capability` is confirmed working on hardware."""
    dev, _homey = make_device(api=_StubApi(history_rows=[_hist(1)]),
                              capabilities=(CAPABILITY_PARK_NAME, CAPABILITY_TODAY_COUNT),
                              settings=_favorite_settings((1, FAV_NAME, PLATE)))

    assert CAPABILITY_PARK_NAME not in dev.get_capabilities()
    assert any(f"remove_capability({CAPABILITY_PARK_NAME}) ok" in line for line in dev.logs)
    # And it takes nothing with it: the new sensor and the buttons are both live.
    assert dev.get_capability_value(CAPABILITY_TODAY_COUNT) == 1
    assert _quick(dev) == [quick_capability(1)]


def test_a_device_paired_after_the_removal_has_nothing_to_shed(make_device):
    """The shed is a no-op rather than a guessed cleanup: a fresh device never had the
    capability, and a log line claiming to have removed one would be a false trail."""
    dev, _homey = make_device(api=_StubApi(history_rows=[]))

    assert not any("remove_capability" in line for line in dev.logs)


def test_no_park_name_machinery_survives_anywhere(make_device):
    """A disabled shell of the old sensor would be worse than the sensor: the next maintainer
    would restore it, believing it had merely been switched off. The *id* stays — `_shed_park_name`
    needs it — but nothing reads a lot name, and `parking_lots` is pairing's call now, not the
    device's."""
    source = (ROOT / "iparking_lib/visitcar/device.py").read_text(encoding="utf-8")

    for gone in ("_name_from", "parking_lots(", "STORE_PARK_NAME", "self._park_name"):
        assert gone not in source, f"{gone} still lives in device.py"


def test_the_only_capability_value_written_is_the_count(make_device):
    """The push-button fix, guarded at the source. The tile buttons are `getable: false`, so there
    is no value to write and nothing to un-latch — and the v0.1.3 `finally` that wrote `False`
    back was the latch it claimed to be curing. Exactly one `_set` call may exist, and it is the
    count's."""
    source = (ROOT / "iparking_lib/visitcar/device.py").read_text(encoding="utf-8")

    assert source.count("await self._set(") == 1
    assert "await self._set(CAPABILITY_TODAY_COUNT" in source


# --- criteria that are about absence -----------------------------------------


def test_v010_makes_no_runtime_store_writes():
    """Criterion 18 — a goal, not a ban. `stor_seq`, `park_seq` and `lot_id` are fixed at
    pairing, so there is nothing for a store write to do; the pattern exists in
    `com.lomohome.localthings` if a later version needs one."""
    for name in ("driver.py", "device.py"):
        source = (ROOT / "iparking_lib/visitcar" / name).read_text(encoding="utf-8")
        # The call, not the word: both modules discuss `set_store_value` in prose, and a
        # test that forbade naming it would be a test against documentation.
        assert ".set_store_value(" not in source
        assert "set_store_value(" not in source.replace("`set_store_value`", "")


def test_the_visitcar_layer_is_the_only_one_that_imports_homey():
    """Acceptance criterion 1 restated from this side: the pure client stays SDK-free, which
    is what makes everything except these two modules testable off-device."""
    assert "from homey import" in (ROOT / "iparking_lib/visitcar/device.py").read_text(
        encoding="utf-8"
    )
    for path in (ROOT / "iparking_lib/iparking").glob("*.py"):
        assert "import homey" not in path.read_text(encoding="utf-8")


def test_the_park_name_capability_schema_is_gone_for_good():
    """Removing the sensor from the device is only half of it. While the schema is still declared
    app-wide, any code path that calls `add_capability` with it succeeds — so deleting the file
    is what makes the removal irreversible by accident. The *id* stays in `const` on purpose:
    `_shed_park_name` needs it to take the capability off devices that already have it."""
    assert not (ROOT / ".homeycompose/capabilities/iparking_park_name.json").exists()
    assert const.CAPABILITY_PARK_NAME == "iparking_park_name"
    declared = {path.stem for path in (ROOT / ".homeycompose/capabilities").glob("*.json")}
    assert declared == {CAPABILITY_TODAY_COUNT} | {
        quick_capability(index) for index in range(1, MAX_FAVORITES + 1)
    }


def test_the_count_is_a_read_only_integer_sensor_with_insights():
    """The fields that make it legal and legible: nothing on a tile may write it (`setable: false`
    + `uiComponent: sensor`), and `decimals: 0` because a count of cars is an integer — Homey
    would otherwise render `1.0`.

    **`insights: true` is the opposite decision from the one 주차장명 got, for the same reason.**
    A near-constant string graphed over time is an empty graph. A count of registrations per day
    is a real measurement, and the graph answers a real question."""
    spec = json.loads(
        (ROOT / ".homeycompose/capabilities/iparking_today_count.json").read_text(
            encoding="utf-8"
        )
    )

    assert spec["type"] == "number"
    assert spec["uiComponent"] == "sensor"
    assert spec["getable"] is True
    assert spec["setable"] is False
    assert spec["decimals"] == 0
    assert spec["min"] == 0
    assert spec["insights"] is True
    assert spec["title"] == {"ko": "오늘 등록", "en": "Registered today"}
    assert spec["units"] == {"ko": "대", "en": "cars"}


def test_the_flow_card_makes_the_date_optional_and_the_plate_required():
    """`required: false` on `visit_date` **is** the two-mode behaviour the maintainer asked
    for — plate alone means today. Making it mandatory would remove the mode, not tighten
    it."""
    spec = json.loads(
        (ROOT / ".homeycompose/flow/actions/register_visitor.json").read_text(
            encoding="utf-8"
        )
    )
    args = {arg["name"]: arg for arg in spec["args"]}

    assert args["device"]["filter"] == "driver_id=visitcar"
    assert args["car_number"]["type"] == "text"
    assert args["car_number"]["required"] is True
    assert args["car_number"]["placeholder"]["ko"] == "12가1234"
    assert args["visit_date"]["type"] == "date"
    assert args["visit_date"]["required"] is False


def test_the_today_card_has_no_date_argument_at_all():
    """Not an optional date, not a hidden one — absent. An empty optional field already means
    today, so this card's entire reason to exist is that the Flow editor shows no field to fill
    in wrongly, and a wrong day on access control fails silently at the gate."""
    spec = _card("register_visitor_today")
    args = {arg["name"]: arg for arg in spec["args"]}

    assert sorted(args) == ["car_number", "device"]
    assert args["device"]["filter"] == "driver_id=visitcar"
    assert args["car_number"]["type"] == "text"
    assert args["car_number"]["required"] is True
    assert args["car_number"]["placeholder"]["ko"] == "12가1234"
    assert "[[visit_date]]" not in json.dumps(spec, ensure_ascii=False)


def test_the_two_register_cards_do_not_share_a_title():
    """A Flow editor listing two cards called the same thing is worse than listing one: the
    user picks by title and cannot tell which one takes a date."""
    dated = _card("register_visitor")
    today = _card("register_visitor_today")

    assert dated["title"] == {"ko": "방문 차량 등록 (날짜 지정)",
                              "en": "Register a visitor (choose a date)"}
    assert today["title"] == {"ko": "방문 차량 등록 (오늘)",
                              "en": "Register a visitor (today)"}
    for language in ("ko", "en"):
        assert dated["title"][language] != today["title"][language]


def test_both_cards_warn_that_they_write_to_a_building():
    """The disclosure is a property of the write, so a second route to it carries the same
    sentence — a card whose hint omitted it would be the quiet one people reach for."""
    for card_id in ("register_visitor", "register_visitor_today"):
        hint = _card(card_id)["hint"]
        assert "출입통제 시스템" in hint["ko"]
        assert "access-control system" in hint["en"]


def _card(card_id: str) -> dict:
    return json.loads(
        (ROOT / ".homeycompose/flow/actions" / f"{card_id}.json").read_text(encoding="utf-8")
    )


def test_the_driver_declares_exactly_the_today_count_sensor():
    """**One, and it is 오늘 등록.** Every paired device has it from pairing, which is also what
    keeps a freshly paired lot from being capability-less: the ten `iparking_quick_*` schemas are
    declared app-wide but must be added per device, because a new lot has no favourites yet and
    listing any of them here would hand every device a dead button.

    Still no `poll_interval`: the count is already updated the instant this app's own register,
    cancel or history read answers, so a knob could only invite the tightening the 3600 s cadence
    exists to avoid."""
    spec = json.loads(
        (ROOT / "drivers/visitcar/driver.compose.json").read_text(encoding="utf-8")
    )
    settings = {item["id"]: item for item in spec["settings"]}

    assert spec["capabilities"] == [CAPABILITY_TODAY_COUNT]
    assert spec["class"] == "sensor"
    assert spec["connectivity"] == ["cloud"]
    assert sorted(settings) == ["favorites"]
    assert "poll_interval" not in json.dumps(spec)
    assert CAPABILITY_PARK_NAME not in json.dumps(spec)


def test_both_locales_carry_the_flow_notification_keys():
    """The notification text is the misparse mitigation, so it is translated rather than
    hardcoded — and a key present in only one locale would silently fall back to English on a
    Korean hub, which is this app's default audience."""
    for language in ("ko", "en"):
        table = json.loads(
            (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
        )
        for key in ("flow_registered", "flow_already_registered", "date_ambiguous"):
            assert "{date}" in table[key]
        assert "{plate}" in table["flow_registered"]


def test_the_capability_and_driver_assets_exist():
    """`homey app validate` checks these, but only after a build; a missing driver image is
    otherwise a device with a blank tile."""
    assert (ROOT / "assets/capabilities/visitcar.svg").exists()
    # `parking.svg` went with the 주차장명 capability that referenced it. An orphaned asset in a
    # public repo is the kind of thing a reader mistakes for a live surface.
    assert not (ROOT / "assets/capabilities/parking.svg").exists()
    assert (ROOT / "drivers/visitcar/assets/icon.svg").exists()
    for name in ("small", "large", "xlarge"):
        assert (ROOT / "drivers/visitcar/assets/images" / f"{name}.png").exists()


def test_lots_ok_still_describes_the_lot_these_tests_pair():
    """A guard on the shared fixture rather than on this module: `_row()` mirrors
    `conftest.lots_ok`, and the day those two disagree these tests would pass against a lot
    shape the client never sees."""
    row = lots_ok()["resultData"][0]

    assert {key: row[key] for key in _row()} == _row()


# --- 자주 오는 차량: the tile buttons -------------------------------------------
#
# Twenty settings in, up to ten buttons out, and the failures worth guarding against are again
# the silent ones:
#
# * **A pair that does not count produces nothing.** A user who typed `12가 456` sees no button
#   and gets no error, so the rejection has to reach the log naming the slot — otherwise the
#   only diagnosis available is "it doesn't work".
# * **A button that latches looks like a switch somebody left on.** That was the v0.1.3 defect,
#   and it is now prevented in the *manifest* (`getable: false`) rather than papered over with a
#   reset write — so what is tested is that nothing writes a capability value at all.
# * **A button that outlives its pair is a live control wired to a deleted plate**, and pressing
#   it writes to a building.
# * **The SDK spellings are confirmed snake_case on hardware**, but the tolerance stays and is
#   tested against a fake that implements exactly one contract at a time — a permissive fake
#   would pass whichever spelling the code happened to try first, which is precisely how the
#   `create_notification` dict shape got through review.

FAV_NAME = "엄마차"
FAV_NAME_2 = "아빠차"
PLATE_2 = "34나7890"
#: The maintainer's own example, and the reason `plate.normalize_plate` is on this path: a
#: space. The site itself just rejects it.
PLATE_WITH_SPACE = "12가 4567"


def _favorite_settings(*pairs) -> dict:
    """Device settings for `pairs` given as `(index, name, plate)`."""
    settings = {}
    for index, name, plate in pairs:
        settings[favorite_name_setting(index)] = name
        settings[favorite_plate_setting(index)] = plate
    return settings


def _quick(dev) -> list[str]:
    """The quick-button capabilities this device currently carries, in slot order."""
    return [cap for cap in dev.get_capabilities() if cap.startswith("iparking_quick_")]


async def _save(dev, settings: dict):
    """Save device settings the way a hub does. Three details are faithful, and each was a bug.

    * **The new values are persisted before the hook runs.** Homey has already stored them by
      the time `on_settings` is called, which is what makes `press_favorite`'s re-read of
      `get_settings()` see the same favourite the reconcile just built a button for.
    * **`set_settings` is refused for the duration of the hook.** `Device._on_settings` sets
      `_on_settings_pending` around the call and `Device.set_settings` raises while it is set —
      so an inline write-back cannot land. On the maintainer's hub it did not: `fav_plate_1`
      stayed ``12가 3456`` and the refusal went into a log nobody was watching.
    * **The loop keeps turning after the hook returns.** The write-back is a scheduled task, so
      a helper that stopped at `on_settings`'s return would report it as never happening.
    """
    old = dict(dev.settings)
    dev.settings = dict(settings)
    dev.on_settings_pending = True
    try:
        await dev.on_settings(
            {"oldSettings": old, "newSettings": dict(settings), "changedKeys": sorted(settings)}
        )
    finally:
        dev.on_settings_pending = False
    for _ in range(5):
        await asyncio.sleep(0)


# --- what counts as a pair ----------------------------------------------------


def test_a_pair_counts_only_when_both_halves_are_there_and_the_plate_validates():
    """Four ways to not be a favourite, and none of them may silently vanish: a name with no
    plate, a plate with no name, a plate the vendor's own regex refuses, and an empty slot."""
    favorites, rejected = device_mod.read_favorites({
        **_favorite_settings((1, FAV_NAME, PLATE)),
        favorite_name_setting(2): FAV_NAME_2,
        favorite_plate_setting(3): PLATE_2,
        **_favorite_settings((4, "오타차", "12가456")),
    })

    assert favorites == [device_mod.Favorite(1, FAV_NAME, PLATE)]
    assert [reason.split(":")[0] for reason in rejected] == ["슬롯 2", "슬롯 3", "슬롯 4"]
    assert "차량번호가 비어" in rejected[0]
    assert "이름이 없습니다" in rejected[1]
    assert "형식이 올바르지 않습니다" in rejected[2]


def test_the_plate_a_user_types_with_a_space_is_accepted_and_stored_without_one():
    """`12가 4567` is the maintainer's own example. The vendor's site does not trim, it just
    refuses — so a feature that inherited that would fail on the first favourite anyone enters.
    """
    favorites, rejected = device_mod.read_favorites(
        _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))
    )

    assert favorites == [device_mod.Favorite(1, FAV_NAME, PLATE)]
    assert rejected == []


def test_a_rejected_slot_is_named_in_the_log_with_the_plate_masked(make_device):
    """"Do not silently ignore a typo'd plate" — and criterion 14 still applies to the sentence
    that says so, because a favourite's plate is a plate."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((2, "오타차", "12가 456")),
    )

    assert any("슬롯 2" in line and "형식이 올바르지 않습니다" in line for line in dev.logs)
    assert not any("12가456" in line for line in dev.logs)
    assert _quick(dev) == []


# --- the buttons themselves ---------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 3, MAX_FAVORITES])
def test_a_device_shows_exactly_as_many_buttons_as_it_has_pairs(make_device, count):
    """The whole reason ten capabilities are declared statically but added at runtime: a
    device with two favourites must not show ten buttons, eight of them dead."""
    plates = [PLATE, PLATE_2, "56다1234", "임1234", "외교123456",
              "78마9012", "90바3456", "11사7890", "22아1234", "33자5678"]
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings(
            *((index, f"차량{index}", plates[index - 1]) for index in range(1, count + 1))
        ),
    )

    assert _quick(dev) == [quick_capability(index) for index in range(1, count + 1)]


def test_the_buttons_are_reconciled_at_on_init_so_a_restart_does_not_lose_them(make_device):
    """A paired device is rebuilt from scratch on every app start and capabilities added at
    runtime are the device's own state — so `on_init` has to put them back, with no settings
    change to react to."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE), (3, FAV_NAME_2, PLATE_2)),
    )

    assert _quick(dev) == [quick_capability(1), quick_capability(3)]


def test_the_button_label_is_the_favourites_own_name(make_device):
    """`[엄마차 방문 등록]`, which is the whole request. The manifest title is static and the
    name is typed after install, so `set_capability_options` is the only route it has."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE)),
    )

    assert dev.capability_options[quick_capability(1)]["title"] == "엄마차 방문 등록"
    assert dev.capability_options[quick_capability(1)]["title"] == i18n.translate(
        "quick_button", "ko", name=FAV_NAME
    )


def test_the_label_follows_a_renamed_favourite(make_device):
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE)),
    )

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME_2, PLATE))))

    assert dev.capability_options[quick_capability(1)]["title"] == "아빠차 방문 등록"


def test_a_cleared_favourite_takes_its_button_with_it(make_device):
    """A stale button is a live control wired to a plate the user has already removed, and
    pressing it writes to a real building."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE), (2, FAV_NAME_2, PLATE_2)),
    )
    assert _quick(dev) == [quick_capability(1), quick_capability(2)]

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE))))

    assert _quick(dev) == [quick_capability(1)]


def test_a_slot_cleared_and_filled_in_again_gets_a_working_button(make_device):
    """`remove_capability` takes the listener with it, so the rebind has to happen — and a
    button that lost its listener looks exactly like a working one right up until it is pressed
    and nothing happens."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))
    asyncio.run(_save(dev, {}))
    assert _quick(dev) == []
    dev.listeners.clear()  # what the hub does when the capability goes away

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME_2, PLATE_2))))
    asyncio.run(dev.press(quick_capability(1)))

    assert api.registers[-1]["car_number"] == PLATE_2


def test_a_plate_broken_after_the_fact_also_takes_the_button_away(make_device):
    """Editing a working favourite into an invalid one is the same verdict as never having
    filled it in — silently keeping the old button would leave it registering a plate the
    settings page no longer shows."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE)),
    )

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, "12가456"))))

    assert _quick(dev) == []


# --- the normalized write-back ------------------------------------------------
#
# The write itself is old; **the deferral is the fix**. It has to happen after `on_settings`
# returns, because the SDK refuses it while the hook is pending (`_save` models that refusal, and
# `conftest.Device._set_settings` raises the SDK's own sentence). Inline, on the maintainer's hub,
# `fav_plate_1` stayed ``12가 3456`` — space included — while the log recorded a failure nobody
# was reading. Two properties are asserted separately because they fail separately: that the
# value ends up normalized, and that the write did not happen inside the window.


def test_on_settings_writes_the_normalized_plate_back_so_the_user_sees_it(make_device):
    """The one deliberate runtime **settings** write in the app (the store-write invariant is a
    different object and still holds). A user who saves `12가 4567` and sees it unchanged has no
    way to tell whether the space mattered."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))))

    assert dev.setting_writes == [{favorite_plate_setting(1): PLATE}]
    assert dev.settings[favorite_plate_setting(1)] == PLATE
    assert _quick(dev) == [quick_capability(1)]


def test_the_write_back_is_deferred_until_the_hook_has_returned(make_device):
    """The carried-over defect, pinned. `set_settings` raises for the whole duration of
    `on_settings` — so the assertion that matters is not "the value is normalized" (an inline
    write would leave the *in-memory* dict normalized too and still never reach the hub) but
    that **nothing was written while the hook was running** and the write landed afterwards."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

    async def _observe():
        dev.settings = _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))
        dev.on_settings_pending = True
        await dev.on_settings({"newSettings": dict(dev.settings)})
        during = list(dev.setting_writes)
        # The hub clears its flag only after the hook has returned; until then a write raises.
        dev.on_settings_pending = False
        for _ in range(5):
            await asyncio.sleep(0)
        return during, list(dev.setting_writes)

    during, after = asyncio.run(_observe())

    assert during == []
    assert after == [{favorite_plate_setting(1): PLATE}]
    assert not any("failed" in line for line in dev.logs)


def test_the_button_appears_on_the_same_save_that_the_write_is_deferred_from(make_device):
    """Deferring the *write* must not defer the *feature*. The reconcile runs against the
    normalized dict in memory, so `12가 4567` produces a working button on the save it was typed
    on rather than on the next one — the visible write-back is only about the user seeing what
    was stored."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications())

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))))
    asyncio.run(dev.press(quick_capability(1)))

    assert _quick(dev) == [quick_capability(1)]
    assert api.registers[0]["car_number"] == PLATE


def test_a_second_save_supersedes_a_pending_write_rather_than_racing_it(make_device):
    """Two saves in quick succession: the newer values are the true ones, so the pending write is
    cancelled instead of being allowed to land after them. Convergent either way — the plate is
    idempotent under normalization — but "either way" is not an ordering guarantee."""
    dev, _homey = make_device(api=_StubApi())

    async def _two_saves():
        first = _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))
        second = _favorite_settings((1, FAV_NAME, "34나 7890"))
        dev.settings = dict(first)
        await dev.on_settings({"newSettings": dict(first)})
        dev.settings = dict(second)
        await dev.on_settings({"newSettings": dict(second)})
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_two_saves())

    assert dev.setting_writes == [{favorite_plate_setting(1): PLATE_2}]
    assert dev.settings[favorite_plate_setting(1)] == PLATE_2


def test_on_uninit_cancels_a_pending_write_so_nothing_outlives_the_device(make_device):
    """The poll task is gone; the scheduled write is the only thing this device can still have in
    flight. A `set_settings` that lands after Homey considers the device gone is a write against a
    torn-down object, which is the bug the poll task's teardown existed to prevent."""
    dev, _homey = make_device(api=_StubApi())

    async def _save_then_uninit():
        settings = _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))
        dev.settings = dict(settings)
        await dev.on_settings({"newSettings": dict(settings)})
        await dev.on_uninit()
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_save_then_uninit())

    assert dev.setting_writes == []
    assert dev._normalize_task is None


def test_an_invalid_plate_is_left_exactly_as_the_user_typed_it(make_device):
    """Half-fixing a value that is still going to be rejected, while saying nothing, is worse
    than leaving the typo where its author can see it."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, "12가 456"))))

    assert dev.setting_writes == []


def test_on_init_reconciles_the_buttons_without_writing_any_settings(make_device):
    """The write-back belongs to the save, not to every restart. A space can only reach `on_init`
    if the write that should have removed it failed — and the button works either way, because
    `read_favorites` normalizes before it ever builds a `Favorite`. So boot stays read-only
    rather than rewriting the user's settings behind them on every app start."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE)),
    )

    assert dev.setting_writes == []
    assert dev.settings[favorite_plate_setting(1)] == PLATE_WITH_SPACE
    assert _quick(dev) == [quick_capability(1)]


def test_an_already_normalized_plate_is_not_written_again(make_device):
    """The convergence half of the loop guard: a normalized plate normalizes to itself, so a
    second pass has nothing to write even before `_settings_busy` is consulted."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))
    settings = _favorite_settings((1, FAV_NAME, PLATE))

    asyncio.run(_save(dev, settings))
    asyncio.run(_save(dev, settings))

    assert dev.setting_writes == []


def test_a_re_entered_settings_callback_writes_nothing(make_device):
    """The structural half. This is the app's only settings write from inside a settings
    callback — exactly the shape that loops — and relying on convergence to stop a loop is not
    a guarantee."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))
    dev._settings_busy = True

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))))

    assert dev.setting_writes == []
    assert any("re-entered by our own write" in line for line in dev.logs)


def test_no_log_line_carries_a_favourites_plate(make_device):
    """Criterion 14 on this path too: only the settings *keys* are logged, never the values."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))))

    assert any(favorite_plate_setting(1) in line for line in dev.logs)
    assert not any(PLATE in line for line in dev.logs)


# --- on_settings' unknown call shape -----------------------------------------


@pytest.mark.parametrize("shape", ["event_dict", "positional", "kwargs_camel",
                                   "kwargs_snake", "bare_dict"])
def test_on_settings_tolerates_every_plausible_sdk_call_shape(make_device, shape):
    """Node's SDK3 passes one `{oldSettings, newSettings, changedKeys}` object, SDK2 passed
    three positionals, and every manager this app actually uses is snake_case with plain
    arguments — with **no Python stub shipped with the CLI** to settle which. A signature
    mismatch here presents as settings that save and never produce a button."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))
    old = {}
    new = _favorite_settings((1, FAV_NAME, PLATE))
    dev.settings = dict(new)  # the hub has already stored them by the time the hook runs
    call = {
        "event_dict": lambda: dev.on_settings(
            {"oldSettings": old, "newSettings": new, "changedKeys": list(new)}
        ),
        "positional": lambda: dev.on_settings(old, new, list(new)),
        "kwargs_camel": lambda: dev.on_settings(newSettings=new, oldSettings=old),
        "kwargs_snake": lambda: dev.on_settings(new_settings=new, old_settings=old),
        "bare_dict": lambda: dev.on_settings(new),
    }[shape]

    asyncio.run(call())

    assert _quick(dev) == [quick_capability(1)]


def test_the_positional_shape_reads_the_second_dict_not_the_first(make_device):
    """`(old, new, changed)`: picking the first would reconcile against the settings the user
    just replaced, i.e. the buttons would lag one edit behind — which looks like a caching bug
    and is not one."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))
    old = _favorite_settings((1, FAV_NAME, PLATE))
    new = _favorite_settings((1, FAV_NAME, PLATE), (2, FAV_NAME_2, PLATE_2))
    dev.settings = dict(new)

    asyncio.run(dev.on_settings(old, new, [favorite_name_setting(2)]))

    assert _quick(dev) == [quick_capability(1), quick_capability(2)]


def test_on_settings_re_reads_the_settings_when_it_can_tell_nothing(make_device):
    """The fallback that lets the tolerance above stay conservative rather than clever."""
    dev, _homey = make_device(
        api=_StubApi(lot_rows=[_row()]),
        settings=_favorite_settings((1, FAV_NAME, PLATE)),
    )
    asyncio.run(dev._sdk_call(("remove_capability",), quick_capability(1)))

    asyncio.run(dev.on_settings())

    assert _quick(dev) == [quick_capability(1)]
    assert any("no readable settings" in line for line in dev.logs)


# --- pressing a button -------------------------------------------------------


def test_pressing_a_button_registers_that_plate_for_today_in_kst(make_device):
    """The request, end to end: press `[엄마차 방문 등록]`, `12가4567` is registered for today.
    `visit_date` reaches `register` as `None`, so the day is resolved by `dates.today_kst()` on
    the same path every other register on this device takes."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications,
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    ok = asyncio.run(dev.press(quick_capability(1)))

    assert ok is True
    assert api.registers == [
        {"car_number": PLATE, "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ, "visit_date": None}
    ]
    assert notifications.excerpts == [
        i18n.translate("quick_registered", "ko", name=FAV_NAME, plate=PLATE,
                       date=dates.format_kst_human(dates.today_api()))
    ]


def test_the_notification_names_the_favourite_the_plate_and_the_resolved_date(make_device):
    """All three, because a tile button carries no context of its own: which button fired, what
    it actually registered, and for which day — the same date echo that makes a misparse visible
    on the Flow path."""
    notifications = FakeNotifications()
    dev, _homey = make_device(api=_StubApi(), notifications=notifications,
                             settings=_favorite_settings((2, FAV_NAME, PLATE)))

    asyncio.run(dev.press(quick_capability(2)))

    excerpt = notifications.excerpts[0]
    assert FAV_NAME in excerpt
    assert PLATE in excerpt
    assert dates.format_kst_human(dates.today_api()) in excerpt


def test_a_button_can_be_pressed_twice(make_device):
    """The v0.1.3 defect, from the user's side. It latched because the capability was
    `getable: true`: the press wrote a readable `true`, Homey drew a switch that was on, and the
    second press produced no change for a listener to fire on. With `getable: false` there is no
    value at all — so this asserts what the maintainer actually reported, that pressing the same
    button twice registers twice, and it does so without any reset write to make it true."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    asyncio.run(dev.press(quick_capability(1)))
    asyncio.run(dev.press(quick_capability(1)))

    assert [call["car_number"] for call in api.registers] == [PLATE, PLATE]


def test_a_refused_press_leaves_the_button_alone(make_device):
    """A refusal the user can fix must not also leave them with a dead button — and the way that
    is guaranteed now is that a press touches no state at all, so there is nothing a failed write
    could leave behind. The old `finally` that reset the value was the latch it claimed to cure."""
    dev, _homey = make_device(api=_StubApi(error=NotPermittedError("권한 없음")),
                             notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    with pytest.raises(Exception, match="관리사무소"):
        asyncio.run(dev.press(quick_capability(1)))

    assert quick_capability(1) in dev.get_capabilities()
    assert dev.capability_options[quick_capability(1)]["title"] == "엄마차 방문 등록"


def test_a_press_without_permission_explains_itself_where_the_press_happened(make_device):
    """`can_register` is gated inside `client.register` — before any write — so this asserts the
    other half: a tile has no Flow error branch, so the explanation only exists if it is
    notified. `관리사무소에 문의하세요` is the remedy and the user cannot act on it unseen."""
    notifications = FakeNotifications()
    dev, _homey = make_device(api=_StubApi(error=NotPermittedError("권한 없음")),
                             notifications=notifications,
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    with pytest.raises(Exception, match="관리사무소"):
        asyncio.run(dev.press(quick_capability(1)))

    assert notifications.excerpts == [f"{FAV_NAME} · {i18n.translate('not_permitted', 'ko')}"]


def test_a_duplicate_from_a_button_says_so_rather_than_reporting_failure(make_device):
    """Pressing yesterday's button again today is ordinary; pressing it twice in a minute is
    the most likely real mistake. Neither is a failed action."""
    api = _StubApi(outcome=codes.OUTCOME_ALREADY_REGISTERED)
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications,
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    ok = asyncio.run(dev.press(quick_capability(1)))

    assert ok is True
    assert notifications.excerpts == [
        i18n.translate("quick_already_registered", "ko", name=FAV_NAME, plate=PLATE,
                       date=dates.format_kst_human(dates.today_api()))
    ]


def test_an_uncertain_press_names_the_button_and_still_never_invites_a_retry(make_device):
    """`_prefixed` attributes the sentence without touching it: a retry is what turns one
    uncertain write into two real registrations at a building, and a button makes retrying
    one tap away."""
    uncertain = RegisterUncertain(
        f"{MASKED} 20260805 등록 결과를 확인할 수 없습니다. 다시 등록하지 마시고"
    )
    api = _StubApi(error=uncertain)
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications,
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    with pytest.raises(RegisterUncertain):
        asyncio.run(dev.press(quick_capability(1)))

    assert len(api.registers) == 1
    assert notifications.excerpts[0].startswith(f"{FAV_NAME} · ")
    assert "다시 등록하지 마시고" in notifications.excerpts[0]


def test_pressing_a_button_whose_pair_vanished_writes_nothing(make_device):
    """A button and the plate behind it are two objects on a hub, and the interval between them
    includes the user editing the settings. So the pair is re-read at press time, not cached —
    registering a plate the user has since replaced cannot be taken back."""
    api = _StubApi()
    notifications = FakeNotifications()
    dev, _homey = make_device(api=api, notifications=notifications,
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))
    dev.settings.clear()

    ok = asyncio.run(dev.press(quick_capability(1)))

    assert ok is False
    assert api.registers == []
    assert notifications.excerpts == [i18n.translate("quick_unset", "ko")]
    # And the button that should not have been there is gone by the time the press returns.
    assert _quick(dev) == []


def test_a_press_uses_the_same_register_path_as_the_flow_cards(make_device):
    """Not "currently agrees with" — the same call. Zero retries on `POST /invitations`, the
    recovery re-query, the hourly ceiling and `RegisterUncertain` are guaranteed once because
    they are written once, and a third caller that re-implemented any of them would be a third
    route to a double registration."""
    dev, _homey = make_device(api=_StubApi(), notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))
    calls = []

    async def _spy(*, car_number, visit_date="", label=""):
        calls.append({"car_number": car_number, "visit_date": visit_date, "label": label})
        return True

    dev.flow_register = _spy

    asyncio.run(dev.press(quick_capability(1)))

    assert calls == [{"car_number": PLATE, "visit_date": "", "label": FAV_NAME}]


def test_each_button_registers_its_own_slot(make_device):
    """The closure binds its slot at registration time. Late-bound, all five buttons would
    register whichever favourite was reconciled last — and every one of them would look like it
    worked."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE),
                                                         (4, FAV_NAME_2, PLATE_2)))

    asyncio.run(dev.press(quick_capability(4)))
    asyncio.run(dev.press(quick_capability(1)))

    assert [call["car_number"] for call in api.registers] == [PLATE_2, PLATE]


# --- the SDK surface that cannot be checked off-device ------------------------


@pytest.mark.parametrize("sdk_awaitable", [False, True])
@pytest.mark.parametrize("sdk_spelling", ["snake", "camel"])
def test_the_button_surface_tolerates_either_sdk_spelling(make_device, sdk_spelling,
                                                          sdk_awaitable):
    """`add_capability`, `remove_capability`, `set_capability_options`, `set_settings`,
    `get_settings` and the listener registrar — six unpinned spellings, and the CLI ships no
    Python stub to check any of them against. `sdk_awaitable` covers the other half of
    `compat.resolve`'s contract, where an un-awaited coroutine looks like a successful call and
    does nothing."""
    api = _StubApi()
    dev, _homey = make_device(api=api, notifications=FakeNotifications(),
                             sdk_spelling=sdk_spelling, sdk_awaitable=sdk_awaitable)

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))))
    ok = asyncio.run(dev.press(quick_capability(1)))

    assert ok is True
    assert dev.capability_options[quick_capability(1)]["title"] == "엄마차 방문 등록"
    assert dev.settings[favorite_plate_setting(1)] == PLATE
    assert api.registers[0]["car_number"] == PLATE


def test_the_spelling_that_answered_is_logged_so_one_real_press_settles_it(make_device):
    """None of the six can be verified without the hub, so the log is the instrument: one press
    on the maintainer's own Homey names every spelling that worked."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]),
                              settings=_favorite_settings((1, FAV_NAME, PLATE)))

    assert any(f"add_capability({quick_capability(1)}) ok" in line for line in dev.logs)
    assert any(f"set_capability_options({quick_capability(1)} title) ok" in line
               for line in dev.logs)
    assert any(f"press handler bound via register_capability_listener({quick_capability(1)})"
               in line for line in dev.logs)


def test_a_runtime_with_no_capability_mutators_still_registers(make_device):
    """A device that cannot grow a button must still be a working Flow target — that is the
    requirement the maintainer stated first, and the tile buttons are a convenience on top of
    it. So `on_init` finishes, nothing raises, and `flow_register` still writes."""
    api = _StubApi()
    dev, _homey = make_device(api=api, sdk_spelling="none", notifications=FakeNotifications(),
                              settings=_favorite_settings((1, FAV_NAME, PLATE)))

    assert _quick(dev) == []
    assert asyncio.run(dev.flow_register(car_number=PLATE, visit_date="")) is True
    # The message says what was observed — that neither accessor exists — rather than guessing
    # why. An earlier version logged "exposes no get_settings" whenever the settings came back
    # unusable *for any reason*, and on the real hub the method was right there while a too-narrow
    # isinstance check discarded its answer. The log then pointed away from the actual cause.
    assert any("has neither get_settings nor getSettings" in line for line in dev.logs)


def test_a_failing_add_capability_does_not_take_the_device_down(make_device):
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]),
                              add_capability_error=RuntimeError("capability refused"),
                              settings=_favorite_settings((1, FAV_NAME, PLATE)))

    assert _quick(dev) == []
    assert any("add_capability" in line and "failed" in line for line in dev.logs)


# --- the manifest side -------------------------------------------------------


def test_one_button_capability_is_declared_per_favourite_slot():
    """**The count guard, in both directions.** Homey has no dynamic-capability declaration: a
    capability missing from `app.json` cannot be added to a device at all, and one declared with
    no slot behind it is dead weight in the manifest. So a file without a slot and a slot without
    a file both fail here — on a hub the first shows up only as an `add_capability` error and the
    second not at all."""
    directory = ROOT / ".homeycompose/capabilities"

    assert {path.name for path in directory.glob("iparking_quick_*.json")} == {
        f"{quick_capability(index)}.json" for index in range(1, MAX_FAVORITES + 1)
    }
    assert MAX_FAVORITES == 10


def test_every_button_capability_is_a_momentary_push_button():
    """The push-button fix, at the manifest level where it actually lives.

    Shaped exactly like Homey's own `button`
    (`homey-lib/assets/capability/capabilities/button.json`): `getable: false`, `setable: true`,
    `uiComponent: "button"`, `uiQuickAction: true`.

    **`getable: false` is the fix and `getable: true` was the bug.** v0.1.3 set it true and
    justified it as needed to un-latch the press; the reasoning was inverted. A readable value
    *is* a state, Homey renders state as a switch, and the maintainer's tile duly sat there
    looking permanently pressed. With nothing to read there is nothing to latch and nothing to
    reset — which is why `device.py` no longer writes a capability value anywhere."""
    directory = ROOT / ".homeycompose/capabilities"
    for index in range(1, MAX_FAVORITES + 1):
        spec = json.loads(
            (directory / f"{quick_capability(index)}.json").read_text(encoding="utf-8")
        )
        assert spec["type"] == "boolean"
        assert spec["uiComponent"] == "button"
        assert spec["setable"] is True
        assert spec["getable"] is False
        assert spec["uiQuickAction"] is True
        # A momentary button is not a measurement; an insights graph of it would be empty.
        assert "insights" not in spec
        assert spec["title"]["ko"].endswith("방문 등록")
        assert spec["title"]["en"]
        # The `$comment` has to *teach* the right reason. Asserted positively rather than by
        # forbidding the word "un-latch": the comment names the old justification in order to
        # correct it, and a grep that cannot tell a rule from its retraction would fail on the
        # explanation and then get deleted for crying wolf.
        assert "`getable: false` is what makes this a momentary push button" in spec["$comment"]
        assert "inverted" in spec["$comment"]


def test_the_favourite_settings_are_labelled_pairs_in_a_group():
    """Two text fields per slot — twenty of them — in a group so they read as advanced settings
    rather than cluttering the device page, and labelled in both languages, because the settings
    page is the only place their numbering can be matched up."""
    spec = json.loads(
        (ROOT / "drivers/visitcar/driver.compose.json").read_text(encoding="utf-8")
    )
    group = next(item for item in spec["settings"] if item["id"] == "favorites")
    children = {child["id"]: child for child in group["children"]}

    assert group["type"] == "group"
    assert group["label"] == {"ko": "자주 오는 차량", "en": "Frequent vehicles"}
    assert len(children) == 2 * MAX_FAVORITES
    for index in range(1, MAX_FAVORITES + 1):
        name = children[favorite_name_setting(index)]
        plate = children[favorite_plate_setting(index)]
        assert name["type"] == plate["type"] == "text"
        assert name["value"] == plate["value"] == ""
        assert name["label"]["ko"] == f"자주 오는 차량 이름 {index}"
        assert plate["label"]["ko"] == f"차량번호 {index}"
        assert name["label"]["en"] and plate["label"]["en"]
        # The example goes where the format errors happen, and it is the site's own hint.
        assert "12가1234" in plate["hint"]["ko"]
        assert "12가1234" in plate["hint"]["en"]


def test_both_locales_carry_the_button_keys():
    """A key present in only one locale falls back to English on a Korean hub — this app's
    default audience — and these four are the only text a tile press ever produces."""
    for language in ("ko", "en"):
        table = json.loads(
            (ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8")
        )
        assert "{name}" in table["quick_button"]
        for key in ("quick_registered", "quick_already_registered"):
            for placeholder in ("{name}", "{plate}", "{date}"):
                assert placeholder in table[key]
        assert table["quick_unset"]


def test_the_settings_write_is_the_only_runtime_write_and_the_store_is_still_untouched():
    """Criterion 18 restated now that a runtime write exists. `set_settings` is deliberate —
    user input, normalized in place, visible on the page it was typed on. The **store** is a
    different object with a different lifetime and still has nothing to write: `stor_seq`,
    `park_seq` and `lot_id` are fixed at pairing."""
    source = (ROOT / "iparking_lib/visitcar/device.py").read_text(encoding="utf-8")

    assert "set_store_value(" not in source.replace("`set_store_value`", "")
    assert '"set_settings", "setSettings"' in source


