"""`IparkingApi` plumbing: transport policy, auth retry, and store enumeration.

Covers acceptance criteria 4, 5, 11 and 12. The register path has its own file
(`test_register_path.py`) because its invariants are about *not* doing things, and mixing
those assertions in with ordinary plumbing is how one of them gets deleted as noise.

Nothing here contacts a real host. The seam is `conftest.StubHandler`, which replaces the
socket while leaving `StrictRedirectHandler` and the whole error-conversion chain in place —
so a stubbed 301 exercises the real refusal and a route raising `URLError` produces a real
`NetworkError`. No event-loop plugin is installed, so each test drives `asyncio.run` itself.
"""

from __future__ import annotations

import asyncio
import urllib.error

import pytest
from conftest import (
    HISTORY_URL,
    LOT_ID,
    LOTS_URL,
    MEMBERS_ROOT,
    OAUTH_URL,
    PARK_NAME,
    PARK_SEQ,
    STOR_SEQ,
    envelope,
    history_ok,
    login_ok,
    lots_ok,
)

from iparking_lib.const import (
    LOGIN_ATTEMPTS,
    MEMBERS_HOST,
    OAUTH_HOST,
    READ_ATTEMPTS,
    RECOVERY_ATTEMPTS,
)
from iparking_lib.iparking.client import (
    IparkingApi,
    IparkingApiError,
    IparkingAuthError,
    IparkingError,
    NeedCredentialsError,
    NotPermittedError,
)
from iparking_lib.iparking.transport import (
    BodyRedirect,
    ConnectionLost,
    InsecureRedirect,
    NetworkError,
)


def reset() -> ConnectionResetError:
    """One instance of the members host's measured fault (~30 % of plain-HTTP connections)."""
    return ConnectionResetError(54, "Connection reset by peer")


