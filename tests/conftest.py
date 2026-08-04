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
import types
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
        # Every retry backoff the transport asked for, in order. Lives on the handler rather
        # than in its own fixture so `make_api`'s return tuple stays three-wide and the
        # existing tests keep working; `make_api` wires `Transport(sleep=...)` to it, so a
        # retry test asserts the backoff happened without the suite really waiting for it.
        self.backoffs = []

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
            transport=Transport(
                log=logs.append, handlers=[stub], sleep=stub.backoffs.append
            ),
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


# --- the fake `homey` SDK -----------------------------------------------------
#
# `import homey` genuinely fails in this checkout — `python_packages/*/` carries only certifi
# — so without this harness `app.py`, `api.py` and the driver modules cannot be imported at
# all and their tests cannot even be collected.
#
# The surface below was enumerated by reading `iparking_lib/compat.py`, `app.py` and `api.py`,
# not guessed; strictness is the point. An attribute nobody stubbed raises AttributeError
# instead of returning a mock that swallows the call, because a permissive fake would let a
# test pass against a member the real SDK does not have — which is precisely the risk a fake
# SDK introduces.
#
# Both calling contracts are supported deliberately. `compat.resolve` awaits whatever comes
# back if it is awaitable, and `compat.flow_card` / `register_run_listener` try the snake_case
# and camelCase spellings in turn, because the SDK's Python surface is not pinned. compat.py's
# docstring names getting this wrong as the app's silent failure mode — an un-awaited
# `settings.set()` coroutine looks like a successful write and stores nothing — so the fake
# lets each test pick the contract it stands on rather than baking one in.
#
# Nothing here touches the `iparking_lib/iparking/*` tests: that package never imports the
# SDK (acceptance criterion 1), which is what makes it the part testable off-device.


class _Strict:
    """Base whose unstubbed attributes raise, with a message saying what to do."""

    def __getattr__(self, name):
        raise AttributeError(
            f"{type(self).__name__} has no {name!r}. If that is part of the homey SDK "
            f"surface, add it to tests/conftest.py deliberately, after checking the real SDK "
            f"has it — this fake stubs only what iparking_lib actually calls."
        )


def _maybe_async(value, awaitable: bool):
    """Return `value`, or a coroutine yielding it, per the contract under test."""
    if not awaitable:
        return value

    async def _coro():
        return value

    return _coro()


class FakeSettings(_Strict):
    """`homey.settings` — compat.setting_get / setting_set / setting_unset."""

    def __init__(self, values=None, *, awaitable=False, has_unset=True):
        self.values = dict(values or {})
        self.writes = []
        self.unsets = []
        self._awaitable = awaitable
        self._has_unset = has_unset

    def get(self, key):
        return _maybe_async(self.values.get(key), self._awaitable)

    def set(self, key, value):
        self.values[key] = value
        self.writes.append((key, value))
        return _maybe_async(None, self._awaitable)

    def unset(self, key):
        # Not every build exposes unset(); compat.setting_unset falls back to set(key, "")
        # when it is missing, so the fake can model a build without it.
        if not self._has_unset:
            raise AttributeError("unset")
        self.values.pop(key, None)
        self.unsets.append(key)
        return _maybe_async(None, self._awaitable)


class FakeI18n(_Strict):
    """`homey.i18n` — compat.language tries get_language() then getLanguage()."""

    def __init__(self, language="ko", *, spelling="snake", awaitable=False):
        self.language = language
        self._awaitable = awaitable
        if spelling in ("snake", "both"):
            self.get_language = self._get_language
        if spelling in ("camel", "both"):
            self.getLanguage = self._get_language  # noqa: N815 — the SDK's own spelling

    def _get_language(self):
        return _maybe_async(self.language, self._awaitable)


