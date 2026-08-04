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

from iparking_lib import i18n
from iparking_lib.const import (
    CAPABILITY_PARK_NAME,
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

    def __init__(self, *, lots=(), lot_rows=None, outcome=codes.OUTCOME_OK, error=None):
        self._lots = list(lots)
        self._lot_rows = lot_rows
        self._outcome = outcome
        self._error = error
        self.lot_calls = []
        self.registers = []

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

    Boot runs for real — `on_init` seeds the capability from the store, starts the task, the
    task acquires the shared session and performs the first poll — and is then torn down, so
    every later poll in a test is one this file drives explicitly. `_api` survives the
    teardown, which is what makes `asyncio.run(dev._poll_once())` a shipping code path rather
    than a re-implementation of one.
    """

    def _make(*, api=None, store=None, capabilities=(CAPABILITY_PARK_NAME,), ticks=30,
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


# --- the device: the 주차장명 sensor -------------------------------------------


def test_the_capability_is_answered_from_the_store_before_any_request(make_homey):
    """A lot's name is fixed at pairing and does not need the network to be true, so a hub
    that boots offline shows 주차장명 immediately rather than an empty tile that looks
    broken."""
    homey = make_homey(api=None)
    dev = device_mod.VisitCarDevice_(homey=homey, store=_store(),
                                    capabilities=[CAPABILITY_PARK_NAME])

    async def _boot_and_stop():
        await dev.on_init()
        # Read before the poll task has had a chance to run at all.
        value = dev.get_capability_value(CAPABILITY_PARK_NAME)
        await dev._teardown()
        return value

    assert asyncio.run(_boot_and_stop()) == PARK_NAME


def test_the_boot_poll_reads_the_name_from_the_server(make_device):
    api = _StubApi(lot_rows=[_row(park_name="예시동 샘플아파트[출입통제C]")])
    dev, _homey = make_device(api=api)

    assert api.lot_calls == [STOR_SEQ]
    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == "예시동 샘플아파트[출입통제C]"
    assert any("lot renamed" in line for line in dev.logs)


def test_a_poll_is_one_request_per_tick(make_device):
    """24 requests/day/device. Politeness enforced by arithmetic rather than asserted — and
    `parking_lots` rather than `enumerate_lots` is what keeps it one request no matter how
    many stores the account holds."""
    api = _StubApi(lot_rows=[_row()])
    dev, _homey = make_device(api=api)
    before = len(api.lot_calls)

    asyncio.run(dev._poll_once())

    assert len(api.lot_calls) == before + 1


def test_the_device_reaches_its_poll_loop(make_device):
    """The boot sequence is guarded as one block, so a failure in it cannot escape before the
    loop is entered — a device that never reaches the loop polls never again until the app
    restarts, silently."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

    assert any("next poll in" in line for line in dev.logs)


def test_one_failure_keeps_the_device_available_and_two_do_not(make_device):
    """Two rather than one: a single dropped request against a cloud API addressed over plain
    HTTP is ordinary; two in a row is a pattern."""
    assert MAX_POLL_FAILURES == 2
    api = _StubApi(lot_rows=[_row()])
    dev, _homey = make_device(api=api)
    api._lot_rows = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(dev._poll_once())
    assert dev.availability[-1] == ("available", None)

    with pytest.raises(RuntimeError):
        asyncio.run(dev._poll_once())
    assert dev.availability[-1] == ("unavailable", "주차장 정보를 가져올 수 없습니다")


def test_recovery_makes_the_device_available_again(make_device):
    api = _StubApi(lot_rows=[_row()])
    dev, _homey = make_device(api=api)
    api._lot_rows = RuntimeError("boom")
    for _ in range(MAX_POLL_FAILURES):
        with pytest.raises(RuntimeError):
            asyncio.run(dev._poll_once())
    api._lot_rows = [_row()]

    asyncio.run(dev._poll_once())

    assert dev._failures == 0
    assert dev.availability[-1] == ("available", None)


def test_a_lot_missing_from_the_account_gets_its_own_reason(make_device):
    """Not a transport failure, but not a healthy device either — and the remedy differs
    (re-pair, or ask the office), so it does not borrow the network message."""
    api = _StubApi(lot_rows=[_row()])
    dev, _homey = make_device(api=api)
    api._lot_rows = [_row(lot_id="1160009999", park_seq=9999)]

    for _ in range(MAX_POLL_FAILURES):
        asyncio.run(dev._poll_once())

    assert dev.availability[-1][0] == "unavailable"
    assert "다시 추가" in dev.availability[-1][1]
    # The last known name is kept: the store value is still the truth about this device.
    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == PARK_NAME


def test_the_lot_is_matched_on_lot_id_before_park_seq(make_device):
    """`park_seq` is only a fallback for a deployment that stops sending `lot_id`. Matching on
    the weaker key first could pick another store's lot that happens to share a `park_seq` —
    the same uncertainty that kept `park_seq` out of `data.id`."""
    api = _StubApi(lot_rows=[[
        _row(lot_id="1160009999", park_seq=PARK_SEQ, park_name="다른 스토어의 주차장"),
        _row(park_name="올바른 주차장"),
    ]])
    dev, _homey = make_device(api=api)

    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == "올바른 주차장"


def test_park_seq_still_matches_when_the_lot_id_is_missing(make_device):
    api = _StubApi(lot_rows=[[{"park_seq": PARK_SEQ, "park_name": "lot_id 없는 응답"}]])
    dev, _homey = make_device(api=api)

    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == "lot_id 없는 응답"


def test_set_is_guarded_by_get_capabilities(make_device):
    """The real SDK raises on a capability the device does not have, so the app filters
    against `get_capabilities()` itself — which is what lets a capability list be edited in
    `driver.compose.json` without every paired device throwing."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]), capabilities=())

    assert dev.get_capability_value(CAPABILITY_PARK_NAME) is None
    assert not any("set iparking_park_name failed" in line for line in dev.logs)


def test_a_dead_poll_task_is_restarted_and_the_handle_is_reassigned(make_device):
    """Both halves were bugs in the sibling app first. Without the reassignment a later
    `on_uninit` cancels the dead original, the restarted loop outlives the device, and it
    keeps writing capability values to a torn-down Device."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

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
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

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
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))
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


