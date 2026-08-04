"""Redirect-policy and fault-classification tests for `iparking_lib.iparking.transport`.

These never touch a real host. `StubHandler` replaces the socket via urllib's own
substitution rule (see its docstring) while leaving `StrictRedirectHandler` installed, so
a stubbed 301 exercises the real refusal code rather than a mock of it.

The assertions that matter are the *negative* ones: a refused redirect must leave the
target URL unrequested, and a timeout must **not** be retried however many attempts were
allowed. "It raised" alone would still pass if the request had already gone out, and "it
retried" alone would still pass if it were retrying the wrong fault.
"""

from __future__ import annotations

import email.message
import errno
import http.client
import io
import urllib.error
import urllib.request
import urllib.response

import pytest

from iparking_lib.iparking import transport as transport_module
from iparking_lib.iparking.transport import (
    DEFAULT_TIMEOUT_S,
    RETRY_BACKOFF_MAX_S,
    RETRY_JITTER,
    BodyRedirect,
    ConnectionLost,
    InsecureRedirect,
    NetworkError,
    RedirectRefused,
    Response,
    Transport,
)

OAUTH = "https://oauth.parkingcloud.co.kr/api/oauth/store/authorize"
MEMBERS_HTTP = "http://members.iparking.co.kr/api/members/parkinglot/list/100001"
MEMBERS_HTTPS = "https://members.iparking.co.kr/api/members/parkinglot/list/100001"


class _StubResponse(urllib.response.addinfourl):
    """`addinfourl` plus the `.msg` attribute `HTTPErrorProcessor` reads off a response.

    Verified empirically on CPython 3.14: `addinfourl` supplies `.code`, `.status`,
    `.geturl()` and `.info()`, but no `.msg`.
    """

    def __init__(self, status: int, headers: dict, body: bytes, url: str) -> None:
        message = email.message.Message()
        for key, value in headers.items():
            message[key] = value
        super().__init__(io.BytesIO(body), message, url, status)
        self.msg = "Stub"


class StubHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Serves canned responses instead of opening a socket.

    Subclassing both `HTTPHandler` and `HTTPSHandler` is what makes `build_opener`
    substitute this for the two default transport handlers. `handler_order = 100` (below
    the default 500) is what makes it win the `https_open` chain against the real
    `HTTPSHandler` that `Transport` installs unconditionally — without it the tie is
    broken by insertion order and the real one would try to dial out.
    """

    handler_order = 100

    def __init__(self, routes: dict) -> None:
        super().__init__()
        # A route is one outcome, or a **list** consumed in order with the last entry reused.
        # The list form is what lets a retry test script "fail, then answer" — the only shape
        # that can tell a retry that worked from one that never happened.
        self.routes = {url: list(v) if isinstance(v, list) else [v]
                       for url, v in routes.items()}
        self.requested: list[str] = []
        self.timeouts: list[float | None] = []
        #: Every backoff the transport asked for, in order. Wired via `Transport(sleep=...)`,
        #: so a retry test asserts the wait happened without the suite really waiting.
        self.backoffs: list[float] = []

    def http_open(self, req):
        self.requested.append(req.full_url)
        self.timeouts.append(req.timeout)
        script = self.routes[req.full_url]
        outcome = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            # A route that builds its own response object — used for the truncated body.
            return outcome(req.full_url)
        status, headers, body = outcome
        return _StubResponse(status, headers, body, req.full_url)

    https_open = http_open


class _TruncatedResponse(_StubResponse):
    """A response whose body dies halfway through being read.

    This is where `IncompleteRead` really comes from, and the distinction matters: urllib
    hands back a perfectly good response object and the socket dies while `read()` consumes
    the body. A test that raised `IncompleteRead` from `http_open` instead would pass against
    a classifier that only covered the connect phase, and the real fault would still escape.
    """

    def read(self, *args, **kwargs):
        raise http.client.IncompleteRead(b"p" * 255, 100)


def truncated(url: str) -> _TruncatedResponse:
    return _TruncatedResponse(200, {}, b"", url)


def _transport(routes: dict):
    """`(transport, logs)`. Backoff is recorded on the stub, never really slept."""
    logs: list[str] = []
    stub = StubHandler(routes)
    transport = Transport(log=logs.append, handlers=[stub], sleep=stub.backoffs.append)
    return transport, logs


def _stub_of(transport: Transport) -> StubHandler:
    return next(h for h in transport._opener.handlers if isinstance(h, StubHandler))


# --- the stdlib behaviour predicate 2 exists to prevent ---------------------


def test_stdlib_turns_a_post_301_into_a_bodyless_get():
    """Pins the premise of predicate 2 against the *stdlib*, not against our code.

    If a future CPython starts preserving the body on a 301'd POST, this test fails and
    tells whoever is reading it that predicate 2's justification has changed — rather than
    leaving a rule in `transport.py` whose stated reason is quietly no longer true.
    Predicate 2 would still be defensible (an unannounced method change is not something to
    follow blind), but the docstring would need rewriting.
    """
    req = urllib.request.Request(MEMBERS_HTTP, data=b"encrypted-payload", method="POST")
    req.add_header("Content-Type", "application/json;charset=UTF-8")
    req.timeout = None
    headers = email.message.Message()
    headers["location"] = MEMBERS_HTTP + "/elsewhere"

    new = urllib.request.HTTPRedirectHandler().redirect_request(
        req, io.BytesIO(b""), 301, "Moved Permanently", headers, headers["location"]
    )

    assert new.data is None, "the body is dropped"
    assert new.get_method() == "GET", "the method is downgraded"
    assert "Content-Type" not in new.headers, "CONTENT_HEADERS are stripped"


# --- predicate 1: scheme ----------------------------------------------------


def test_https_to_http_301_raises_insecure_redirect():
    """The members host's real behaviour: a 301 down to cleartext must never be followed."""
    transport, _ = _transport({
        MEMBERS_HTTPS: (301, {"location": MEMBERS_HTTP}, b""),
        MEMBERS_HTTP: (200, {}, b'{"result":"0000"}'),
    })
    with pytest.raises(InsecureRedirect):
        transport.request("GET", MEMBERS_HTTPS)
    # The whole point: the cleartext URL was never requested.
    assert _stub_of(transport).requested == [MEMBERS_HTTPS]


def test_http_to_https_upgrade_is_allowed_and_logged():
    """An upgrade must NOT be refused — the vendor fixing their TLS must not break us."""
    transport, logs = _transport({
        MEMBERS_HTTP: (301, {"location": MEMBERS_HTTPS}, b""),
        MEMBERS_HTTPS: (200, {}, b'{"result":"0000"}'),
    })
    resp = transport.request("GET", MEMBERS_HTTP)
    assert resp.status == 200
    assert _stub_of(transport).requested == [MEMBERS_HTTP, MEMBERS_HTTPS]
    assert any("upgrade http -> https" in line for line in logs)


def test_downgrade_check_runs_before_the_body_check():
    """https->http on a body-carrying POST reports the downgrade, the sharper diagnosis."""
    transport, _ = _transport({
        MEMBERS_HTTPS: (301, {"location": MEMBERS_HTTP}, b""),
        MEMBERS_HTTP: (200, {}, b'{"result":"0000"}'),
    })
    with pytest.raises(InsecureRedirect):
        transport.request("POST", MEMBERS_HTTPS, body=b"encrypted")
    assert _stub_of(transport).requested == [MEMBERS_HTTPS]


# --- predicate 2: body ------------------------------------------------------


