"""Tests for the settings-page handlers in `api.py`.

Scope is deliberately "everything that does not need a hub". The handlers themselves are thin
— every consequential decision lives in `iparking_lib/iparking/client.py`, which
`test_client.py` and `test_register_path.py` cover — so what is worth pinning here is the
plumbing that fails *silently*:

* `_body` / `_query` guessing the wrong calling convention. Wrong, the handler sees empty
  fields and reports a validation error the user cannot act on; the history table just renders
  empty, which is indistinguishable from an account with no registrations.
* `/status` shaping. `today_kst` is the sole date authority for both the date input's `min` and
  its default, and `can_register` must be read from the **live** login response on every load
  rather than cached — the building office can grant the permission later.
* Redaction. A response body or log line that carries `memb_name` (a home address), the token's
  value, or an unmasked plate.

No event-loop plugin is installed, so each test drives `asyncio.run` itself.
"""

import asyncio
import json

import pytest
from conftest import STOR_SEQ, FakeDrivers

import api
from iparking_lib.const import SETTING_PASSWORD, SETTING_USERNAME
from iparking_lib.iparking import codes, dates
from iparking_lib.iparking.client import (
    AuthEntry,
    IparkingApiError,
    NeedCredentialsError,
    NotPermittedError,
    RegisterResult,
    RegisterUncertain,
)
from iparking_lib.iparking.dates import DateTooFarError

PARK_SEQ = 9001
MEMB_NAME = "999동9999호"
TOKEN = "11111111-2222-3333-4444-555555555555"
PLATE = "12가1236"


# --- _body / _query / _int / _mask --------------------------------------------


def test_body_accepts_a_wrapped_body():
    assert api._body({"body": {"username": "iparking-dev"}}) == {"username": "iparking-dev"}


def test_body_accepts_a_flattened_body():
    """The other half of the contract: a build that spreads the body into kwargs."""
    assert api._body({"username": "iparking-dev"}) == {"username": "iparking-dev"}


def test_body_ignores_a_non_dict_body_kwarg():
    """A `body` that is a string (or None) must not shadow the flattened fields — reading it
    literally is how the form ends up looking empty."""
    flattened = {"body": "not-a-dict", "username": "iparking-dev"}
    assert api._body(flattened) is flattened


@pytest.mark.parametrize("name", ["query", "params", "args"])
def test_query_accepts_every_wrapper_spelling(name):
    assert api._query({name: {"park_seq": "9001"}}) == {"park_seq": "9001"}


def test_query_falls_back_to_flattened_kwargs():
    flattened = {"park_seq": 9001, "stor_seq": 100001}
    assert api._query(flattened) is flattened


def test_query_ignores_a_non_dict_wrapper():
    flattened = {"query": "park_seq=9001", "park_seq": "9001"}
    assert api._query(flattened) is flattened


def test_int_survives_a_query_strings_stringly_typed_numbers():
    # This is why every caller runs the values through `_int`: a query string delivers 9001 as
    # "9001", and `int()` on the raw value would be a TypeError on the flattened contract.
    assert api._int(" 9001 ") == 9001
    assert api._int(9001) == 9001
    assert api._int(None) == 0
    assert api._int("") == 0
    assert api._int("abc", 7) == 7


def test_mask_keeps_only_the_ends_of_an_account_id():
    assert api._mask("iparking-dev") == "i***v"
    assert api._mask("ab") == "***"
    assert api._mask("") == ""


def test_mask_is_not_the_plate_masker():
    """Two different redaction rules on purpose: an account id has no meaningful prefix to
    keep, while a plate keeps its region/class part so a user can tell entries apart. A single
    shared implementation would have to lose one of the two properties."""
    from iparking_lib.iparking.plate import mask_plate

    assert api._mask(PLATE) != mask_plate(PLATE)
    assert mask_plate(PLATE) == "12가****"


# --- GET /status --------------------------------------------------------------


def test_status_reports_the_kst_window_even_with_no_account(make_homey):
    """The date fields are unconditional: an unconfigured page still renders a form, and a
    date input with no `min` lets the user pick a date the server will refuse."""
    homey = make_homey()
    status = asyncio.run(api.get_status(homey))

    assert status["configured"] is False
    assert status["can_register"] is None
    assert status["today_kst"] == dates.today_kst()
    assert status["max_days_ahead"] == dates.MAX_DAYS_AHEAD