class FakeApp(_Strict):
    """`homey.app` — compat.shared_api / reauth_shared_api / app_logout, plus the `log` all
    three reach for.

    `calls` records the order in which they were used, which is what
    `clear_credentials`'s load-bearing "logout *before* forgetting the account" ordering is
    asserted on.
    """

    def __init__(self, api=None, *, awaitable=False, expose_shared_api=True,
                 expose_reauth=True, expose_logout=True, shared_api_error=None,
                 reauth_error=None):
        self.api = api
        self.logs = []
        self.calls = []
        self.reauths = []
        self._awaitable = awaitable
        self._shared_api_error = shared_api_error
        self._reauth_error = reauth_error
        # Absent rather than present-and-None: compat uses getattr(app, "shared_api", None)
        # to decide whether the shared session exists at all, so the fallback branch is only
        # reachable when the attribute is genuinely missing.
        if expose_shared_api:
            self.shared_api = self._shared_api
        if expose_reauth:
            self.reauth = self._reauth
        if expose_logout:
            self.logout = self._logout

    def log(self, *parts):
        self.logs.append(" ".join(str(p) for p in parts))

    async def _shared_api(self):
        self.calls.append("shared_api")
        if self._shared_api_error is not None:
            raise self._shared_api_error
        return self.api

    async def _reauth(self, username, password):
        self.calls.append("reauth")
        self.reauths.append((username, password))
        if self._reauth_error is not None:
            raise self._reauth_error
        return self.api

    async def _logout(self):
        self.calls.append("logout")
        if self.api is not None:
            self.api.logout()
        return self.api


class FakeFlowCard(_Strict):
    """One Flow card — compat.register_run_listener tries both spellings."""

    def __init__(self, card_id, *, spelling="snake"):
        self.card_id = card_id
        self.listener = None
        if spelling in ("snake", "both"):
            self.register_run_listener = self._register
        if spelling in ("camel", "both"):
            self.registerRunListener = self._register  # noqa: N815 — the SDK's spelling

    def _register(self, fn):
        self.listener = fn


class FakeFlow(_Strict):
    """`homey.flow` — compat.flow_card tries get_*_card then get*Card."""

    def __init__(self, *, spelling="snake"):
        self.cards = {}
        self._spelling = spelling
        if spelling in ("snake", "both"):
            self.get_action_card = self._action
            self.get_condition_card = self._condition
        if spelling in ("camel", "both"):
            self.getActionCard = self._action        # noqa: N815 — the SDK's spelling
            self.getConditionCard = self._condition  # noqa: N815 — the SDK's spelling

    def _card(self, kind, card_id):
        key = (kind, card_id)
        if key not in self.cards:
            self.cards[key] = FakeFlowCard(card_id, spelling=self._spelling)
        return self.cards[key]

    def _action(self, card_id):
        return self._card("action", card_id)

    def _condition(self, card_id):
        return self._card("condition", card_id)


class FakeNotifications(_Strict):
    """`homey.notifications` — the Flow card's register outcome goes here.

    The Python SDK's spelling and call shape for this manager are **not** established
    anywhere readable (the Node API is `createNotification({excerpt})`, while every manager
    this app does use is snake_case with plain arguments), so `device._notify` tries both
    names and three call shapes. This fake therefore models *one* contract at a time and
    raises `TypeError` on the others — which is what makes the tolerance testable rather
    than merely present: a fake that accepted everything would pass whichever shape the
    code tried first, including a wrong one.
    """

    def __init__(self, *, spelling="snake", shape="kwarg", awaitable=False, error=None):
        self.excerpts = []
        self._shape = shape
        self._awaitable = awaitable
        self._error = error
        if spelling in ("snake", "both"):
            self.create_notification = self._create
        if spelling in ("camel", "both"):
            self.createNotification = self._create  # noqa: N815 — the SDK's own spelling

    def _create(self, *args, **kwargs):
        if self._shape == "kwarg":
            if args or list(kwargs) != ["excerpt"]:
                raise TypeError("create_notification() expects excerpt=")
            excerpt = kwargs["excerpt"]
        elif self._shape == "dict":
            if kwargs or len(args) != 1 or not isinstance(args[0], dict):
                raise TypeError("create_notification() expects one options dict")
            excerpt = args[0].get("excerpt")
        elif self._shape == "permissive":
            # What the real hub does, measured 2026-08-04: one positional argument of **any**
            # type, stored in `excerpt` verbatim. This contract is why the dict shape used to
            # "succeed" — nothing raised, the log said ok, and the timeline rendered a blank
            # row because the field held a dict. Every other shape here raises on a wrong
            # call, so none of them could reproduce that, and no test caught it.
            if kwargs or len(args) != 1:
                raise TypeError("create_notification() expects one positional argument")
            excerpt = args[0]
        else:  # positional string
            if kwargs or len(args) != 1 or not isinstance(args[0], str):
                raise TypeError("create_notification() expects one string")
            excerpt = args[0]
        if self._error is not None:
            raise self._error
        self.excerpts.append(excerpt)
        return _maybe_async(None, self._awaitable)