def header(headers: dict, name: str) -> str | None:
    """Case-insensitive lookup — urllib title-cases header names on the way out."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


# --- criterion 4: the final scheme, per host, by name -------------------------


def test_oauth_is_addressed_over_https_and_answers_over_https():
    """The password host. Asserted **by name**, not derived from `const.SCHEMES`.

    A test that read the table would happily agree with a table someone had edited the
    wrong way, which is the entire failure this assertion exists to catch.
    """
    api = IparkingApi(username="u", password="p", log=lambda m: None)
    url = api._oauth_url()

    assert url == f"https://{OAUTH_HOST}/api/oauth/store/authorize"
    assert url.startswith("https://")


def test_members_is_addressed_over_http_by_name():
    """The API host, over cleartext deliberately: it 301s every https request down.

    This is the assertion most likely to look like a bug to a future reader, so it says why
    in place rather than in a doc they will not open.
    """
    api = IparkingApi(username="u", password="p", log=lambda m: None)

    assert api._members_url("/invitations") == f"http://{MEMBERS_HOST}/api/members/invitations"


def test_every_request_ends_on_the_scheme_its_host_requires(make_api):
    """The **final**, post-redirect scheme — the only value that can honestly answer
    "did this use TLS?". Requested schemes cannot."""
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        LOTS_URL: lots_ok(),
        HISTORY_URL: history_ok(),
    })

    asyncio.run(api.enumerate_lots())
    asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    for _method, url, _headers, _body in stub.calls:
        scheme = url.split("://", 1)[0]
        if OAUTH_HOST in url:
            assert scheme == "https", f"oauth must be https, got {url}"
        elif MEMBERS_HOST in url:
            assert scheme == "http", f"members must be http, got {url}"
        else:
            raise AssertionError(f"unexpected host in {url}")


def test_the_domain_field_supplies_the_host_but_never_the_scheme(make_api):
    """`operation_company[0].domain` literally reads `http://members.iparking.co.kr`.

    Taking the scheme from it would let the vendor's server choose our transport — and would
    also be read as licence to use http for the oauth host, which carries the password. The
    host is taken; the scheme comes from `const.SCHEMES`.
    """
    body = login_ok()
    # Even if the vendor started advertising https here, members stays http by policy: the
    # scheme is decided by measurement, not by the server's own description of itself.
    body["auth_data"]["operation_company"] = [{"domain": f"https://{MEMBERS_HOST}"}]
    api, _stub, _ = make_api({OAUTH_URL: body, LOTS_URL: lots_ok()})

    asyncio.run(api.login())

    assert api.api_host == MEMBERS_HOST
    assert api._members_url("/x").startswith("http://")


def test_a_bare_hostname_in_domain_still_parses(make_api):
    """`urlsplit` needs the `//`; a bare host would otherwise land in `path` and be lost."""
    body = login_ok()
    body["auth_data"]["operation_company"] = [{"domain": MEMBERS_HOST}]
    api, _stub, _ = make_api({OAUTH_URL: body})

    asyncio.run(api.login())

    assert api.api_host == MEMBERS_HOST


def test_a_missing_domain_falls_back_to_the_known_host(make_api):
    body = login_ok()
    body["auth_data"].pop("operation_company")
    api, _stub, _ = make_api({OAUTH_URL: body})

    asyncio.run(api.login())

    assert api.api_host == MEMBERS_HOST


def test_https_to_http_301_on_the_login_request_raises_insecure_redirect(make_api):
    """The one request carrying the password must never be followed down to cleartext."""
    downgraded = f"http://{OAUTH_HOST}/api/oauth/store/authorize"
    api, stub, _ = make_api({
        OAUTH_URL: (301, {"location": downgraded}, b""),
        downgraded: login_ok(),
    })

    with pytest.raises(InsecureRedirect):
        asyncio.run(api.login())

    # The assertion that matters is the negative one: the cleartext URL was never requested.
    assert stub.urls() == [OAUTH_URL]
    assert api.access_token == ""


def test_any_3xx_on_a_body_carrying_request_is_refused_even_same_scheme(make_api):
    """Predicate 2. `urllib` would retry a 301'd POST as a **bodyless GET**, and this API's
    entire payload is the body — so a followed redirect returns plausible garbage instead of
    an error. Refused even though nothing is downgraded here."""
    elsewhere = f"{MEMBERS_ROOT}/invitations/list-moved"
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        HISTORY_URL: (301, {"location": elsewhere}, b""),
        elsewhere: history_ok(),
    })

    with pytest.raises(BodyRedirect):
        asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert elsewhere not in stub.urls()


def test_a_transport_level_urlerror_becomes_a_network_error(make_api):
    """The seam replaces the socket, not the error handling: this goes through the real
    `except URLError` conversion rather than a mock of it."""
    api, _stub, _ = make_api({OAUTH_URL: urllib.error.URLError("no route to host")})

    with pytest.raises(NetworkError):
        asyncio.run(api.login())


def test_every_request_carries_a_real_timeout(make_api):
    """urllib's default is `None` — block forever. A hung socket is what strands the
    register path, so the timeout is not optional anywhere."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), LOTS_URL: lots_ok()})

    asyncio.run(api.enumerate_lots())

    assert stub.timeouts, "no requests were made"
    assert all(t == 15.0 for t in stub.timeouts), stub.timeouts


# --- criterion 5: headers and the single re-login -----------------------------


def test_authorization_header_has_no_bearer_prefix(make_api):
    """The bundle sends the raw UUID. The server rejects the prefixed form."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(token="tok-uuid-1"), LOTS_URL: lots_ok()})

    asyncio.run(api.enumerate_lots())

    sent = [header(h, "authorization") for h in stub.headers_for(LOTS_URL)]
    assert sent == ["tok-uuid-1"]
    for value in sent:
        assert not value.lower().startswith("bearer")


def test_login_sends_no_authorization_header_at_all(make_api):
    """Not an empty one: an empty credential is a distinct thing to send, and the bundle
    sends nothing."""
    api, stub, _ = make_api({OAUTH_URL: login_ok()})

    asyncio.run(api.login())

    assert header(stub.headers_for(OAUTH_URL)[0], "authorization") is None


def test_headers_are_exactly_the_three_the_api_wants(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), LOTS_URL: lots_ok()})

    asyncio.run(api.enumerate_lots())
    headers = stub.headers_for(LOTS_URL)[0]

    assert header(headers, "content-type") == "application/json;charset=UTF-8"
    assert header(headers, "version") == "2.0.0"
    assert header(headers, "authorization") == "tok-uuid-1"


@pytest.mark.parametrize("code", ["2031", "2041", "1009"])
def test_an_expired_token_triggers_exactly_one_relogin_and_one_retry(make_api, code):
    """One re-login, one retry, then the answer — not a loop.

    Scripted as [expired, ok]: two calls to the endpoint and two logins total (the initial
    one plus the retry's). A third request would mean the retry budget is not bounded.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        LOTS_URL: [envelope(code, "token expired"), lots_ok()],
    })

    lots = asyncio.run(api.enumerate_lots())

    assert stub.count(LOTS_URL) == 2, "one retry, no more"
    assert stub.count(OAUTH_URL) == 2, "one re-login, no more"
    assert len(lots) == 1