def test_status_max_date_is_exactly_the_visit_window(make_homey):
    """`max_date` closes the end of the window `resolve_visit_date` enforces, so the form
    cannot submit a date that is guaranteed to be rejected."""
    from datetime import timedelta

    homey = make_homey()
    status = asyncio.run(api.get_status(homey))
    expected = dates.now_kst().date() + timedelta(days=dates.MAX_DAYS_AHEAD)

    assert status["max_date"] == expected.isoformat()
    # And it really is the far end of what the register path will accept.
    assert str(dates.resolve_visit_date(status["max_date"])) == expected.strftime("%Y%m%d")


def test_status_today_is_kst_not_the_hosts_timezone(make_homey, monkeypatch):
    """`today_kst` derives from a fixed +09:00 offset. Moving the host's TZ must not move it —
    "today" here means today at a parking lot in Korea, which is the only thing the vendor's
    server accepts."""
    homey = make_homey()
    before = asyncio.run(api.get_status(homey))["today_kst"]
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    import time as _time

    if hasattr(_time, "tzset"):
        _time.tzset()
    assert asyncio.run(api.get_status(homey))["today_kst"] == before


def test_status_reads_can_register_from_the_live_session(make_homey, logged_in_api):
    live = logged_in_api(stores=((STOR_SEQ, True),))
    homey = make_homey(api=live, settings={SETTING_USERNAME: "iparking-dev"})
    status = asyncio.run(api.get_status(homey))

    assert status["configured"] is True
    assert status["logged_in"] is True
    assert status["can_register"] is True
    assert status["stores"] == 1


def test_status_re_reads_can_register_on_every_call(make_homey, logged_in_api):
    """The whole reason `can_register` is not cached at pairing: the building office can grant
    the permission after the account was set up, and the settings page has to notice without a
    re-pair. Flipping the live session's flag between two calls must change the answer."""
    live = logged_in_api(stores=((STOR_SEQ, False),))
    homey = make_homey(api=live, settings={SETTING_USERNAME: "iparking-dev"})

    assert asyncio.run(api.get_status(homey))["can_register"] is False

    live.auth_entries = [AuthEntry(STOR_SEQ, True)]
    assert asyncio.run(api.get_status(homey))["can_register"] is True


def test_status_distinguishes_refused_from_unknown(make_homey):
    """`False` is a refusal the page renders as a banner; `None` is "could not determine".
    Collapsing them would tell a user with a network problem to go and talk to the building
    office."""
    homey = make_homey(
        settings={SETTING_USERNAME: "iparking-dev"},
        shared_api_error=NeedCredentialsError("need an account"),
    )
    status = asyncio.run(api.get_status(homey))

    assert status["ok"] is False
    assert status["can_register"] is None
    assert status["key"] == "need_credentials"
    # Still usable as a form.
    assert status["today_kst"] == dates.today_kst()


def test_status_never_returns_the_password_or_the_home_address(make_homey, logged_in_api):
    live = logged_in_api(memb_name=MEMB_NAME, token=TOKEN)
    homey = make_homey(
        api=live,
        settings={SETTING_USERNAME: "iparking-dev", SETTING_PASSWORD: "synthetic-pw"},
    )
    rendered = json.dumps(asyncio.run(api.get_status(homey)), ensure_ascii=False)

    assert "synthetic-pw" not in rendered
    assert MEMB_NAME not in rendered
    assert TOKEN not in rendered


# --- GET /diagnostics ---------------------------------------------------------


def test_diagnostics_reports_the_token_by_presence_and_length_only(make_homey, logged_in_api):
    live = logged_in_api(token=TOKEN, memb_name=MEMB_NAME)
    homey = make_homey(api=live, settings={SETTING_USERNAME: "iparking-dev"})
    report = asyncio.run(api.diagnostics(homey))

    assert report["token_present"] is True
    assert report["token_length"] == len(TOKEN)
    rendered = json.dumps(report, ensure_ascii=False)
    assert TOKEN not in rendered
    # A home address, required by the register body and reported nowhere.
    assert MEMB_NAME not in rendered


