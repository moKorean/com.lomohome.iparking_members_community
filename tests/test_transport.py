"""Redirect-policy tests for `iparking_lib.iparking.transport`.

These never touch a real host. `StubHandler` replaces the socket via urllib's own
substitution rule (see its docstring) while leaving `StrictRedirectHandler` installed, so
a stubbed 301 exercises the real refusal code rather than a mock of it.

The assertions that matter are the *negative* ones: a refused redirect must leave the
target URL unrequested. "It raised" alone would still pass if the request had already
gone out.
"""

from __future__ import annotations

import email.message
import io
import urllib.error
import urllib.request
import urllib.response

import pytest

from iparking_lib.iparking.transport import (
    DEFAULT_TIMEOUT_S,
    BodyRedirect,
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
        self.routes = routes
        self.requested: list[str] = []
        self.timeouts: list[float | None] = []

    def http_open(self, req):
        self.requested.append(req.full_url)
        self.timeouts.append(req.timeout)
        outcome = self.routes[req.full_url]
        if isinstance(outcome, Exception):
            raise outcome
        status, headers, body = outcome
        return _StubResponse(status, headers, body, req.full_url)

    https_open = http_open


def _transport(routes: dict):
    logs: list[str] = []
    return Transport(log=logs.append, handlers=[StubHandler(routes)]), logs


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
