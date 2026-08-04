"""Puts the repo root on `sys.path` so `import iparking_lib...` resolves.

Mirrors `../com.lomohome.navien/tests/conftest.py`. This is needed rather than optional:
`pyproject.toml` declares `packages = []` (the flat-layout fix — see the comment there), so
`uv pip install -e ".[dev]"` installs nothing importable, and pytest's `prepend` import
mode only puts `tests/` itself on the path. Without this line every test module fails
collection with `ModuleNotFoundError: No module named 'iparking_lib'`.

Shared file: other workers' fixtures and any fake-`homey` harness belong here too. Note
that nothing under `iparking_lib/iparking/` needs such a harness — that package never
imports the `homey` SDK (acceptance criterion 1), which is why it is the part that is
testable off-device.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ruff: noqa: E402  — the path insert above has to run before iparking_lib is importable.
import email.message
import io
import json
import time
import urllib.request
import urllib.response

import pytest

from iparking_lib.const import MEMBERS_BASE_PATH, MEMBERS_HOST, OAUTH_HOST, OAUTH_PATH
from iparking_lib.iparking.client import IparkingApi
from iparking_lib.iparking.transport import Transport

# --- URLs the client is expected to build -------------------------------------
#
# Spelled out in full, and asserted **by name**, because the per-host scheme asymmetry is
# the single most misreadable thing in this app: `https` for oauth (it carries the
# password), `http` for members (that server 301s every https request down to cleartext).
# A test that derived these from `const.SCHEMES` would agree with a table that had been
# edited wrongly, which is the one failure the assertion exists to catch.

OAUTH_URL = f"https://{OAUTH_HOST}{OAUTH_PATH}"
MEMBERS_ROOT = f"http://{MEMBERS_HOST}{MEMBERS_BASE_PATH}"
MEMBERS_ROOT_HTTPS = f"https://{MEMBERS_HOST}{MEMBERS_BASE_PATH}"

STOR_SEQ = 100001
PARK_SEQ = 9001
LOT_ID = "1160009001"
PARK_NAME = "예시동 샘플아파트[출입통제A]"

LOTS_URL = f"{MEMBERS_ROOT}/parkinglot/list/{STOR_SEQ}"
HISTORY_URL = f"{MEMBERS_ROOT}/invitations/list"
REGISTER_URL = f"{MEMBERS_ROOT}/invitations"


# --- canned vendor responses --------------------------------------------------


def login_ok(*, stores=((STOR_SEQ, "Y"),), memb_name="999동9999호", token="tok-uuid-1"):
    """A login envelope shaped like the real one (`docs/RECON.md` §1).

    `stores` is a list of `(stor_seq, yn)` so the authorization-list cases criterion 11 asks
    for — empty, multi-entry, and `!= "Y"` — are each one argument rather than a hand-built
    body. `memb_name` defaults to a synthetic unit, never the maintainer's real address.
    """
    return {
        "result": "0000",
        "resultMessage": "성공",
        "auth_data": {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": 604800,
            # Literally what the vendor sends: an `http://` URL. The client must take the
            # host from it and the scheme from policy.
            "operation_company": [
                {"domain": f"http://{MEMBERS_HOST}", "operation_cmpy_cd": 1}
            ],
            "memb_name": memb_name,
            "stor_name": memb_name,
            "invitation_authorization_list": [
                {"stor_seq": seq, "invitation_register_authorization_yn": yn}
                for seq, yn in stores
            ],
        },
    }


def lots_ok(rows=None):
    return {
        "result": "0000",
        "totalCnt": 1,
        "resultData": rows if rows is not None else [
            {"park_seq": PARK_SEQ, "lot_id": LOT_ID, "park_name": PARK_NAME,
             "park_group_id": None}
        ],
    }


def history_ok(rows=(), total=()):
    """A 등록 내역 envelope. `rows` are `(car_number, date, status)` or full dicts.

    `total` defaults to `[]`, which is what the real server returned even on a 43-record
    range — verified twice, hence never used as a status aggregate.
    """
    built = []
    for index, row in enumerate(rows, start=1):
        # A dict passes through verbatim (a test pinning exact keys), and so does anything
        # that is not a 3-tuple — that is how a deliberately malformed row gets injected.
        if not isinstance(row, tuple) or len(row) != 3:
            built.append(row)
            continue
        car, date, status = row
        built.append({
            "invitation_date": date,
            "invt_seq": 3184550 + index,
            "car_number": car,
            "inot_status": status,
            "park_name": PARK_NAME,
            "seq_num": float(index),
        })
    return {
        "result": "0000",
        "totalCnt": len(built),
        "resultData": {"total": list(total), "invitationList": built},
    }


def envelope(result, message="", **extra):
    """Any other vendor envelope, e.g. `envelope("2031")` for an expired token."""
    return {"result": result, "resultMessage": message, **extra}


# --- the transport seam -------------------------------------------------------


class _StubResponse(urllib.response.addinfourl):
    """`addinfourl` plus the `.msg` attribute `HTTPErrorProcessor` reads off a response.

    `addinfourl` supplies `.code`, `.status`, `.geturl()` and `.info()` but not `.msg`
    (verified on CPython 3.14).
    """

    def __init__(self, status, headers, body, url):
        message = email.message.Message()
        for key, value in headers.items():
            message[key] = value
        super().__init__(io.BytesIO(body), message, url, status)
        self.msg = "Stub"


class StubHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Serves canned responses instead of opening a socket.

    Subclassing **both** handlers is what makes `build_opener` substitute this for the two
    real ones, and `handler_order = 100` (below the default 500) is what makes it win the
    `https_open` chain against the `HTTPSHandler` that `Transport` always installs. Without
    the ordering the tie breaks on insertion order and the suite dials out for real.

    It replaces the socket and **nothing else**: `StrictRedirectHandler` stays installed, so
    a stubbed 301 exercises the real refusal, and a route that raises `URLError` goes through
    the real `NetworkError` conversion. That is what makes the classification assertions in
    these tests mean something.
    """

    handler_order = 100

    def __init__(self, routes):
        super().__init__()
        # url -> a single outcome or a list consumed in order, last entry reused. The reuse
        # keeps a test scripting only the responses it is actually asserting on.
        self.routes = {url: list(v) if isinstance(v, list) else [v]
                       for url, v in routes.items()}
        self.calls = []          # (method, url, headers, body)
        self.timeouts = []

    def http_open(self, req):
        self.calls.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        self.timeouts.append(req.timeout)
        try:
            script = self.routes[req.full_url]
        except KeyError:
            raise AssertionError(f"unstubbed request: {req.get_method()} {req.full_url}") from None
        outcome = script[0] if len(script) == 1 else script.pop(0)
        if callable(outcome) and not isinstance(outcome, Exception):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        status, headers, body = self._normalize(outcome)
        return _StubResponse(status, headers, body, req.full_url)

    https_open = http_open

    @staticmethod
    def _normalize(outcome):
        if isinstance(outcome, dict):
            return 200, {}, json.dumps(outcome, ensure_ascii=False).encode()
        if isinstance(outcome, tuple) and len(outcome) == 3:
            status, headers, body = outcome
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode()
            elif isinstance(body, str):
                body = body.encode()
            return status, headers, body or b""
        raise AssertionError(f"cannot interpret stub outcome {outcome!r}")

    # --- assertions these tests are built on ---

    def urls(self, method=None):
        return [url for m, url, _h, _b in self.calls if method is None or m == method]

    def count(self, url):
        return sum(1 for _m, u, _h, _b in self.calls if u == url)

    def headers_for(self, url):
        return [h for _m, u, h, _b in self.calls if u == url]

    def bodies_for(self, url):
        return [b for _m, u, _h, b in self.calls if u == url]