def test_diagnostics_masks_the_account_id(make_homey, logged_in_api):
    homey = make_homey(api=logged_in_api(), settings={SETTING_USERNAME: "iparking-dev"})
    report = asyncio.run(api.diagnostics(homey))

    assert report["username_masked"] == "i***v"
    assert "iparking-dev" not in json.dumps(report, ensure_ascii=False)


def test_diagnostics_reports_the_scheme_per_host_by_name(make_homey, logged_in_api):
    """Asserted by host name rather than derived from `SCHEMES`, for the same reason
    `test_client.py` spells its URLs out: a test that read the table would agree with a table
    that had been edited wrongly, and "surely both should be https" is the single most likely
    well-meant regression in this app."""
    homey = make_homey(api=logged_in_api(), settings={SETTING_USERNAME: "iparking-dev"})
    hosts = {row["host"]: row for row in asyncio.run(api.diagnostics(homey))["hosts"]}

    assert hosts["oauth.parkingcloud.co.kr"]["scheme"] == "https"
    assert hosts["oauth.parkingcloud.co.kr"]["required_scheme"] == "https"
    assert hosts["members.iparking.co.kr"]["scheme"] == "http"
    # Deliberately absent: the day the vendor fixes their TLS, an upgrade must improve this app
    # rather than break it.
    assert hosts["members.iparking.co.kr"]["required_scheme"] is None


def test_diagnostics_only_claims_a_final_scheme_it_can_vouch_for(make_homey, logged_in_api):
    """A successful login *is* proof for oauth — `client._require_scheme` refuses any other
    final scheme on that response. Nothing asserts or records members', so it stays `None`
    rather than echoing the policy value back as though it had been measured."""
    homey = make_homey(api=logged_in_api(), settings={SETTING_USERNAME: "iparking-dev"})
    hosts = {row["host"]: row for row in asyncio.run(api.diagnostics(homey))["hosts"]}
    assert hosts["oauth.parkingcloud.co.kr"]["final_scheme"] == "https"
    assert hosts["members.iparking.co.kr"]["final_scheme"] is None

    cold = make_homey()
    cold_hosts = {row["host"]: row for row in asyncio.run(api.diagnostics(cold))["hosts"]}
    assert cold_hosts["oauth.parkingcloud.co.kr"]["final_scheme"] is None


def test_diagnostics_reports_can_register_live(make_homey, logged_in_api):
    """The place a user looks to find out *why* the register card is disabled, so a stale flag
    here would be worse than no flag."""
    live = logged_in_api(stores=((STOR_SEQ, False),))
    homey = make_homey(api=live, settings={SETTING_USERNAME: "iparking-dev"})
    assert asyncio.run(api.diagnostics(homey))["can_register"] is False


# --- POST /credentials --------------------------------------------------------


def test_save_credentials_refuses_empty_fields(make_homey):
    homey = make_homey()
    result = asyncio.run(api.save_credentials(homey, body={"username": "", "password": ""}))

    assert result["ok"] is False
    assert result["key"] == "need_credentials"
    assert homey.settings.writes == []


def test_save_credentials_stores_nothing_until_the_login_succeeds(make_homey):
    """A typo must not clobber a working account, which is why `reauth` runs first and the
    writes come after it."""
    homey = make_homey(reauth_error=IparkingApiError("로그인에 실패했습니다.", "2002"))
    result = asyncio.run(
        api.save_credentials(homey, body={"username": "iparking-dev", "password": "wrong"})
    )

    assert result["ok"] is False
    assert result["code"] == "2002"
    assert homey.settings.values.get(SETTING_PASSWORD) is None


def test_save_credentials_restores_the_shared_session_after_a_rejection(make_homey):
    """`reauth` points the shared client at the new credentials *before* trying them, so a
    rejected save leaves it holding a password the server refused. Without the restore, one
    typo logs every running device out until the app restarts."""
    homey = make_homey(reauth_error=IparkingApiError("nope", "2002"))
    asyncio.run(
        api.save_credentials(homey, body={"username": "iparking-dev", "password": "wrong"})
    )
    assert homey.app.calls == ["reauth", "shared_api"]


def test_save_credentials_refuses_an_account_with_no_stores(make_homey, logged_in_api):
    homey = make_homey(api=logged_in_api(stores=()))
    result = asyncio.run(
        api.save_credentials(homey, body={"username": "iparking-dev", "password": "pw"})
    )

    assert result["ok"] is False
    assert result["key"] == "no_stores"
    assert homey.settings.values.get(SETTING_USERNAME) is None
    # The login succeeded, so the shared client is holding credentials we just refused to save.
    assert homey.app.calls[-1] == "shared_api"