def test_a_second_expiry_is_raised_rather_than_retried_again(make_api):
    """Otherwise a server stuck on `2031` becomes an infinite login loop against a host
    that can rate-limit us."""
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        LOTS_URL: envelope("2031", "token expired"),
    })

    with pytest.raises(IparkingApiError) as caught:
        asyncio.run(api.enumerate_lots())

    assert caught.value.code == "2031"
    assert stub.count(LOTS_URL) == 2
    assert stub.count(OAUTH_URL) == 2


def test_a_non_auth_error_code_is_not_retried_at_all(make_api):
    """`12105 notAllowed` is a verdict a fresh token cannot change."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), LOTS_URL: envelope("12105", "not allowed")})

    with pytest.raises(IparkingApiError) as caught:
        asyncio.run(api.enumerate_lots())

    assert caught.value.code == "12105"
    assert caught.value.key == "error.not_allowed"
    assert stub.count(LOTS_URL) == 1


def test_concurrent_callers_seeing_the_same_expiry_produce_one_login(make_api):
    """`login_if_stale` deduping, and it is not a micro-optimisation.

    The token is per-account: a second login **invalidates the first one's token**, which is
    the very failure the retry exists to fix. Two callers racing on one expiry must
    therefore produce one login, not two.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        LOTS_URL: [envelope("2031"), envelope("2031"), lots_ok(), lots_ok()],
    })

    async def both():
        await api.login()
        return await asyncio.gather(
            api.parking_lots(STOR_SEQ), api.parking_lots(STOR_SEQ)
        )

    asyncio.run(both())

    # One initial login plus exactly one re-login shared by both callers.
    assert stub.count(OAUTH_URL) == 2, stub.urls()


def test_auth_gen_advances_only_after_a_login_fully_parses(make_api):
    """A half-parsed login must not make a waiting `login_if_stale` believe a session
    exists — it would then skip the login that is actually needed."""
    api, _stub, _ = make_api({OAUTH_URL: envelope("0000", "ok", auth_data={})})

    assert api.auth_gen == 0
    with pytest.raises(IparkingAuthError):
        asyncio.run(api.login())
    assert api.auth_gen == 0


def test_login_accepts_result_data_as_well_as_auth_data(make_api):
    """Some deployments return `resultData`. The difference is invisible until production."""
    body = login_ok()
    body["resultData"] = body.pop("auth_data")
    api, _stub, _ = make_api({OAUTH_URL: body})

    asyncio.run(api.login())

    assert api.logged_in
    assert api.stor_seq == STOR_SEQ


def test_a_wrong_password_is_an_auth_error_not_a_retryable_one(make_api):
    """`2002` is `loginError`. Retrying repeats it, so it must not look retryable."""
    api, _stub, _ = make_api({OAUTH_URL: envelope("2002", "아이디 또는 비밀번호를 확인하세요")})

    with pytest.raises(IparkingAuthError):
        asyncio.run(api.login())


def test_missing_credentials_are_refused_before_any_request(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok()}, username="", password="")

    with pytest.raises(NeedCredentialsError):
        asyncio.run(api.login())

    assert stub.calls == []


def test_an_unparseable_body_becomes_a_vendor_verdict_not_a_traceback(make_api):
    api, _stub, _ = make_api({OAUTH_URL: (200, {}, b"<html>maintenance</html>")})

    with pytest.raises(IparkingApiError):
        asyncio.run(api.login())