def slow(outcome, seconds):
    """A route that blocks the executor thread before answering.

    Used for the register-path timeout tests, and it reproduces the real hazard rather than
    approximating it: `asyncio.wait_for` cancels the *await*, not this thread, so the "POST"
    is genuinely still in flight when the budget fires — which is exactly the orphan the
    recovery re-query exists to resolve.
    """
    def _route():
        time.sleep(seconds)
        return outcome
    return _route


@pytest.fixture
def make_api():
    """Build an `IparkingApi` wired to a `StubHandler`, plus its log lines.

    Returns `(api, stub, logs)`. The `Transport` is real — only its socket is stubbed.
    """
    def _build(routes, *, username="iparking-dev", password="synthetic-pw", **kwargs):
        stub = StubHandler(routes)
        logs = []
        api = IparkingApi(
            username=username,
            password=password,
            log=logs.append,
            transport=Transport(log=logs.append, handlers=[stub]),
            **kwargs,
        )
        return api, stub, logs
    return _build


@pytest.fixture
def no_sleep(monkeypatch):
    """Record the recovery pause instead of really waiting three seconds for it.

    Returns the list of requested durations, so a test can assert the pause *happened* —
    dropping it would make the recovery query a server that has not committed the write yet,
    which is a false "not registered".
    """
    import asyncio as _asyncio

    slept = []
    real_sleep = _asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        slept.append(seconds)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(_asyncio, "sleep", fake_sleep)
    return slept