def test_same_scheme_301_on_post_with_body_raises_body_redirect():
    """The non-obvious predicate: no downgrade at all, and still refused.

    urllib would retry this as a bodyless GET (`redirect_request` builds
    `Request(newurl, method="GET", ...)` with no `data=`), so following it would send an
    empty request to an API whose entire payload is the body.
    """
    target = "http://members.iparking.co.kr/api/members/elsewhere"
    transport, _ = _transport({
        MEMBERS_HTTP: (301, {"location": target}, b""),
        target: (200, {}, b'{"result":"0000"}'),
    })
    with pytest.raises(BodyRedirect):
        transport.request("POST", MEMBERS_HTTP, body=b"encrypted-payload")
    assert _stub_of(transport).requested == [MEMBERS_HTTP]


def test_https_to_https_301_on_post_with_body_raises_body_redirect():
    """Same-scheme and fully TLS, on the host that carries the password. Still refused."""
    target = "https://oauth.parkingcloud.co.kr/api/oauth/store/authorize/v2"
    transport, _ = _transport({
        OAUTH: (302, {"location": target}, b""),
        target: (200, {}, b'{"result":"0000"}'),
    })
    with pytest.raises(BodyRedirect):
        transport.request("POST", OAUTH, body=b"encrypted-credentials")
    assert _stub_of(transport).requested == [OAUTH]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_every_followable_3xx_on_a_body_request_is_refused(code):
    target = "http://members.iparking.co.kr/api/members/elsewhere"
    transport, _ = _transport({
        MEMBERS_HTTP: (code, {"location": target}, b""),
        target: (200, {}, b'{"result":"0000"}'),
    })
    with pytest.raises(RedirectRefused):
        transport.request("POST", MEMBERS_HTTP, body=b"encrypted")
    assert _stub_of(transport).requested == [MEMBERS_HTTP]


def test_unfollowable_3xx_on_a_body_request_is_also_refused():
    """A 304 never reaches `redirect_request`; the Transport-level check catches it."""
    transport, _ = _transport({MEMBERS_HTTP: (304, {}, b"")})
    with pytest.raises(BodyRedirect):
        transport.request("POST", MEMBERS_HTTP, body=b"encrypted")


def test_same_scheme_redirect_without_a_body_is_followed():
    """Predicate 2 is conditioned on the body, not a blanket ban on same-scheme 3xx."""
    target = "http://members.iparking.co.kr/api/members/elsewhere"
    transport, _ = _transport({
        MEMBERS_HTTP: (301, {"location": target}, b""),
        target: (200, {}, b'{"result":"0000"}'),
    })
    resp = transport.request("GET", MEMBERS_HTTP)
    assert resp.status == 200
    assert _stub_of(transport).requested == [MEMBERS_HTTP, target]


# --- final_url, timeout, error surfacing ------------------------------------


def test_final_url_is_the_post_redirect_url():
    """Acceptance criterion 4 asserts the scheme actually reached, not the one requested."""
    transport, _ = _transport({
        MEMBERS_HTTP: (301, {"location": MEMBERS_HTTPS}, b""),
        MEMBERS_HTTPS: (200, {}, b"{}"),
    })
    resp = transport.request("GET", MEMBERS_HTTP)
    assert resp.final_url == MEMBERS_HTTPS
    assert resp.final_scheme == "https"


def test_final_scheme_of_a_direct_response():
    assert Response(200, "{}", MEMBERS_HTTP).final_scheme == "http"
    assert Response(200, "{}", OAUTH).final_scheme == "https"


def test_timeout_is_passed_on_every_open():
    """urllib's default is None — block forever. Regression guard, not a style check."""
    transport, _ = _transport({MEMBERS_HTTP: (200, {}, b"{}")})
    transport.request("POST", MEMBERS_HTTP, body=b"x")
    assert _stub_of(transport).timeouts == [DEFAULT_TIMEOUT_S]
    assert DEFAULT_TIMEOUT_S == 15.0


def test_timeout_is_passed_through_a_redirect():
    transport, _ = _transport({
        MEMBERS_HTTP: (301, {"location": MEMBERS_HTTPS}, b""),
        MEMBERS_HTTPS: (200, {}, b"{}"),
    })
    transport.request("GET", MEMBERS_HTTP)
    assert _stub_of(transport).timeouts == [DEFAULT_TIMEOUT_S, DEFAULT_TIMEOUT_S]