def test_teardown_awaits_the_task_so_nothing_outlives_the_device(make_homey):
    dev = device_mod.VisitCarDevice_(homey=make_homey(api=_StubApi(lot_rows=[_row()])),
                                    store=_store(), capabilities=[CAPABILITY_PARK_NAME])

    async def _boot_and_uninit():
        await dev.on_init()
        for _ in range(30):
            await asyncio.sleep(0)
        await dev.on_uninit()
        return dev._poll_task

    task = asyncio.run(_boot_and_uninit())

    assert task.done()
    assert dev._closing is True


async def _no_sleep(seconds, *args, **kwargs):
    return None


async def _noop(*args, **kwargs):
    return None


# --- criteria that are about absence -----------------------------------------


def test_v010_makes_no_runtime_store_writes():
    """Criterion 18 — a goal, not a ban. `stor_seq`, `park_seq` and `lot_id` are fixed at
    pairing and `park_name` lives on the capability, so there is nothing for a store write to
    do; the pattern exists in `com.lomohome.localthings` if a later version needs one."""
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


def test_the_capability_is_a_read_only_string_sensor_with_no_insights():
    """The one capability, and the fields that make it legal at all: Homey has no free-text
    tile *input*, so a string capability must be `setable: false` with `uiComponent: sensor`.

    `insights` is absent deliberately. The schema permits it on a string, but this value is
    the name of a parking lot — it changes when a building office renames the lot, i.e.
    approximately never — so logging it would produce an empty graph and nothing else. See
    `const.CAPABILITY_PARK_NAME`, which says so where a maintainer editing the JSON will look.
    """
    spec = json.loads(
        (ROOT / ".homeycompose/capabilities/iparking_park_name.json").read_text(
            encoding="utf-8"
        )
    )

    assert spec["type"] == "string"
    assert spec["uiComponent"] == "sensor"
    assert spec["getable"] is True
    assert spec["setable"] is False
    assert spec["title"] == {"ko": "주차장", "en": "Parking lot"}
    assert "insights" not in spec


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


def test_the_driver_declares_one_capability_and_no_poll_knob():
    """The driver's *static* capability list stays at one, and that is the 자주 오는 차량 design
    rather than an omission: the five `iparking_quick_*` schemas are declared app-wide, but a
    freshly paired device must start with **zero** buttons. Listing them here would give every
    device five, three of them dead, which is exactly what `add_capability` exists to avoid.

    Still no `poll_interval`: the sensor is a lot's name, which changes approximately never, so
    a knob could only invite the tightening the 3600 s cadence exists to avoid."""
    spec = json.loads(
        (ROOT / "drivers/visitcar/driver.compose.json").read_text(encoding="utf-8")
    )
    settings = {item["id"]: item for item in spec["settings"]}

    assert spec["capabilities"] == [CAPABILITY_PARK_NAME]
    assert spec["class"] == "sensor"
    assert spec["connectivity"] == ["cloud"]
    assert sorted(settings) == ["favorites"]
    assert "poll_interval" not in json.dumps(spec)


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
    assert (ROOT / "assets/capabilities/parking.svg").exists()
    assert (ROOT / "assets/capabilities/visitcar.svg").exists()
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
# Ten settings in, up to five buttons out, and the failures worth guarding against are again
# the silent ones:
#
# * **A pair that does not count produces nothing.** A user who typed `12가 456` sees no button
#   and gets no error, so the rejection has to reach the log naming the slot — otherwise the
#   only diagnosis available is "it doesn't work".
# * **A button that latches works exactly once.** `true` on press is sticky, and the press that
#   matters is the second one.
# * **A button that outlives its pair is a live control wired to a deleted plate**, and pressing
#   it writes to a building.
# * **The SDK spellings cannot be checked off-device at all** (no Python stub ships with the
#   CLI), so the tolerance is tested against a fake that implements exactly one contract at a
#   time — a permissive fake would pass whichever spelling the code happened to try first.

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