def test_a_500_still_has_its_envelope_read(make_api):
    """4xx/5xx are returned rather than raised precisely because this API puts its verdict
    inside the body, and an error status can still carry it."""
    api, _stub, _ = make_api({OAUTH_URL: (500, {}, envelope("1002", "dbError"))})

    with pytest.raises(IparkingApiError) as caught:
        asyncio.run(api.login())

    assert caught.value.code == "1002"


# --- criterion 12 / the never-logged rule ------------------------------------


def test_the_log_never_contains_the_password_token_or_address(make_api):
    """Every value in the never-log list, checked on a real login + read + write-shaped path.

    `memb_name` is a home address; the token can register vehicles at a building and travels
    in cleartext; plates are masked because diagnostic output gets pasted into issues.
    """
    api, _stub, logs = make_api(
        {
            OAUTH_URL: login_ok(memb_name="999동9999호", token="secret-token-uuid"),
            LOTS_URL: lots_ok(),
            HISTORY_URL: history_ok((("12가3456", "20260805", "RESERVE"),)),
        },
        password="hunter2-secret",
    )

    asyncio.run(api.enumerate_lots())
    asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))
    blob = "\n".join(logs)

    assert "hunter2-secret" not in blob
    assert "secret-token-uuid" not in blob
    assert "999동9999호" not in blob
    assert "12가3456" not in blob


def test_the_token_length_is_logged_but_never_its_value(make_api):
    """Presence and length are the diagnostic value; the string itself is a live credential."""
    api, _stub, logs = make_api({OAUTH_URL: login_ok(token="abcdefghij")})

    asyncio.run(api.login())
    blob = "\n".join(logs)

    assert "token len=10" in blob
    assert "abcdefghij" not in blob


def test_request_bodies_are_never_logged(make_api):
    """The body is the encrypted payload. Logging it would defeat every other rule here."""
    api, stub, logs = make_api({OAUTH_URL: login_ok(), LOTS_URL: lots_ok()})

    asyncio.run(api.enumerate_lots())
    sent = [b for b in stub.bodies_for(OAUTH_URL) if b]
    blob = "\n".join(logs)

    assert sent, "login sent no body"
    for body in sent:
        assert body.decode() not in blob


def test_logout_clears_the_session_and_refuses_further_traffic(make_api):
    """The kill flag. This object is the only seam the app has on a running device, since
    `homey` exposes no device registry in this Python surface."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), LOTS_URL: lots_ok()})
    asyncio.run(api.login())
    before = len(stub.calls)

    api.logout()

    assert api.access_token == ""
    assert api.memb_name == ""
    assert api.auth_entries == []
    with pytest.raises(IparkingError) as caught:
        asyncio.run(api.parking_lots(STOR_SEQ))
    assert "다시 로그인" in str(caught.value)
    assert len(stub.calls) == before, "a disabled session must not reach the network"


def test_the_token_is_an_attribute_of_this_object_and_nothing_else(make_api):
    """Criterion 12's other half — the grep over `api.py`/`app.py` — belongs to those files.

    What this layer can guarantee is that the client offers no persistence hook at all: no
    setting name for the token exists in `const.py`, so there is nothing for a later handler
    to helpfully wire up.
    """
    from iparking_lib import const

    api, _stub, _ = make_api({OAUTH_URL: login_ok(token="tok")})
    asyncio.run(api.login())

    assert api.access_token == "tok"
    token_settings = [
        name for name in dir(const)
        if name.startswith(("SETTING_", "STORE_")) and "token" in getattr(const, name).lower()
    ]
    assert token_settings == [], token_settings


# --- criterion 11: the authorization list ------------------------------------


def test_an_empty_authorization_list_is_refused_with_a_specific_message(make_api):
    """Not a crash and not an empty device list: an account with no store is a real
    configuration the building office has to fix, so it gets its own sentence."""
    api, _stub, _ = make_api({OAUTH_URL: login_ok(stores=())})

    with pytest.raises(IparkingApiError) as caught:
        asyncio.run(api.enumerate_lots())

    assert caught.value.key == "no_stores"
    assert "관리사무소" in str(caught.value)
    assert api.stor_seq is None


def test_lots_are_enumerated_across_every_authorization_entry(make_api):
    """Multi-store accounts generalize with no special-casing — one request per entry.

    Only a 1×1 account is testable live, so this is the synthetic coverage the plan's §9.5
    accepted. Collapsing to `[0]` would silently drop every other store.
    """
    other = 999111
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(stores=((STOR_SEQ, "Y"), (other, "Y"))),
        LOTS_URL: lots_ok(),
        f"{MEMBERS_ROOT}/parkinglot/list/{other}": lots_ok([
            {"park_seq": 7001, "lot_id": "1160007001", "park_name": "다른 주차장"},
        ]),
    })

    lots = asyncio.run(api.enumerate_lots())

    assert [lot.stor_seq for lot in lots] == [STOR_SEQ, other]
    assert [lot.lot_id for lot in lots] == [LOT_ID, "1160007001"]
    assert stub.count(LOTS_URL) == 1
    assert stub.count(f"{MEMBERS_ROOT}/parkinglot/list/{other}") == 1


def test_multiple_lots_within_one_store_are_all_enumerated(make_api):
    """`parkinglot/list` returns an array; a store can hold more than one lot."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        LOTS_URL: lots_ok([
            {"park_seq": PARK_SEQ, "lot_id": LOT_ID, "park_name": PARK_NAME},
            {"park_seq": 6059, "lot_id": "1160006059", "park_name": "출입통제B"},
        ]),
    })

    lots = asyncio.run(api.enumerate_lots())

    assert [lot.park_seq for lot in lots] == [PARK_SEQ, 6059]
    assert [lot.park_name for lot in lots] == [PARK_NAME, "출입통제B"]