def test_error_status_body_is_returned_not_raised():
    """This API puts its verdict in the body, so a 4xx body must reach the caller."""
    transport, _ = _transport({MEMBERS_HTTP: (400, {}, b'{"result":"2031"}')})
    resp = transport.request("POST", MEMBERS_HTTP, body=b"x")
    assert (resp.status, resp.text) == (400, '{"result":"2031"}')


def test_urlerror_becomes_network_error():
    transport, _ = _transport({MEMBERS_HTTP: urllib.error.URLError("no route to host")})
    with pytest.raises(NetworkError):
        transport.request("GET", MEMBERS_HTTP)


def test_timeout_error_becomes_network_error():
    transport, _ = _transport({MEMBERS_HTTP: TimeoutError("timed out")})
    with pytest.raises(NetworkError):
        transport.request("GET", MEMBERS_HTTP)


def test_str_body_is_encoded_as_utf8():
    """`crypto.py` hands over a base64 `str`; urllib needs bytes."""
    transport, _ = _transport({MEMBERS_HTTP: (200, {}, b"{}")})
    resp = transport.request(
        "POST", MEMBERS_HTTP, headers={"version": "2.0.0"}, body="한글-payload"
    )
    assert resp.status == 200


def test_body_and_header_values_are_never_logged():
    """Plan §8: the body is the encrypted payload and `authorization` is a live token."""
    transport, logs = _transport({MEMBERS_HTTP: (200, {}, b"{}")})
    token = "11111111-2222-3333-4444-555555555555"
    transport.request(
        "POST", MEMBERS_HTTP, headers={"authorization": token}, body=b"secret-payload"
    )
    joined = "\n".join(logs)
    assert token not in joined
    assert "secret-payload" not in joined


# --- classifying the members host's ~30 % connection resets -------------------
#
# Measured 2026-08-04: 20 identical read-only requests, 14 answers, 6 dead sockets. The
# classification is what licenses a retry, so these tests are about the *boundary* of the
# retryable class far more than about its interior.


def test_a_connection_reset_is_a_connection_lost():
    """macOS errno 54 — the shape the fault takes off the hub."""
    transport, _ = _transport({MEMBERS_HTTP: ConnectionResetError(54, "Connection reset by peer")})
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP)


def test_a_connection_lost_is_still_a_network_error():
    """Subclassing matters: every existing `except NetworkError` must keep catching it.

    `client._attempt_register` catches `NetworkError`, and if a reset stopped being one, a
    reset on the register POST would escape that handler and skip the recovery re-query —
    turning a *resolvable* outcome into a traceback. Which is the bug this task started from.
    """
    transport, _ = _transport({MEMBERS_HTTP: ConnectionResetError(54, "reset")})
    with pytest.raises(NetworkError):
        transport.request("GET", MEMBERS_HTTP)
    assert issubclass(ConnectionLost, NetworkError)


def test_an_incomplete_read_while_consuming_the_body_is_a_connection_lost():
    """The hub's report of the same fault: `IncompleteRead(255 bytes read)`.

    `IncompleteRead` subclasses `http.client.HTTPException`, **not** `OSError`, so before it
    was classified it escaped the transport's `except` clause entirely — past the register
    path's handlers too. That is why a failed registration logged three request lines and then
    nothing at all: no error, no traceback, nothing for the maintainer to read.
    """
    assert not issubclass(http.client.IncompleteRead, OSError), "the premise of this test"
    transport, _ = _transport({MEMBERS_HTTP: truncated})
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP)


def test_a_remote_disconnection_is_a_connection_lost():
    transport, _ = _transport({
        MEMBERS_HTTP: http.client.RemoteDisconnected("Remote end closed connection"),
    })
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP)