class FakeHomey(_Strict):
    """The `self.homey` every handler, device and driver is handed."""

    def __init__(self, *, settings=None, i18n=None, app=None, flow=None, language=None,
                 notifications=None):
        self.settings = settings if settings is not None else FakeSettings()
        self.i18n = i18n if i18n is not None else FakeI18n()
        self.app = app if app is not None else FakeApp()
        self.flow = flow if flow is not None else FakeFlow()
        # Left absent unless a test asks for it: `device._notify` checks for the manager with
        # `getattr(..., None)` and degrades to a log line, and that branch is reachable only
        # when the attribute genuinely does not exist.
        if notifications is not None:
            self.notifications = notifications
        # compat.language's last resort. Left unset unless a test asks for it, so the
        # accessor chain above is what actually gets exercised.
        if language is not None:
            self.language = language


class Device(_Strict):
    """Stand-in for `homey.device.Device`.

    The capability/settings mutators below are the 자주 오는 차량 buttons' whole surface, and
    every one of them is spelling-selectable for the same reason `FakeFlow` is: **no Python stub
    ships with the Homey CLI**, so `add_capability` versus `addCapability` cannot be checked off
    a real SDK for any of them. `sdk_spelling="none"` models a runtime that has neither, which
    is the branch where a device must still end up with a working 주차장명 sensor.

    `sdk_awaitable=True` makes them return coroutines instead of values — the half of
    `compat.resolve`'s contract that fails silently when it is wrong.
    """

    def __init__(self, *, homey=None, store=None, capabilities=(), name="테스트 기기",
                 settings=None, sdk_spelling="snake", sdk_awaitable=False,
                 add_capability_error=None):
        self.homey = homey if homey is not None else FakeHomey()
        self.logs = []
        self.availability = []
        self._store = dict(store or {})
        self._capabilities = list(capabilities)
        self._name = name
        self._values = {}
        self.settings = dict(settings or {})
        # Every capability_options payload, in order, so a test can assert the *title* the tile
        # would show rather than merely that the setter was called.
        self.capability_options = {}
        self.setting_writes = []
        self.listeners = {}
        self._sdk_awaitable = sdk_awaitable
        self._add_capability_error = add_capability_error
        if sdk_spelling in ("snake", "both"):
            self.get_settings = self._get_settings
            self.set_settings = self._set_settings
            self.add_capability = self._add_capability
            self.remove_capability = self._remove_capability
            self.set_capability_options = self._set_capability_options
            self.register_capability_listener = self._register_capability_listener
        if sdk_spelling in ("camel", "both"):
            self.getSettings = self._get_settings                            # noqa: N815
            self.setSettings = self._set_settings                            # noqa: N815
            self.addCapability = self._add_capability                        # noqa: N815
            self.removeCapability = self._remove_capability                  # noqa: N815
            self.setCapabilityOptions = self._set_capability_options         # noqa: N815
            self.registerCapabilityListener = self._register_capability_listener  # noqa: N815

    def get_store(self) -> dict:
        return dict(self._store)

    # --- the capability/settings surface the tile buttons ride on ---

    def _get_settings(self):
        return _maybe_async(dict(self.settings), self._sdk_awaitable)

    def _set_settings(self, values):
        # The real SDK merges a partial dict rather than replacing the whole set.
        self.settings.update(values)
        self.setting_writes.append(dict(values))
        return _maybe_async(None, self._sdk_awaitable)

    def _add_capability(self, capability):
        if self._add_capability_error is not None:
            raise self._add_capability_error
        if capability not in self._capabilities:
            self._capabilities.append(capability)
        return _maybe_async(None, self._sdk_awaitable)

    def _remove_capability(self, capability):
        if capability in self._capabilities:
            self._capabilities.remove(capability)
        self._values.pop(capability, None)
        self.capability_options.pop(capability, None)
        return _maybe_async(None, self._sdk_awaitable)

    def _set_capability_options(self, capability, options):
        if capability not in self._capabilities:
            # The real SDK has no capability to hang options on either.
            raise ValueError(f"device has no capability {capability!r}")
        self.capability_options.setdefault(capability, {}).update(options)
        return _maybe_async(None, self._sdk_awaitable)

    def _register_capability_listener(self, capability, fn):
        self.listeners[capability] = fn

    async def press(self, capability, value=True):
        """Press a tile button the way Homey does: set the value, then fire the listener."""
        self._values[capability] = value
        return await self.listeners[capability](value, {})

    def get_capabilities(self) -> list:
        return list(self._capabilities)

    def get_name(self) -> str:
        return self._name

    def log(self, *parts) -> None:
        self.logs.append(" ".join(str(p) for p in parts))

    def get_capability_value(self, capability):
        return self._values.get(capability)

    async def set_available(self) -> None:
        self.availability.append(("available", None))

    async def set_unavailable(self, reason=None) -> None:
        self.availability.append(("unavailable", reason))

    async def set_capability_value(self, capability, value) -> None:
        if capability not in self._capabilities:
            # The real SDK rejects a capability the device does not have; the app is expected
            # to filter these out itself against get_capabilities().
            raise ValueError(f"device has no capability {capability!r}")
        self._values[capability] = value