def test_a_store_that_may_not_register_still_pairs(make_api):
    """The 주차장명 sensor is useful without the write permission, so such a store pairs and
    only the write is gated. Refusing to pair would also mean re-pairing later, after the
    building office grants it."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(stores=((STOR_SEQ, "N"),)),
        LOTS_URL: lots_ok(),
    })

    lots = asyncio.run(api.enumerate_lots())

    assert len(lots) == 1
    assert lots[0].park_name == PARK_NAME
    assert lots[0].can_register is False
    assert api.can_register is False


def test_register_is_refused_when_the_account_may_not_register(make_api):
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(stores=((STOR_SEQ, "N"),)),
    })

    with pytest.raises(NotPermittedError) as caught:
        asyncio.run(api.register(car_number="12가4567", park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert "권한이 없습니다" in str(caught.value)
    # The gate is before the write, so nothing was sent to the register endpoint.
    assert f"{MEMBERS_ROOT}/invitations" not in stub.urls()


def test_per_store_permission_is_read_per_store_not_from_the_first_entry(make_api):
    """A mixed account must gate each store on its own flag. Collapsing to `[0]` would let
    a permitted first store authorise a write against a forbidden second one."""
    forbidden = 999222
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(stores=((STOR_SEQ, "Y"), (forbidden, "N"))),
    })
    asyncio.run(api.login())

    assert api._entry_for(STOR_SEQ).can_register is True
    assert api._entry_for(forbidden).can_register is False
    with pytest.raises(NotPermittedError):
        asyncio.run(api.register(car_number="12가4567", park_seq=PARK_SEQ, stor_seq=forbidden))


def test_an_unknown_store_is_a_clean_error(make_api):
    api, _stub, _ = make_api({OAUTH_URL: login_ok()})
    asyncio.run(api.login())

    with pytest.raises(IparkingApiError) as caught:
        api._entry_for(4242)

    assert caught.value.key == "error.not_find_store"


# --- reads -------------------------------------------------------------------


def test_history_rows_are_parsed_with_both_sides_normalized(make_api):
    """Server plates go through `strip_plate`, never `normalize_plate`.

    A `car_number` the vendor accepts but our validator does not must not turn a status
    lookup into an exception — which is what makes the comparison in the register path's
    recovery safe to run against arbitrary server data.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        HISTORY_URL: history_ok((
            ("12가 3456", "20260805", "RESERVE"),
            ("12가4567", "20260806", "cancel"),
        )),
    })

    rows = asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert [r.car_number for r in rows] == ["12가3456", "12가4567"]
    assert [r.status for r in rows] == ["RESERVE", "CANCEL"]
    assert rows[0].is_active is True
    assert rows[1].is_active is False