def test_a_reset_wrapped_in_a_urlerror_is_still_a_connection_lost():
    """urllib usually hands the socket error back inside `URLError.reason`."""
    transport, _ = _transport({
        MEMBERS_HTTP: urllib.error.URLError(ConnectionResetError(54, "reset")),
    })
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP)


def test_a_urlerror_with_a_plain_string_reason_is_not_a_connection_lost():
    """The unwrap must survive a `reason` that is not an exception at all."""
    transport, _ = _transport({MEMBERS_HTTP: urllib.error.URLError("no route to host")})
    with pytest.raises(NetworkError) as caught:
        transport.request("GET", MEMBERS_HTTP)
    assert not isinstance(caught.value, ConnectionLost)


# --- THE boundary: a timeout is not a reset ----------------------------------


def test_a_timeout_is_never_classified_as_a_connection_lost():
    """The single most load-bearing assertion in this file.

    A reset means the connection is **gone**, so for a read, asking again is free. A timeout
    means the request may still be **in flight and may still land**. `TimeoutError` is an
    `OSError` subclass, so a classifier that checked errnos before checking for a timeout
    would call one a reset — and then `POST /invitations` would be retryable, and a visitor
    vehicle would get registered twice at a real building.

    This test is what stops that refactor. If it ever fails, the fix is the classifier, never
    this assertion.
    """
    transport, _ = _transport({MEMBERS_HTTP: TimeoutError("timed out")})
    with pytest.raises(NetworkError) as caught:
        transport.request("GET", MEMBERS_HTTP)
    assert not isinstance(caught.value, ConnectionLost), "a timeout is not a dead socket"


def test_a_timeout_wrapped_in_a_urlerror_is_not_a_connection_lost():
    """The same boundary, one unwrap deeper. `URLError` is itself an `OSError` subclass, so
    this is the case that fails first if the classifier is ever loosened to "any `OSError`"."""
    transport, _ = _transport({MEMBERS_HTTP: urllib.error.URLError(TimeoutError("timed out"))})
    with pytest.raises(NetworkError) as caught:
        transport.request("GET", MEMBERS_HTTP)
    assert not isinstance(caught.value, ConnectionLost)


def test_no_stdlib_exception_is_both_a_timeout_and_a_dead_socket():
    """Pins the *premise* of the ordering, against CPython rather than against our code.

    Every errno meaning "the peer tore the connection down" maps onto a type in `_LOST_TYPES`,
    and none of them maps onto `TimeoutError`. That measured fact is why the timeout check
    changes no answer today — and why an errno-based branch was deleted from the classifier as
    unreachable. If a future CPython reshuffles this mapping, this test says so out loud
    instead of leaving a comment that is quietly no longer true.
    """
    for name in ("ECONNRESET", "ECONNABORTED", "EPIPE", "ESHUTDOWN"):
        built = OSError(getattr(errno, name), "x")
        assert isinstance(built, transport_module._LOST_TYPES), name
        assert not isinstance(built, TimeoutError), name

    assert not isinstance(OSError(errno.ETIMEDOUT, "x"), transport_module._LOST_TYPES)
    assert TimeoutError("timed out").errno is None


def test_a_timeout_never_wins_the_race_against_the_retryable_types():
    """The ordering itself, exercised with the shape the stdlib will not produce.

    This exception is deliberately synthetic: nothing in CPython is both a `TimeoutError` and
    a `ConnectionResetError` (the test above proves it), so on real traffic the ordering is
    unobservable. It is still the line that holds the invariant, because the realistic future
    edit is *widening the allow-list* — adding `TimeoutError` or `OSError` to `_LOST_TYPES` to
    "catch more transient faults". When that happens the ordering is the only thing left
    saying a timeout is not a dead socket, and an ordering nobody exercises is one nobody can
    trust.

    Called directly rather than through `request()` because there is no way to make a real
    socket produce this, and a guard tested only via behaviour it cannot reach is untested.
    """
    class _TimedOutAndReset(TimeoutError, ConnectionResetError):
        """Both natures at once — a timeout that also matches the retryable allow-list."""

    assert isinstance(_TimedOutAndReset(), transport_module._LOST_TYPES), "the premise"
    assert transport_module._is_connection_lost(_TimedOutAndReset()) is False, (
        "the timeout nature must win, or writes become retryable"
    )
    # And the same through one layer of urllib wrapping, which is how it would really arrive.
    assert transport_module._is_connection_lost(
        urllib.error.URLError(_TimedOutAndReset())
    ) is False