def test_save_credentials_stores_the_account_and_reports_the_permission(
    make_homey, logged_in_api
):
    homey = make_homey(api=logged_in_api(stores=((STOR_SEQ, True), (100002, False))))
    result = asyncio.run(
        api.save_credentials(homey, body={"username": "iparking-dev", "password": "pw"})
    )

    assert result == {"ok": True, "configured": True, "can_register": True, "stores": 2}
    assert homey.settings.values[SETTING_USERNAME] == "iparking-dev"
    assert homey.settings.values[SETTING_PASSWORD] == "pw"


def test_save_credentials_never_logs_the_password_or_the_id(make_homey, logged_in_api):
    homey = make_homey(api=logged_in_api())
    asyncio.run(
        api.save_credentials(
            homey, body={"username": "iparking-dev", "password": "synthetic-pw"}
        )
    )
    logged = "\n".join(homey.app.logs)

    assert "synthetic-pw" not in logged
    assert "iparking-dev" not in logged


def test_save_credentials_accepts_a_flattened_body(make_homey, logged_in_api):
    """The contract `_body` exists for. Guessing wrong here means the save button reports
    "아이디와 비밀번호를 입력하세요" against a filled-in form."""
    homey = make_homey(api=logged_in_api())
    result = asyncio.run(
        api.save_credentials(homey, username="iparking-dev", password="pw")
    )
    assert result["ok"] is True


# --- POST /credentials-clear --------------------------------------------------


def test_clear_credentials_disables_the_session_before_forgetting_the_account(
    make_homey, logged_in_api
):
    """The order is load-bearing. Every caller caches the session object it was handed, so
    clearing the saved account first would leave them polling a live session for an account
    that no longer exists."""
    live = logged_in_api()
    homey = make_homey(
        api=live,
        settings={SETTING_USERNAME: "iparking-dev", SETTING_PASSWORD: "synthetic-pw"},
    )
    result = asyncio.run(api.clear_credentials(homey))

    assert result == {"ok": True, "configured": False}
    assert homey.app.calls == ["logout"]
    assert homey.settings.unsets == [SETTING_USERNAME, SETTING_PASSWORD]
    # And the in-memory token — the only copy that exists — went with it.
    assert live.access_token == ""
    assert live.disabled is True


def test_clear_credentials_keeps_the_ui_language(make_homey, logged_in_api):
    """A display preference, not a credential."""
    from iparking_lib.const import SETTING_LANGUAGE

    homey = make_homey(
        api=logged_in_api(),
        settings={SETTING_USERNAME: "iparking-dev", SETTING_LANGUAGE: "ko"},
    )
    asyncio.run(api.clear_credentials(homey))
    assert homey.settings.values[SETTING_LANGUAGE] == "ko"


# --- POST /language -----------------------------------------------------------


def test_set_language_records_what_the_webview_reported(make_homey):
    homey = make_homey(language="en")
    assert asyncio.run(api.set_language(homey, body={"language": "ko-KR"}))["language"] == "ko"


def test_set_language_ignores_a_value_that_is_not_a_language(make_homey):
    from iparking_lib.const import SETTING_LANGUAGE

    homey = make_homey(settings={SETTING_LANGUAGE: "ko"})
    asyncio.run(api.set_language(homey, body={"language": "12"}))
    assert homey.settings.values[SETTING_LANGUAGE] == "ko"


# --- POST /register -----------------------------------------------------------