def test_history_requests_the_whole_window_in_one_call(make_api):
    """`page_size` is honoured verbatim (verified: 100 returned all 43 rows), so pagination
    is a display concern rather than a fetch concern."""
    from iparking_lib.iparking import crypto

    api, stub, _ = make_api({OAUTH_URL: login_ok(), HISTORY_URL: history_ok()})

    asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))
    sent = crypto.decode_body(stub.bodies_for(HISTORY_URL)[0])

    assert sent["page_size"] == 100
    assert sent["current_page"] == 1
    assert sent["storSeq"] == STOR_SEQ
    assert sent["parkSeq"] == PARK_SEQ


def test_an_unparseable_history_row_is_skipped_rather_than_fatal(make_api):
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        HISTORY_URL: history_ok((
            "not-a-dict",
            {"car_number": "12가4567", "invitation_date": "20260805",
             "inot_status": "RESERVE", "invt_seq": 1},
        )),
    })

    rows = asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert [r.car_number for r in rows] == ["12가4567"]


def test_aggregate_counts_stay_empty_because_the_server_sends_empty(make_api):
    """`resultData.total` was `[]` even on a 43-record range — verified twice. It is
    optional display metadata, never a status aggregate, and there is deliberately no
    counting logic anywhere in this app."""
    assert IparkingApi.aggregate_counts(history_ok((("12가4567", "20260805", "IN"),))) == []
    assert IparkingApi.aggregate_counts(
        history_ok((), total=({"inot_status": "IN", "cnt": 2},))
    ) == [{"inot_status": "IN", "cnt": 2}]


def test_cancel_issues_a_delete_with_no_body(make_api):
    """취소 is production code because the settings table needs it anyway — which is what let
    the probe's cleanup path be shipping code rather than a throwaway script."""
    url = f"{MEMBERS_ROOT}/invitations/3184553"
    api, stub, _ = make_api({OAUTH_URL: login_ok(), url: envelope("0000", "성공")})

    asyncio.run(api.cancel(3184553))

    assert stub.urls("DELETE") == [url]
    assert stub.bodies_for(url) == [None]


# --- retrying the reset-prone host, per endpoint -----------------------------
#
# `members.iparking.co.kr` resets ~30 % of plain-HTTP connections (measured 2026-08-04: 20
# identical read-only requests → 14 answers, 6 dead sockets). Each test below names the
# *endpoint*, never the method, because this API serves reads over POST and a method-shaped
# rule would retry `POST /invitations`. That endpoint's own test lives in
# `test_register_path.py`, where the negative assertions are.


def test_a_reset_on_the_history_read_retries_and_then_succeeds(make_api):
    """`POST /invitations/list` — a POST that is a read, and therefore retryable."""
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        HISTORY_URL: [reset(), history_ok((("12가4567", "20260805", "RESERVE"),))],
    })

    rows = asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert [r.car_number for r in rows] == ["12가4567"], "the retry produced the real answer"
    assert stub.count(HISTORY_URL) == 2
    assert len(stub.backoffs) == 1