def _save(dev, settings: dict):
    """Save device settings the way a hub does: **persist them, then** call the hook.

    The order is faithful on purpose. Homey has already stored the new values by the time
    `on_settings` runs, which is what makes `press_favorite`'s re-read of `get_settings()` see
    the same favourite the reconcile just built a button for. A helper that only passed the
    event object would leave the two disagreeing and hide exactly that.
    """
    old = dict(dev.settings)
    dev.settings = dict(settings)
    return dev.on_settings(
        {"oldSettings": old, "newSettings": dict(settings), "changedKeys": sorted(settings)}
    )


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
    """The whole reason five capabilities are declared statically but added at runtime: a
    device with two favourites must not show five buttons, three of them dead."""
    plates = [PLATE, PLATE_2, "56다1234", "임1234", "외교123456"]
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
    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == PARK_NAME


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


def test_on_settings_writes_the_normalized_plate_back_so_the_user_sees_it(make_device):
    """The one deliberate runtime **settings** write in the app (the store-write invariant is a
    different object and still holds). A user who saves `12가 4567` and sees it unchanged has no
    way to tell whether the space mattered."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]))

    asyncio.run(_save(dev, _favorite_settings((1, FAV_NAME, PLATE_WITH_SPACE))))

    assert dev.setting_writes == [{favorite_plate_setting(1): PLATE}]
    assert dev.settings[favorite_plate_setting(1)] == PLATE
    assert _quick(dev) == [quick_capability(1)]


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


def test_the_button_does_not_latch(make_device):
    """A `boolean` button reports `true` on press and keeps it. Without the reset the control
    works exactly once per app start — and the press that matters is the second one, because the
    first is when the user finds out the button exists."""
    dev, _homey = make_device(api=_StubApi(), notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    asyncio.run(dev.press(quick_capability(1)))

    assert dev.get_capability_value(quick_capability(1)) is False


def test_the_button_is_pressable_again_even_after_a_refused_write(make_device):
    """The reset is in a `finally` for this: a refusal the user can fix must not also leave them
    with a dead button."""
    dev, _homey = make_device(api=_StubApi(error=NotPermittedError("권한 없음")),
                             notifications=FakeNotifications(),
                             settings=_favorite_settings((1, FAV_NAME, PLATE)))

    with pytest.raises(Exception, match="관리사무소"):
        asyncio.run(dev.press(quick_capability(1)))

    assert dev.get_capability_value(quick_capability(1)) is False


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


def test_a_runtime_with_no_capability_mutators_still_gets_its_sensor(make_device):
    """The 주차장명 capability is the requirement the maintainer stated first and it needs no
    buttons at all, so a device that cannot grow one must still poll and still show its name."""
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]), sdk_spelling="none",
                              settings=_favorite_settings((1, FAV_NAME, PLATE)))

    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == PARK_NAME
    assert any("next poll in" in line for line in dev.logs)
    assert any("exposes no get_settings" in line for line in dev.logs)


def test_a_failing_add_capability_does_not_take_the_device_down(make_device):
    dev, _homey = make_device(api=_StubApi(lot_rows=[_row()]),
                              add_capability_error=RuntimeError("capability refused"),
                              settings=_favorite_settings((1, FAV_NAME, PLATE)))

    assert _quick(dev) == []
    assert dev.get_capability_value(CAPABILITY_PARK_NAME) == PARK_NAME
    assert any("add_capability" in line and "failed" in line for line in dev.logs)


# --- the manifest side -------------------------------------------------------


def test_five_button_capabilities_are_declared_and_all_shaped_the_same():
    """Homey has no dynamic-capability declaration: a capability missing from `app.json` cannot
    be added to a device at all, so `MAX_FAVORITES` and these files have to move together — and
    a mismatch shows up only as an `add_capability` failure on a hub."""
    directory = ROOT / ".homeycompose/capabilities"

    assert sorted(path.name for path in directory.glob("iparking_quick_*.json")) == [
        f"{quick_capability(index)}.json" for index in range(1, MAX_FAVORITES + 1)
    ]
    for index in range(1, MAX_FAVORITES + 1):
        spec = json.loads(
            (directory / f"{quick_capability(index)}.json").read_text(encoding="utf-8")
        )
        assert spec["type"] == "boolean"
        assert spec["uiComponent"] == "button"
        assert spec["setable"] is True
        # `getable` — unlike Homey's own `button`, which is getable:false. The press has to be
        # un-latched afterwards, and a value that cannot be read cannot be reset either.
        assert spec["getable"] is True
        # A momentary button is not a measurement; an insights graph of it would be empty.
        assert "insights" not in spec
        assert spec["title"]["ko"].endswith("방문 등록")
        assert spec["title"]["en"]


def test_the_ten_favourite_settings_are_five_labelled_pairs_in_a_group():
    """Ten text fields, exactly as asked for, in a group so they read as advanced settings
    rather than cluttering the device page — and labelled in both languages, because the
    settings page is the only place their numbering can be matched up."""
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
