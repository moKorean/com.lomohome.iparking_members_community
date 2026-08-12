"""Homey Python SDK accessors that tolerate either calling contract.

The SDK's Python surface is only partly documented, so there is no ground truth for
whether settings/i18n return values or coroutines. Rather than betting on one, await
whatever comes back if it is awaitable. Getting this wrong is silent: an un-awaited
`settings.set()` coroutine looks like a successful write and stores nothing.

Copied, near-verbatim, from the sibling app `com.lomohome.navien` (`navien_lib/compat.py`,
itself copied from `com.lomohome.localthings`) — this layer is vendor-neutral. The only
iparking-specific parts are the credential fields updated in place below, because
`IparkingApi` carries a different set of them than `NavienApi` does.
"""

import inspect

# Whether `shared_api`'s private-session fallback has ever fired on a real runtime. It is
# the one branch that silently opens a second account session, and the whole
# one-session-per-account design assumes it is dead code — so the warning fires once per
# app start, loudly, and nothing else changes.
_FALLBACK_WARNED = False

# The one fallback session, cached. Without the cache every `shared_api` call on a runtime
# without `homey.app` would build a *new* IparkingApi, so N devices × every retry would
# open N logins against an account whose token is per-account — each new login invalidating
# the previous device's. Caching makes the degraded mode cost one session instead, i.e. as
# close to the shared-session design as this branch can get. Kept module-level, like the
# warning, because there is no app object here to hang it on.
_FALLBACK_API = None


async def resolve(value):
    """Return `value`, awaiting it first if it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


async def setting_get(homey, key: str, default: str = "") -> str:
    try:
        value = await resolve(homey.settings.get(key))
    except Exception:
        return default
    return default if value is None else value


async def setting_set(homey, key: str, value) -> None:
    await resolve(homey.settings.set(key, value))


async def setting_unset(homey, key: str) -> None:
    """Remove a setting, falling back to an empty value.

    Not every build exposes unset(); an empty string reads the same to every
    consumer in this app.
    """
    try:
        await resolve(homey.settings.unset(key))
    except Exception:
        await resolve(homey.settings.set(key, ""))


async def language(homey, default: str = "en") -> str:
    """Two-letter UI language, or `default` if it can't be determined."""
    for get in (
        lambda: homey.i18n.get_language(),
        lambda: homey.i18n.getLanguage(),
        lambda: homey.language,
    ):
        try:
            value = await resolve(get())
        except Exception:
            continue
        if isinstance(value, str) and value:
            return value[:2].lower()
    return default


def repoint_credentials(api, username: str, password: str) -> None:
    """Point an existing `IparkingApi` at new credentials **in place**.

    In place rather than replaced, because every device and every settings-page handler
    caches the object it was handed and never asks for another one; building a new client
    would leave them all talking to the old session. Clearing the session fields is what
    forces the next request to log in with the new credentials.

    Shared with `IparkingApp._client`, which is the whole reason it is a function rather
    than a method on either: two copies of "which fields go with a credential change" is
    one too many, and the field they would forget is the same one both times.

    `memb_name` and `auth_entries` go with the token deliberately: they describe *the
    account that minted it*. Leaving them behind would let a freshly repointed session
    answer `can_register` — and put a home address into a register body — for the previous
    account.
    """
    api.username = username
    api.password = password
    api.access_token = ""
    api.memb_name = ""
    api.auth_entries = []


async def shared_api(homey):
    """Return the app-wide shared IparkingApi (one session per account), logging in if
    needed. Falls back to a private session if the app object can't be reached, so a
    device still works on a runtime that doesn't expose `homey.app`."""
    global _FALLBACK_WARNED, _FALLBACK_API
    app = getattr(homey, "app", None)
    getter = getattr(app, "shared_api", None) if app is not None else None
    if getter is not None:
        return await resolve(getter())

    from .const import SETTING_PASSWORD, SETTING_USERNAME
    from .iparking.client import IparkingApi

    if not _FALLBACK_WARNED:
        _FALLBACK_WARNED = True
        log = getattr(app, "log", print) if app is not None else print
        log("iparking: WARNING shared_api fallback — homey.app exposes no shared_api, so "
            "this caller is opening its OWN account session. The vendor's token is per "
            "account, so a second login invalidates the first one's token.")

    username = await setting_get(homey, SETTING_USERNAME)
    password = await setting_get(homey, SETTING_PASSWORD)
    api = _FALLBACK_API
    if api is None:
        api = _FALLBACK_API = IparkingApi(
            username=username, password=password,
            log=getattr(app, "log", print) if app is not None else print,
        )
    elif api.username != username or api.password != password:
        repoint_credentials(api, username, password)
    # The other half of `app_logout`'s disable, and it has to live here: a runtime that
    # reaches this branch has no `homey.app`, hence no `reauth` to re-enable the session
    # the way IparkingApp does. Saved credentials being present again is the only re-login
    # signal this branch gets — and `clear_credentials` unsets them immediately after
    # logging out, so a cleared account stays refused.
    if username and password:
        api.disabled = False
    if not api.logged_in:
        await api.login()
    return api