def test_the_classifier_terminates_on_a_reason_that_is_not_an_exception():
    """`URLError.reason` is a `str` more often than not; the unwrap walk must end."""
    assert transport_module._is_connection_lost(urllib.error.URLError("no route")) is False
    assert transport_module._is_connection_lost(None) is False


def test_a_timeout_is_not_retried_even_when_attempts_allows_it():
    """Retry policy keys on the *class*, not on `attempts` being greater than one.

    A caller that legitimately asked for 4 attempts of a read must still not re-send anything
    on a timeout — because the same `attempts` plumbing is what a future edit would reach for
    on the write path.
    """
    transport, _ = _transport({MEMBERS_HTTP: TimeoutError("timed out")})
    with pytest.raises(NetworkError):
        transport.request("GET", MEMBERS_HTTP, attempts=4)
    assert len(_stub_of(transport).requested) == 1, "a timeout may never be re-sent"
    assert _stub_of(transport).backoffs == []


# --- retrying, and only what may be retried ---------------------------------


def test_a_reset_on_a_read_retries_and_then_succeeds():
    """The fix, stated as the behaviour it buys: one reset, one retry, one answer."""
    transport, _ = _transport({
        MEMBERS_HTTP: [ConnectionResetError(54, "reset"), (200, {}, b'{"result":"0000"}')],
    })
    resp = transport.request("POST", MEMBERS_HTTP, body=b"encrypted", attempts=4)
    assert resp.status == 200
    assert resp.text == '{"result":"0000"}'
    assert len(_stub_of(transport).requested) == 2
    assert len(_stub_of(transport).backoffs) == 1, "it waited once, between the two attempts"


def test_three_resets_in_a_row_still_succeed_on_the_fourth_attempt():
    """Why the floor is 4: at P(fail)=0.3, four attempts leaves 0.8 %."""
    transport, _ = _transport({
        MEMBERS_HTTP: [
            ConnectionResetError(54, "reset"),
            http.client.IncompleteRead(b"p" * 255, 100),
            urllib.error.URLError(ConnectionResetError(54, "reset")),
            (200, {}, b'{"result":"0000"}'),
        ],
    })
    assert transport.request("GET", MEMBERS_HTTP, attempts=4).status == 200
    assert len(_stub_of(transport).requested) == 4


def test_the_default_is_no_retry_at_all():
    """`attempts` defaults to 1 so nothing becomes retryable by inheriting a default.

    The register path depends on this being the default rather than the exception.
    """
    transport, _ = _transport({MEMBERS_HTTP: ConnectionResetError(54, "reset")})
    with pytest.raises(ConnectionLost):
        transport.request("POST", MEMBERS_HTTP, body=b"encrypted")
    assert len(_stub_of(transport).requested) == 1
    assert _stub_of(transport).backoffs == []


def test_giving_up_after_the_cap_raises_connection_lost():
    transport, _ = _transport({MEMBERS_HTTP: ConnectionResetError(54, "reset")})
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP, attempts=4)
    assert len(_stub_of(transport).requested) == 4, "exactly the cap, not one more"
    assert len(_stub_of(transport).backoffs) == 3, "no pointless wait after the last attempt"


def test_a_refused_redirect_is_never_retried():
    """Re-sending would not change the server's mind, and predicate 1 is not a fault."""
    transport, _ = _transport({
        MEMBERS_HTTPS: (301, {"location": MEMBERS_HTTP}, b""),
        MEMBERS_HTTP: (200, {}, b'{"result":"0000"}'),
    })
    with pytest.raises(RedirectRefused):
        transport.request("GET", MEMBERS_HTTPS, attempts=4)
    assert _stub_of(transport).requested == [MEMBERS_HTTPS]