def test_a_reset_on_the_lot_list_read_retries_and_then_succeeds(make_api):
    """`POST /parkinglot/list/{seq}` — the pairing path, the most reset-exposed in the app."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), LOTS_URL: [reset(), lots_ok()]})

    lots = asyncio.run(api.enumerate_lots())

    assert [lot.lot_id for lot in lots] == [LOT_ID]
    assert stub.count(LOTS_URL) == 2


def test_a_reset_on_the_detail_read_retries_and_then_succeeds(make_api):
    """`GET /invitations/{seq}`."""
    url = f"{MEMBERS_ROOT}/invitations/3184553"
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        url: [reset(), envelope("0000", resultData={"inot_status": "RESERVE"})],
    })

    assert asyncio.run(api.detail(3184553))["inot_status"] == "RESERVE"
    assert stub.count(url) == 2


def test_a_reset_on_the_login_retries_and_then_succeeds(make_api):
    """The oauth login. A retry just mints another token, so there is nothing to double.

    Also the clearest proof that the policy is per endpoint: this is a POST carrying a body
    that *is* retryable, sitting next to a POST carrying a body that is not.
    """
    api, stub, _ = make_api({OAUTH_URL: [reset(), login_ok()], LOTS_URL: lots_ok()})

    asyncio.run(api.login())

    assert api.logged_in
    assert stub.count(OAUTH_URL) == 2


def test_a_reset_on_the_cancel_retries_because_re_cancelling_is_a_no_op(make_api):
    """`DELETE /invitations/{seq}` may retry, and this test is here to stop that being
    "fixed" into a non-retry by someone reasoning "it is a write, so zero retries".

    The reason is measured, not stylistic: deleting an already-cancelled row returns
    `13001 alreadyDeleted` (verified live 2026-08-04), a no-op on a row that is already
    `CANCEL`. So both readings of a reset — "it never arrived" and "it arrived and the reply
    was lost" — leave the *same* end state. `POST /invitations` has no such property, and
    that asymmetry is the whole reason retry policy is decided per endpoint.
    """
    url = f"{MEMBERS_ROOT}/invitations/3184553"
    api, stub, _ = make_api({OAUTH_URL: login_ok(), url: [reset(), envelope("0000", "성공")]})

    asyncio.run(api.cancel(3184553))

    assert stub.count(url) == 2


def test_a_read_gives_up_after_the_cap_and_raises_connection_lost(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), HISTORY_URL: reset()})

    with pytest.raises(ConnectionLost):
        asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert stub.count(HISTORY_URL) == READ_ATTEMPTS
    assert READ_ATTEMPTS >= 4, "at P(fail)=0.3, four attempts is what buys 0.8%"


def test_a_read_does_not_retry_a_timeout(make_api):
    """The boundary, at the client layer: `attempts=4` still means one send on a timeout.

    A read timing out is harmless to re-send, so this test is not protecting the read — it is
    protecting the *rule*, because the same `attempts` plumbing carries the register POST.
    """
    api, stub, _ = make_api({OAUTH_URL: login_ok(), HISTORY_URL: TimeoutError("timed out")})

    with pytest.raises(NetworkError) as caught:
        asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    assert not isinstance(caught.value, ConnectionLost)
    assert stub.count(HISTORY_URL) == 1, "a timeout may still be in flight; never re-sent"


def test_the_attempt_counts_are_ordered_by_consequence():
    """Not a tautology: it pins the *ordering* the numbers exist to express.

    The recovery re-query must try harder than an ordinary read, because its failure is what
    converts a knowable registration outcome into a bare error for the user.
    """
    assert RECOVERY_ATTEMPTS > READ_ATTEMPTS >= 4
    assert LOGIN_ATTEMPTS >= 4


def test_a_reset_is_logged_with_its_type_on_every_attempt(make_api):
    """The app logged three request lines and then nothing. That was its own defect."""
    api, _stub, logs = make_api({OAUTH_URL: login_ok(), HISTORY_URL: reset()})

    with pytest.raises(ConnectionLost):
        asyncio.run(api.history(park_seq=PARK_SEQ, stor_seq=STOR_SEQ))

    joined = "\n".join(logs)
    assert "ConnectionResetError" in joined
    assert f"gave up after {READ_ATTEMPTS} attempt(s)" in joined


def test_detail_is_a_get_with_no_body(make_api):
    url = f"{MEMBERS_ROOT}/invitations/3184553"
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        url: envelope("0000", resultData={"car_number": "12가4567", "inot_status": "RESERVE"}),
    })

    detail = asyncio.run(api.detail(3184553))

    assert stub.urls("GET") == [url]
    assert detail["inot_status"] == "RESERVE"


def test_a_body_is_encrypted_with_the_double_base64_envelope(make_api):
    """The wire body must be the envelope pinned by `test_crypto.py`, not raw JSON."""
    from iparking_lib.iparking import crypto

    api, stub, _ = make_api({OAUTH_URL: login_ok()}, username="me", password="pw")

    asyncio.run(api.login())
    body = stub.bodies_for(OAUTH_URL)[0]

    assert b"{" not in body, "the body must not be plaintext JSON"
    assert crypto.decode_body(body) == {
        "client_id": "me", "client_pwd": "pw", "client_os_type": "WEB",
    }
