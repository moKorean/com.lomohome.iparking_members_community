"""Shared pairing + repair handlers for the `visitcar` driver.

Pairing reuses the app-wide shared session (one login per account — see `app.py`'s
`shared_api`) and enumerates every lot the account can see. Login itself is **not** part of
pairing: the account is entered once in the app settings, so the start view only checks
whether one is saved and, if so, jumps straight to the device list. Mirrors
`navien_lib/pairing.py`, whose `check_session` → `list_devices` handler shape this keeps.

Repair (after a password change) re-enters the account: it validates the new credentials
against the shared session, and only writes them to app settings once they check out — so a
wrong entry never overwrites a working account. Because the shared client is repointed in
place (`compat.repoint_credentials`), running devices pick up the new credentials without a
re-init.

Every network call is bounded by a hard timeout so a stalled request surfaces as a visible
error rather than an endless spinner — but the bound has to be larger than the worst case of
the thing it bounds, or it is not a safety net, it is a second failure mode. See
`const.PAIR_TIMEOUT_S`, which is derived arithmetic rather than a round number.

**A timeout here does not cancel the request.** `asyncio.wait_for` cancels the *await*, not
the `run_in_executor` thread underneath it, so an expired budget leaves the HTTP call still
in flight: it can still finish and write `access_token` / `auth_entries` onto the shared
session after the pair view has already reported failure. On this pairing path that orphan is
harmless — every call it makes is a read, and a late-arriving login is a session we wanted
anyway. It is emphatically **not** harmless on the register path, and this is the same
mechanism: it is exactly why `client.register` answers a timeout with a *recovery re-query*
instead of a second write. Read that method's docstring before changing any budget here, so
the two files' reasoning does not drift apart.
"""

import asyncio

from iparking_lib import compat
from iparking_lib.const import PAIR_TIMEOUT_S, SETTING_PASSWORD, SETTING_USERNAME
from iparking_lib.iparking.client import IparkingAuthError, NeedCredentialsError

_SLOW_LOGIN = "로그인 응답이 지연됩니다. 네트워크를 확인하고 다시 시도하세요."
_NEED_LOGIN = "먼저 앱 설정에서 아이파킹 계정으로 로그인하세요."


def _payload(data, kwargs) -> dict:
    """The credentials dict, however this Homey build delivers it — positional, or wrapped
    in a `body`/`data` kwarg. Mirrors `api._body`'s tolerance so the form never silently
    sees empty fields."""
    for candidate in (data, kwargs.get("body"), kwargs.get("data"), kwargs):
        if isinstance(candidate, dict) and ("username" in candidate or "password" in candidate):
            return candidate
    return data if isinstance(data, dict) else {}


def install(driver, session, build_devices) -> None:
    """Wire the pair handlers onto `session` (`check_session` + `list_devices`).

    `build_devices(api)` maps the shared session to Homey device payloads. It takes the
    session rather than a lot list because the driver owns the mapping — `data.id`, the
    store contents and the device name are all its business — and it is `async` because
    enumerating lots is one request per store.
    """

    async def on_check_session(data=None, **kwargs) -> dict:
        username = await compat.setting_get(driver.homey, SETTING_USERNAME)
        password = await compat.setting_get(driver.homey, SETTING_PASSWORD)
        ready = bool(username and password)
        driver.log(f"pair: check_session ready={ready}")
        return {"ready": ready, "reason": "" if ready else _NEED_LOGIN}

    async def on_list_devices(data=None, **kwargs) -> list:
        try:
            api = await asyncio.wait_for(
                compat.shared_api(driver.homey), timeout=PAIR_TIMEOUT_S
            )
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        except NeedCredentialsError as exc:
            raise Exception(_NEED_LOGIN) from exc
        except Exception as exc:
            # The reason is logged rather than shown: a login failure here is almost always
            # a stale saved password, and `_NEED_LOGIN` points at the one screen that fixes
            # it. The log line is what a support transcript needs.
            driver.log(f"pair: shared login failed: {exc}")
            raise Exception(_NEED_LOGIN) from exc
        try:
            devices = await asyncio.wait_for(build_devices(api), timeout=PAIR_TIMEOUT_S)
        except TimeoutError:
            raise Exception(_SLOW_LOGIN) from None
        driver.log(f"pair: found {len(devices)} lot(s)")
        return devices

    session.set_handler("check_session", on_check_session)
    session.set_handler("list_devices", on_list_devices)


def install_repair(driver, session) -> None:
    """Wire the repair handler: validate new credentials, then store them."""

    async def on_login(data=None, **kwargs) -> bool:
        body = _payload(data, kwargs)
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        # Presence only. The password is never logged in any form, and the id is not logged
        # either — it is the account holder's, and repair logs get pasted into issues.
        driver.log(f"repair: login attempt (id={'set' if username else 'empty'})")
        if not username or not password:
            raise Exception("아이디와 비밀번호를 입력하세요.")
        try:
            # Validated against the shared session, which `reauth` repoints in place.
            # Settings aren't touched yet, so a wrong entry can't clobber the working
            # account.
            await asyncio.wait_for(
                compat.reauth_shared_api(driver.homey, username, password),
                timeout=PAIR_TIMEOUT_S,
            )
        except IparkingAuthError as exc:
            await _restore_shared(driver.homey)
            raise Exception(str(exc)) from exc
        except TimeoutError:
            await _restore_shared(driver.homey)
            raise Exception(_SLOW_LOGIN) from None
        except Exception as exc:
            await _restore_shared(driver.homey)
            raise Exception(f"로그인에 실패했습니다: {exc}") from exc

        await compat.setting_set(driver.homey, SETTING_USERNAME, username)
        await compat.setting_set(driver.homey, SETTING_PASSWORD, password)
        driver.log("repair: credentials updated; shared session refreshed")
        return True

    session.set_handler("login", on_login)


async def _restore_shared(homey) -> None:
    """After a failed repair the shared client holds the rejected credentials; re-point it
    at whatever is still saved so the running devices keep working.

    `reauth` repoints the shared session *before* it tries the credentials, so without this
    one typo would leave every device polling with a password the server just rejected.
    """
    try:
        await compat.shared_api(homey)
    except Exception as exc:
        # Worth a line: a restore that fails leaves every device logged out, which is
        # precisely the state a support transcript needs to show.
        log = getattr(getattr(homey, "app", None), "log", None)
        if callable(log):
            log(f"iparking: credentials restore FAILED after failed repair: {exc}")