def test_an_error_status_is_never_retried():
    """A 4xx is an *answer*; this API carries its verdict in the body."""
    transport, _ = _transport({MEMBERS_HTTP: (400, {}, b'{"result":"2031"}')})
    resp = transport.request("POST", MEMBERS_HTTP, body=b"x", attempts=4)
    assert resp.status == 400
    assert len(_stub_of(transport).requested) == 1


def test_attempts_below_one_is_refused():
    """`attempts=0` would silently send nothing and return `None`."""
    transport, _ = _transport({MEMBERS_HTTP: (200, {}, b"{}")})
    with pytest.raises(ValueError):
        transport.request("GET", MEMBERS_HTTP, attempts=0)
    assert _stub_of(transport).requested == []


# --- backoff ----------------------------------------------------------------


def test_backoff_grows_and_stays_under_the_jittered_ceiling():
    transport, _ = _transport({MEMBERS_HTTP: ConnectionResetError(54, "reset")})
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP, attempts=5)

    delays = _stub_of(transport).backoffs
    ceiling = RETRY_BACKOFF_MAX_S * (1.0 + RETRY_JITTER)
    assert len(delays) == 4
    assert all(0.0 <= d <= ceiling for d in delays), delays
    # Jitter can reorder neighbours, but the last must clear the first's un-jittered floor —
    # otherwise the backoff is flat and the "exponential" in the docstring is a lie.
    assert delays[-1] > delays[0]


def test_backoff_is_actually_jittered():
    """Several devices poll on the same hour; identical backoffs would line their retries up.

    Sampled across independent transports because within one call the delays differ anyway
    (they grow) — the property under test is that the *same* attempt number varies.
    """
    firsts = []
    for _ in range(12):
        transport, _ = _transport({MEMBERS_HTTP: ConnectionResetError(54, "reset")})
        with pytest.raises(ConnectionLost):
            transport.request("GET", MEMBERS_HTTP, attempts=2)
        firsts += _stub_of(transport).backoffs
    assert len(set(firsts)) > 1, "every first backoff was identical; the jitter is not applied"


# --- logging the failure (it used to log nothing at all) ---------------------


def test_every_retry_and_the_give_up_are_logged_with_the_exception_type():
    """The maintainer could not see why a registration failed. That was its own defect."""
    transport, logs = _transport({MEMBERS_HTTP: ConnectionResetError(54, "reset")})
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP, attempts=3)

    joined = "\n".join(logs)
    assert joined.count("ConnectionResetError") >= 3, "each attempt names the exception type"
    assert "on attempt 1/3" in joined
    assert "on attempt 2/3" in joined
    assert "gave up after 3 attempt(s)" in joined


def test_the_give_up_line_names_the_hub_form_of_the_fault_too():
    transport, logs = _transport({MEMBERS_HTTP: truncated})
    with pytest.raises(ConnectionLost):
        transport.request("GET", MEMBERS_HTTP, attempts=2)
    assert "IncompleteRead" in "\n".join(logs)


def test_a_retried_request_never_logs_the_token_or_the_body():
    """The redaction rules do not get a pass just because the request is being retried.

    `IncompleteRead`'s `str()` reports a byte *count*, never the partial bytes — verified —
    which is what makes it safe to log at all: those bytes are a response body.
    """
    transport, logs = _transport({MEMBERS_HTTP: [truncated, (200, {}, b"{}")]})
    token = "11111111-2222-3333-4444-555555555555"
    transport.request(
        "POST", MEMBERS_HTTP, headers={"authorization": token}, body=b"secret-payload",
        attempts=4,
    )
    joined = "\n".join(logs)
    assert token not in joined
    assert "secret-payload" not in joined
    assert "ppppp" not in joined, "the partial body must not reach a log line"