async def reauth_shared_api(homey, username: str, password: str):
    """Validate credentials by pointing the app-wide shared session at them and logging
    in; raises on failure. Falls back to a throwaway login if the app can't be reached.

    Returns the validated session so the caller can read `auth_entries` / `can_register`
    off the session it just validated instead of opening a second one. Callers that only
    care about success (pairing's repair view) can keep ignoring it.
    """
    app = getattr(homey, "app", None)
    fn = getattr(app, "reauth", None) if app is not None else None
    if fn is not None:
        return await resolve(fn(username, password))

    from .iparking.client import IparkingApi

    api = IparkingApi(username=username, password=password,
                      log=getattr(app, "log", print) if app is not None else print)
    await api.login()
    return api


async def app_logout(homey) -> None:
    """Disable the app-wide shared session, so callers holding it stop making requests.

    A no-op where `homey.app` exposes no `logout` — the same tolerance every accessor here
    applies, and the shared-session design is already absent on such a runtime.
    """
    app = getattr(homey, "app", None)
    fn = getattr(app, "logout", None) if app is not None else None
    if fn is not None:
        await resolve(fn())
    # `_FALLBACK_API` is a session on the same account, cached and handed out exactly like
    # the app-level one, so a logout that skips it leaves the degraded runtime still
    # polling a deleted account. This is asserted dead code — that is what the
    # `_FALLBACK_WARNED` instrumentation above is for — so it closes a consistency gap
    # rather than a live defect, but the invariant is "logout stops every session we own".
    if _FALLBACK_API is not None:
        _FALLBACK_API.logout()


def flow_card(homey, kind: str, card_id: str):
    """Fetch a flow card, tolerating either the snake_case or camelCase SDK spelling.

    `kind` is "action" or "condition". As with settings/i18n, the Python surface isn't
    pinned, so we try both method names rather than betting on one.
    """
    getters = {
        "action": ("get_action_card", "getActionCard"),
        "condition": ("get_condition_card", "getConditionCard"),
    }[kind]
    for name in getters:
        fn = getattr(homey.flow, name, None)
        if fn is not None:
            return fn(card_id)
    raise AttributeError(f"Homey flow has no {kind}-card getter")


def devices(homey, driver_id: str) -> list:
    """Every paired device of one driver, or `[]` on any runtime that will not say.

    Used for exactly one thing: letting a settings-page history fetch update the 오늘 등록 count
    on the matching device tile, at **no extra request** (see `device.note_history`). That makes
    it a courtesy, so every step is optional and nothing raises — a runtime that exposes no
    driver registry costs the user a tile that is up to one poll stale, and nothing else. Returning
    `[]` and letting the caller do nothing is the correct failure here.

    Both spellings of both accessors, on the same reasoning as `flow_card`. `get_driver` /
    `get_devices` are the shapes the Python SDK actually ships.
    """
    manager = getattr(homey, "drivers", None)
    if manager is None:
        return []
    for getter in ("get_driver", "getDriver"):
        fn = getattr(manager, getter, None)
        if fn is None:
            continue
        try:
            driver = fn(driver_id)
        except Exception:
            return []
        for lister in ("get_devices", "getDevices"):
            listing = getattr(driver, lister, None)
            if listing is None:
                continue
            try:
                return list(listing())
            except Exception:
                return []
        return []
    return []


def register_run_listener(card, fn) -> None:
    for name in ("register_run_listener", "registerRunListener"):
        reg = getattr(card, name, None)
        if reg is not None:
            reg(fn)
            return
    raise AttributeError("flow card has no run-listener registrar")


async def ui_language(homey, default: str = "ko") -> str:
    """The language to write user-facing messages in.

    Prefers what a webview reported, because Homey's Python i18n resolves the *app's*
    language rather than the viewer's and returns 'en' regardless (see `i18n.py`). Falls
    back to that accessor anyway, in case a future firmware fixes it.

    The default is Korean here rather than navien's English: the service, the building and
    every vendor-supplied string in this app are Korean, so an unresolvable language is
    much more likely to be a Korean user than an English one.
    """
    from .const import SETTING_LANGUAGE

    reported = await setting_get(homey, SETTING_LANGUAGE)
    if reported:
        return reported[:2].lower()
    return await language(homey, default)


async def remember_ui_language(homey, value: str) -> None:
    """Store a language a webview resolved, if it looks like one."""
    from .const import SETTING_LANGUAGE

    code = str(value or "")[:2].lower()
    if not code.isalpha() or len(code) != 2:
        return
    if await setting_get(homey, SETTING_LANGUAGE) == code:
        return
    await setting_set(homey, SETTING_LANGUAGE, code)