class _RegisterApi:
    """Stands in for the shared session on the register path.

    A stub rather than the real client because what is under test here is the *shaping* of the
    answer — `client.register()`'s own behaviour (zero retries, the recovery re-query, the
    existence predicate) is `test_register_path.py`'s subject and must not be re-asserted from
    a second place with a second set of expectations.
    """

    def __init__(self, outcome=None, error=None):
        self.access_token = TOKEN
        self.memb_name = MEMB_NAME
        self.auth_entries = [AuthEntry(STOR_SEQ, True)]
        self.api_host = "members.iparking.co.kr"
        self.calls = []
        self._outcome = outcome
        self._error = error

    @property
    def logged_in(self):
        return True

    @property
    def can_register(self):
        return True

    async def register(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return RegisterResult(self._outcome, dates.resolve_visit_date(dates.today_kst()))


def test_register_requires_a_lot(make_homey):
    homey = make_homey(api=_RegisterApi(codes.OUTCOME_OK))
    result = asyncio.run(api.register_visitor(homey, body={"car_number": PLATE}))
    assert result["ok"] is False


def test_register_success_echoes_the_date_it_actually_used(make_homey):
    """A Homey Flow `date` argument in `mm-dd-yyyy` order is shape-identical to `dd-mm-yyyy`,
    and a wrong day on access control is silent. Echoing the resolved date is what makes a
    misparse visible on first use instead of at a closed gate."""
    session = _RegisterApi(codes.OUTCOME_OK)
    homey = make_homey(api=session)
    result = asyncio.run(api.register_visitor(homey, body={
        "car_number": "12가 1236", "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))

    assert result["ok"] is True
    assert result["outcome"] == codes.OUTCOME_OK
    assert result["api_date"] == dates.today_api()
    assert dates.today_kst() in result["date"]
    # Requirement 7: the stripped plate goes back to the page so the removal is *visible*.
    assert result["car_number"] == PLATE


def test_register_already_registered_is_not_a_failure(make_homey):
    """The most likely real outcome of a first use. Reporting it as an error teaches the user
    the app is broken on their very first try."""
    homey = make_homey(api=_RegisterApi(codes.OUTCOME_ALREADY_REGISTERED))
    result = asyncio.run(api.register_visitor(homey, body={
        "car_number": PLATE, "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))

    assert result["ok"] is True
    assert result["outcome"] == "already_registered"
    assert result["key"] == "already_registered"
    assert "이미 등록된 차량" in result["message"]


def test_register_failed_is_a_verdict_not_a_success(make_homey):
    homey = make_homey(api=_RegisterApi(codes.OUTCOME_FAILED))
    result = asyncio.run(api.register_visitor(homey, body={
        "car_number": PLATE, "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))

    assert result["ok"] is False
    assert result["key"] == "register_failed"
    assert result.get("uncertain") is not True


def test_register_uncertain_is_flagged_so_the_page_can_withhold_a_retry(make_homey):
    """The one outcome that must not invite a retry — a retry is what turns one uncertain write
    into two real registrations at a building."""
    session = _RegisterApi(error=RegisterUncertain("결과를 확인할 수 없습니다."))
    homey = make_homey(api=session)
    result = asyncio.run(api.register_visitor(homey, body={
        "car_number": PLATE, "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))

    assert result["ok"] is False
    assert result["uncertain"] is True
    assert result["key"] == "register_uncertain"
    assert "다시 시도하지 마세요" in result["message"]


def test_register_masks_the_plate_in_every_log_line(make_homey):
    homey = make_homey(api=_RegisterApi(codes.OUTCOME_OK))
    asyncio.run(api.register_visitor(homey, body={
        "car_number": PLATE, "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))
    logged = "\n".join(homey.app.logs)

    assert PLATE not in logged
    assert "12가****" in logged


def test_register_surfaces_a_bad_plate_as_a_user_verdict(make_homey):
    """`InvalidPlateError` is a `ValueError`, not an `IparkingError`, and it still has to come
    back with its own key and the site's example hint."""
    from iparking_lib.iparking.plate import InvalidPlateError

    homey = make_homey(api=_RegisterApi(error=InvalidPlateError("12가456")))
    result = asyncio.run(api.register_visitor(homey, body={
        "car_number": "12가456", "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))

    assert result["ok"] is False
    assert result["key"] == "bad_plate"
    assert "예시)" in result["error"]


def test_register_surfaces_not_permitted_with_its_own_key(make_homey):
    homey = make_homey(api=_RegisterApi(error=NotPermittedError("권한이 없습니다.")))
    result = asyncio.run(api.register_visitor(homey, body={
        "car_number": PLATE, "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
    }))

    assert result["ok"] is False
    assert result["key"] == "not_permitted"
    assert "관리사무소" in result["message"]


# --- GET /history and POST /cancel --------------------------------------------


class _HistoryApi(_RegisterApi):
    def __init__(self, rows=(), error=None):
        super().__init__()
        self._rows = list(rows)
        self._error = error
        self.history_calls = []
        self.cancelled = []

    async def history(self, **kwargs):
        self.history_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._rows

    async def cancel(self, invt_seq):
        self.cancelled.append(invt_seq)


def _row(car, date_, status, seq=3184551):
    from iparking_lib.iparking.client import HistoryRow

    return HistoryRow(invt_seq=seq, car_number=car, invitation_date=date_,
                      status=status, park_name="예시동 샘플아파트[출입통제A]")


def test_history_keys_each_row_on_is_active_not_on_presence(make_homey):
    """`DELETE /invitations/{seq}` does not remove a row — it flips `inot_status` to `CANCEL`
    and the row keeps its `invt_seq`. A table looking for the row to *disappear* would report a
    working 취소 as broken."""
    session = _HistoryApi(rows=[
        _row(PLATE, "20260805", "RESERVE"),
        _row("12가1237", "20260805", "CANCEL"),
    ])
    homey = make_homey(api=session)
    rows = asyncio.run(api.get_history(
        homey, query={"park_seq": str(PARK_SEQ), "stor_seq": str(STOR_SEQ)}
    ))["rows"]

    assert [row["is_active"] for row in rows] == [True, False]
    assert rows[1]["status"] == "CANCEL"
    assert rows[1]["invt_seq"] == 3184551


# --- the 오늘 등록 count, updated for free ---------------------------------------


class _CountingDevice:
    """A stand-in for a paired `VisitCarDevice_`, exposing only what `api.py` calls on it."""

    def __init__(self, error=None):
        self.notes = []
        self._error = error

    async def note_history(self, park_seq, stor_seq, rows):
        self.notes.append((park_seq, stor_seq, len(rows)))
        if self._error is not None:
            raise self._error


def test_a_history_read_feeds_the_devices_today_count(make_homey):
    """**Zero extra requests.** The rows are already in the handler's hand, so passing them on is
    what makes the 오늘 등록 tile correct the instant a user registers or cancels on the settings
    page — `form.js` re-reads the table after both actions, which is why neither of those handlers
    needs a refresh of its own."""
    session = _HistoryApi(rows=[_row(PLATE, "20260805", "RESERVE")])
    device = _CountingDevice()
    homey = make_homey(api=session, drivers=FakeDrivers(visitcar=[device]))

    result = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))

    assert result["ok"] is True
    assert device.notes == [(PARK_SEQ, STOR_SEQ, 1)]
    # One request, the one the page asked for. The count came out of its answer.
    assert len(session.history_calls) == 1


def test_a_device_that_objects_does_not_spoil_the_history_response(make_homey):
    """The count update is a courtesy hanging off a read the user asked for. A failure in it must
    never turn a successful history fetch into an error on the page."""
    session = _HistoryApi(rows=[_row(PLATE, "20260805", "RESERVE")])
    device = _CountingDevice(error=RuntimeError("tile write refused"))
    homey = make_homey(api=session, drivers=FakeDrivers(visitcar=[device]))

    result = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))

    assert result["ok"] is True
    assert [row["car_number"] for row in result["rows"]] == [PLATE]


def test_a_runtime_with_no_driver_registry_still_answers_the_history_read(make_homey):
    """`homey.drivers` is not in the fake unless a test asks for it, which is the branch this
    covers: `compat.devices` returns `[]` and the user pays a tile that is up to one poll stale,
    not an error."""
    session = _HistoryApi(rows=[_row(PLATE, "20260805", "RESERVE")])
    homey = make_homey(api=session)

    result = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))

    assert result["ok"] is True


def test_history_rows_come_out_newest_first(make_homey):
    """The vendor answers oldest-first, which buries the rows the user came to look at — the
    visits that have not happened yet — at the bottom of the table. Sorted in the handler, so
    the settings table, the widget and any later consumer all read the same order."""
    session = _HistoryApi(rows=[
        _row(PLATE, "20260601", "RESERVE"),
        _row("12가1237", "20260805", "RESERVE"),
        _row("12가1239", "20260713", "RESERVE"),
    ])
    homey = make_homey(api=session)

    rows = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))["rows"]

    assert [row["invitation_date"] for row in rows] == ["20260805", "20260713", "20260601"]


def test_history_breaks_a_same_date_tie_on_invt_seq_descending(make_homey):
    """Several registrations on one date is the ordinary case — a household registering three
    cars for the same visit. `invt_seq` is server-assigned and increasing, so the highest is
    the one registered last, and using it makes the within-date order deterministic instead of
    whatever the response happened to arrive in."""
    session = _HistoryApi(rows=[
        _row(PLATE, "20260805", "RESERVE", seq=3184551),
        _row("12가1237", "20260805", "RESERVE", seq=3184553),
        _row("12가1239", "20260805", "RESERVE", seq=3184552),
    ])
    homey = make_homey(api=session)

    rows = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))["rows"]

    assert [row["invt_seq"] for row in rows] == [3184553, 3184552, 3184551]
    assert [row["car_number"] for row in rows] == ["12가1237", "12가1239", PLATE]


def test_history_sorts_a_row_whose_date_will_not_parse_without_dropping_it(make_homey):
    """`invitation_date` is `yyyyMMdd`, so the sort is a string sort and needs no parsing —
    which is what keeps the malformed row `_human_date` already tolerates from raising here or
    from vanishing out of the table."""
    session = _HistoryApi(rows=[
        _row(PLATE, "20260805", "RESERVE"),
        _row("12가1237", "not-a-date", "RESERVE"),
    ])
    homey = make_homey(api=session)

    rows = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))["rows"]

    assert len(rows) == 2
    assert {row["car_number"] for row in rows} == {PLATE, "12가1237"}


def test_history_returns_plates_unmasked_because_the_user_owns_them(make_homey):
    """The one place a plate is not masked: 등록 내역 is the user reading their own
    registrations, and a masked table cannot be acted on."""
    homey = make_homey(api=_HistoryApi(rows=[_row(PLATE, "20260805", "RESERVE")]))
    rows = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))["rows"]

    assert rows[0]["car_number"] == PLATE
    assert rows[0]["invitation_date"] == "20260805"
    assert rows[0]["invitation_date_human"] == "2026-08-05 (수)"


def test_history_tolerates_a_row_whose_date_will_not_parse(make_homey):
    """Server data we do not get to reject. One malformed row must not empty the table."""
    homey = make_homey(api=_HistoryApi(rows=[_row(PLATE, "not-a-date", "RESERVE")]))
    rows = asyncio.run(api.get_history(
        homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}
    ))["rows"]

    assert rows[0]["invitation_date_human"] == "not-a-date"


def test_history_converts_the_pages_date_inputs_to_the_wire_format(make_homey):
    session = _HistoryApi()
    homey = make_homey(api=session)
    asyncio.run(api.get_history(homey, query={
        "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ,
        "start_date": "2026-05-08", "end_date": "2026-08-05",
    }))

    assert session.history_calls[0]["start_date"] == "20260508"
    assert session.history_calls[0]["end_date"] == "20260805"


def test_history_omits_the_window_when_the_page_sends_none(make_homey):
    """`None` means "use the client's default 3-month window"; an empty string would be sent
    verbatim and produce an empty table."""
    session = _HistoryApi()
    homey = make_homey(api=session)
    asyncio.run(api.get_history(homey, query={"park_seq": PARK_SEQ, "stor_seq": STOR_SEQ}))

    assert session.history_calls[0]["start_date"] is None
    assert session.history_calls[0]["end_date"] is None


def test_history_strips_the_plate_filter(make_homey):
    """A trailing space in the filter box would otherwise silently match nothing."""
    session = _HistoryApi()
    homey = make_homey(api=session)
    asyncio.run(api.get_history(homey, query={
        "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ, "car_number": " 12가 1236 ",
    }))
    assert session.history_calls[0]["car_number"] == PLATE


def test_history_reports_a_bad_window_as_a_date_verdict(make_homey):
    homey = make_homey(api=_HistoryApi())
    result = asyncio.run(api.get_history(homey, query={
        "park_seq": PARK_SEQ, "stor_seq": STOR_SEQ, "start_date": "05-08-26",
    }))

    assert result["ok"] is False
    assert result["key"] == "bad_date"


def test_cancel_requires_a_row(make_homey):
    homey = make_homey(api=_HistoryApi())
    assert asyncio.run(api.cancel_visitor(homey, body={}))["ok"] is False


def test_cancel_passes_the_row_through_as_an_int(make_homey):
    session = _HistoryApi()
    homey = make_homey(api=session)
    result = asyncio.run(api.cancel_visitor(homey, body={"invt_seq": "3184551"}))

    assert result == {"ok": True, "invt_seq": 3184551}
    assert session.cancelled == [3184551]


# --- GET /lots and GET /check-connection --------------------------------------


class _LotsApi(_RegisterApi):
    def __init__(self, lots=(), error=None):
        super().__init__()
        self._lots = list(lots)
        self._error = error
        self.stale_calls = []

    @property
    def auth_gen(self):
        return 7

    async def login_if_stale(self, gen):
        self.stale_calls.append(gen)

    async def enumerate_lots(self):
        if self._error is not None:
            raise self._error
        return self._lots


def _lot(can_register=True, stor_seq=STOR_SEQ):
    from iparking_lib.iparking.client import Lot

    return Lot(lot_id="1160009001", park_seq=PARK_SEQ, park_name="예시동 샘플아파트[출입통제A]",
               stor_seq=stor_seq, can_register=can_register)


def test_lots_reports_permission_per_lot(make_homey):
    """An account can hold several stores with the permission set differently on each, and the
    lot selector is where that difference has to be visible."""
    homey = make_homey(api=_LotsApi(lots=[_lot(True), _lot(False, 100002)]))
    result = asyncio.run(api.get_lots(homey))

    assert [lot["can_register"] for lot in result["lots"]] == [True, False]
    assert result["lots"][0]["lot_id"] == "1160009001"


def test_check_connection_needs_a_saved_account(make_homey):
    result = asyncio.run(api.check_connection(make_homey()))
    assert result == {"ok": False, "configured": False, "key": "need_credentials",
                     "error": "저장된 계정이 없습니다."}


def test_check_connection_forces_a_login_and_a_real_read(make_homey):
    """Both steps, or this handler checks nothing: `shared_api` returns the live object
    untouched once anything has logged in, so reading fields already in memory would answer
    `ok` unconditionally."""
    session = _LotsApi(lots=[_lot()])
    homey = make_homey(
        api=session,
        settings={SETTING_USERNAME: "iparking-dev", SETTING_PASSWORD: "pw"},
    )
    result = asyncio.run(api.check_connection(homey))

    assert result["ok"] is True
    assert result["lots"] == 1
    # Handed the *current* generation, which makes the "someone logged in for us" shortcut
    # unreachable while still letting it skip a simultaneous login.
    assert session.stale_calls == [7]


def test_check_connection_fails_when_the_read_fails(make_homey):
    """What makes an unreachable server look different from an account with no lots."""
    session = _LotsApi(error=IparkingApiError("서버 오류", "1002"))
    homey = make_homey(
        api=session,
        settings={SETTING_USERNAME: "iparking-dev", SETTING_PASSWORD: "pw"},
    )
    result = asyncio.run(api.check_connection(homey))

    assert result["ok"] is False
    assert result["configured"] is True
    assert result["code"] == "1002"


# --- _fail --------------------------------------------------------------------


def test_fail_renders_the_key_in_the_reported_language(make_homey):
    homey = make_homey(language="en")
    asyncio.run(api.set_language(homey, body={"language": "en"}))
    result = asyncio.run(api._fail(homey, NotPermittedError("권한이 없습니다.")))

    assert result["key"] == "not_permitted"
    assert "building office" in result["message"]
    # The raised sentence is kept too: it is often more specific than the locale template.
    assert result["error"] == "권한이 없습니다."


def test_fail_fills_the_locale_placeholders(make_homey):
    """`date_too_far` needs `{days}` and `error.unknown` needs `{code}`. An unfilled template
    would render the braces to the user."""
    homey = make_homey()
    far = asyncio.run(api._fail(homey, DateTooFarError("너무 멉니다", 80)))
    unknown = asyncio.run(api._fail(homey, IparkingApiError("모르는 코드", "99999")))

    assert "80" in far["message"]
    assert "{" not in far["message"]
    assert "99999" in unknown["message"]


def test_fail_keeps_a_key_less_exception_readable(make_homey):
    homey = make_homey()
    result = asyncio.run(api._fail(homey, RuntimeError("boom")))

    assert result == {"ok": False, "error": "boom"}