class App(_Strict):
    """Stand-in for `homey.app.App`, the base `IparkingApp` subclasses.

    Only `log` and `homey` are stubbed, because those are the only two members `IparkingApp`
    reaches for on its base. Having it here means the shared-session tests exercise the real
    `IparkingApp._client` — where the in-place credential update lives — rather than a
    re-implementation of it.
    """

    def __init__(self, *, homey=None):
        self.homey = homey if homey is not None else FakeHomey()
        self.logs = []

    def log(self, *parts) -> None:
        self.logs.append(" ".join(str(p) for p in parts))


class Driver(_Strict):
    """Stand-in for `homey.driver.Driver` — `log` and `homey`."""

    def __init__(self, *, homey=None, name="테스트 드라이버"):
        self.homey = homey if homey is not None else FakeHomey()
        self.logs = []
        self._name = name

    def log(self, *parts) -> None:
        self.logs.append(" ".join(str(p) for p in parts))

    def get_name(self) -> str:
        return self._name


def _install_fake_homey() -> None:
    homey_module = types.ModuleType("homey")
    device_module = types.ModuleType("homey.device")
    driver_module = types.ModuleType("homey.driver")
    app_module = types.ModuleType("homey.app")
    device_module.Device = Device
    driver_module.Driver = Driver
    app_module.App = App
    homey_module.device = device_module
    homey_module.driver = driver_module
    homey_module.app = app_module
    sys.modules["homey"] = homey_module
    sys.modules["homey.device"] = device_module
    sys.modules["homey.driver"] = driver_module
    sys.modules["homey.app"] = app_module


try:  # a real SDK, wherever one exists, always wins over the fake
    import homey  # noqa: F401
except ImportError:
    _install_fake_homey()


@pytest.fixture
def make_homey():
    """Factory for a `self.homey`; every part of the surface is selectable.

    `awaitable=True` makes settings/i18n return coroutines instead of values, which is the
    half of `compat.resolve`'s contract that fails silently when it is wrong.
    """

    def _make(*, api=None, settings=None, awaitable=False, language="ko",
              i18n_spelling="snake", flow_spelling="snake", expose_shared_api=True,
              expose_reauth=True, expose_logout=True, shared_api_error=None,
              reauth_error=None, homey_language=None, notifications=None):
        return FakeHomey(
            settings=FakeSettings(settings, awaitable=awaitable),
            i18n=FakeI18n(language, spelling=i18n_spelling, awaitable=awaitable),
            app=FakeApp(api, awaitable=awaitable, expose_shared_api=expose_shared_api,
                        expose_reauth=expose_reauth, expose_logout=expose_logout,
                        shared_api_error=shared_api_error, reauth_error=reauth_error),
            flow=FakeFlow(spelling=flow_spelling),
            language=homey_language,
            notifications=notifications,
        )

    return _make


@pytest.fixture
def logged_in_api():
    """An `IparkingApi` already holding a session, built without touching the network.

    Fields are assigned rather than driven through `login()` because these tests are about the
    Homey-facing layers, not the login parser — `test_client.py` owns that. `memb_name` is set
    to the synthetic unit on purpose: it is a home address, and several of these tests assert
    it never reaches a response body or a log line.
    """
    def _build(*, stores=((STOR_SEQ, True),), token="11111111-2222-3333-4444-555555555555",
               memb_name="999동9999호", username="iparking-dev"):
        from iparking_lib.iparking.client import AuthEntry

        logs = []
        api = IparkingApi(username=username, password="synthetic-pw", log=logs.append)
        api.access_token = token
        api.memb_name = memb_name
        api.auth_entries = [AuthEntry(seq, yn) for seq, yn in stores]
        api.logs = logs
        return api
    return _build
