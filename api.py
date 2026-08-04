"""Settings-page API — the app's primary UI, not an afterthought.

Homey has no free-text tile control (the `uiComponent` enum's string-accepting entries are
all read-only) and device settings have no `date` type, so 차량번호 + 방문 예정일 + 등록 has
to be a form. That form is `settings/index.html` + `settings/form.js`; these ten handlers are
everything behind it. They talk to `app.shared_api()` directly, which is why 등록 / 등록 내역 /
취소 all work with **zero devices paired** — the driver exists for Flow-card addressability
and the 주차장명 sensor, not because the UI needs it.

Credentials are app-scoped rather than per-device: one account authenticates to every lot on
it, so storing them once and rotating them once repairs everything together.

## Two conventions worth knowing before editing

**Both calling contracts are tolerated.** `_body()` accepts a request body whether this Homey
build passes it as `body=` or flattens it into kwargs, and `_query()` does the same for a GET's
query string. There is no ground truth for which one a given firmware uses, and betting wrong
is silent: the handler sees empty fields and reports a validation error the user cannot act on.

**Every failure answers with a key, not just prose.** `{"ok": false, "key": ..., "error": ...,
"message": ...}` — `key` is the `locales/{ko,en}.json` key the exception carried, `error` is the
specific Korean sentence it raised, and `message` is `key` rendered in the viewer's language.
The page prefers `message` and falls back to `error`. This is what keeps the page from
re-implementing the vendor's result-code table in JavaScript.

## Never in a response, never in a log

`memb_name` — it is a home address (`999동9999호`), it is required by the register body, and it
appears in **neither**. The password appears in neither. The token's value appears in neither;
`/diagnostics` reports presence and length only, and the token is memory-only — it is held on
the shared client and handed to nothing that persists (see `iparking_lib/const.py`, "Settings
keys"). Plates are masked with `plate.mask_plate` in every log line, and returned unmasked
**only** in a response body, because 등록 내역 is the user reading their own registrations and a
masked table cannot be acted on.
"""

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from iparking_lib import compat, i18n
from iparking_lib.const import (
    MEMBERS_HOST,
    OAUTH_HOST,
    REQUIRED_SCHEMES,
    SCHEMES,
    SETTING_PASSWORD,
    SETTING_USERNAME,
)
from iparking_lib.iparking import codes, dates, tls
from iparking_lib.iparking.client import IparkingError, RegisterUncertain
from iparking_lib.iparking.plate import mask_plate, strip_plate

# --- request/response plumbing ------------------------------------------------


def _body(kwargs: dict) -> dict:
    """Request body, however this Homey build delivers it (some pass `body`, some flatten
    into kwargs)."""
    body = kwargs.get("body")
    return body if isinstance(body, dict) else kwargs


def _query(kwargs: dict) -> dict:
    """Query string of a GET, however this build delivers it.

    The `_body` counterpart, and it exists for the same reason rather than for symmetry:
    `GET /history` is the one handler here that takes parameters on a GET, and the SDK's
    Python surface is no more pinned for `query` than it is for `body`. Several spellings are
    accepted because the cost of guessing wrong is a history table that silently renders
    empty — which looks exactly like an account with no registrations.

    Values arrive as strings from a query string and as native types when flattened, so every
    caller runs them through `_int` / `str` rather than trusting either.
    """
    for name in ("query", "params", "args"):
        candidate = kwargs.get(name)
        if isinstance(candidate, dict):
            return candidate
    return kwargs


def _int(value, default: int = 0) -> int:
    """A tolerant int, because a query string delivers `9001` as `"9001"`."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _mask(value: str) -> str:
    """An identifier safe to put in a diagnostic report someone will paste into an issue.

    Deliberately not `plate.mask_plate`: that one preserves a plate's region/class prefix so a
    user can tell which of their entries a message is about. This is for the account id, where
    there is nothing to tell apart and the goal is only "does the saved id look like the one I
    think I typed".
    """
    if not value:
        return ""
    return f"{value[0]}***{value[-1]}" if len(value) > 2 else "***"


def _log(homey, message: str) -> None:
    for target in (getattr(homey, "app", None), homey):
        log = getattr(target, "log", None)
        if callable(log):
            try:
                log(message)
                return
            except Exception:
                continue


def _i18n_params(exc: Exception) -> dict:
    """Placeholders the locale templates need: `{code}` for `error.unknown`, `{days}` for
    `date_too_far`. Passing extras is harmless — `str.format` ignores unused keywords."""
    return {
        "code": getattr(exc, "code", "") or "",
        "days": getattr(exc, "max_days", dates.MAX_DAYS_AHEAD),
    }


async def _fail(homey, exc: Exception, fallback: str = "") -> dict:
    """The one failure shape every handler returns. See the module docstring."""
    payload = {"ok": False, "error": str(exc) or fallback}
    key = getattr(exc, "key", "")
    if key:
        payload["key"] = key
        rendered = i18n.translate(key, await compat.ui_language(homey), **_i18n_params(exc))
        if rendered != key:
            payload["message"] = rendered
    code = getattr(exc, "code", "")
    if code:
        payload["code"] = code
    return payload


async def _restore_shared(homey) -> None:
    """After a rejected save the shared client holds the rejected credentials; re-point it at
    whatever is still saved so running devices keep working.

    Needed because `reauth` points the shared session at the new credentials *before* it tries
    them. Without the restore, one typo would log every running device out until the app was
    restarted: each caches this object and never re-fetches it, so the next poll would keep
    retrying a password the server has already rejected.

    The counterpart of `pairing._restore_shared`, and unlike that one it reports what happened:
    a restore that fails leaves every device logged out, which is precisely the state worth a
    log line.
    """
    try:
        await compat.shared_api(homey)
    except Exception as exc:
        _log(homey, f"iparking: credentials restore FAILED after failed save: {exc}")
    else:
        _log(homey, "iparking: credentials restored after failed save")


# --- GET /status --------------------------------------------------------------


async def get_status(homey, **kwargs) -> dict:
    """What the settings page needs on load. Never returns the password.

    Two fields here are load-bearing rather than informational:

    **`today_kst`.** The page uses it for the date input's `min` *and* its default value. The
    browser's timezone is never consulted, by design: "today" in this app means today at a
    parking lot in Korea, which is the only thing the vendor's server will accept, and an
    `<input type="date">` value is a bare `yyyy-mm-dd` wall-clock string with no timezone
    attached — so handing the browser a KST date makes both surfaces agree for a user anywhere
    in the world. `max_date` closes the other end of the same window, so the form cannot submit
    a date `resolve_visit_date` is guaranteed to reject.

    **`can_register`.** Read from the **live** login response, on every page load, never from a
    value cached at pairing. The building office can grant the permission after the account was
    first set up, and a user who has just been granted it must not have to re-pair a device to
    see the register card enabled. `None` means "could not be determined", which the page must
    render differently from `False` — one is an unknown, the other is a refusal with a banner
    telling the user to contact the office.
    """
    username = await compat.setting_get(homey, SETTING_USERNAME)
    today = dates.now_kst().date()
    status = {
        "ok": True,
        "configured": bool(username),
        "username": username,
        "today_kst": dates.today_kst(),
        "max_date": (today + timedelta(days=dates.MAX_DAYS_AHEAD)).isoformat(),
        "max_days_ahead": dates.MAX_DAYS_AHEAD,
        "language": await compat.ui_language(homey),
        "logged_in": False,
        "can_register": None,
        "stores": 0,
    }
    if not username:
        return status
    try:
        api = await compat.shared_api(homey)
    except Exception as exc:
        # The date fields survive the failure on purpose: the page still has a form to
        # render, and a date input with no `min` would let the user pick a past date and
        # discover the problem only at submit.
        status.update(await _fail(homey, exc, "연결에 실패했습니다."))
        return status
    status["logged_in"] = api.logged_in
    status["can_register"] = api.can_register
    status["stores"] = len(api.auth_entries)
    return status


# --- POST /credentials, POST /credentials-clear -------------------------------


async def save_credentials(homey, **kwargs) -> dict:
    """Validate an account by logging in, then store it app-scoped.

    Validation goes through the app-wide shared session rather than a throwaway client of its
    own: the vendor's token is minted per account with no refresh endpoint, so a second login
    is a second token and the first one's fate is unverified. Reusing the one session keeps
    "save" from being the button that logs every running device out.

    Nothing is written until the login succeeds, so a typo cannot clobber a working account.
    """
    body = _body(kwargs)
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username or not password:
        return {"ok": False, "key": "need_credentials",
                "error": "아이디와 비밀번호를 입력하세요."}

    try:
        api = await compat.reauth_shared_api(homey, username, password)
    except IparkingError as exc:
        await _restore_shared(homey)
        return await _fail(homey, exc)
    except Exception as exc:  # network, TLS, transport
        await _restore_shared(homey)
        return {"ok": False, "error": f"연결에 실패했습니다: {exc}"}

    if not api.auth_entries:
        # Also a failure path: the login succeeded, so the shared client is already holding
        # credentials this function is about to refuse to store.
        await _restore_shared(homey)
        return {"ok": False, "key": "no_stores",
                "error": "이 계정에는 등록된 주차장 스토어가 없습니다. 관리사무소에 문의하세요."}

    await compat.setting_set(homey, SETTING_USERNAME, username)
    await compat.setting_set(homey, SETTING_PASSWORD, password)
    # Neither the id nor the password, and no token. Counts and the permission flag only.
    _log(homey, f"iparking: account saved (stores={len(api.auth_entries)}, "
                f"can_register={api.can_register})")
    return {
        "ok": True,
        "configured": True,
        "can_register": api.can_register,
        "stores": len(api.auth_entries),
    }


async def clear_credentials(homey, **kwargs) -> dict:
    """계정 삭제 — disable the shared session, then forget the account.

    **The order is load-bearing.** Every device and handler caches the session object it was
    handed and never asks for another one, so clearing the saved account alone would leave
    them polling a live session for an account that no longer exists. Disabling first makes
    their next request raise before it reaches the network — zero HTTP traffic while logged
    out. `IparkingApp.logout` also drops the in-memory token, which is the only copy of it
    anywhere.

    The stored UI language is deliberately kept: it is a display preference, not a credential.
    """
    await compat.app_logout(homey)
    for key in (SETTING_USERNAME, SETTING_PASSWORD):
        await compat.setting_unset(homey, key)
    _log(homey, "iparking: account cleared; shared session disabled")
    return {"ok": True, "configured": False}


# --- GET /diagnostics ---------------------------------------------------------


def _host_report(api) -> list[dict]:
    """The per-host transport policy, and the reached scheme wherever there is evidence for it.

    The asymmetry is a measured fact, not a preference: `oauth.parkingcloud.co.kr` serves TLS
    correctly and carries the password, while `members.iparking.co.kr` answers **every** HTTPS
    request — `/api/members/*` included — with a 301 down to cleartext, so it is addressed over
    plain HTTP deliberately. Reporting it is how a support transcript shows the app knows this
    rather than having drifted into it.

    `final_scheme` is the **post-redirect** scheme, and it is populated only where the app can
    actually vouch for it:

    * **oauth** — a successful login is proof. `client._require_scheme` compares
      `Response.final_scheme` against `REQUIRED_SCHEMES` on the login response and raises if it
      is anything but https, so `logged_in` could not be true otherwise.
    * **members** — deliberately has no required scheme (the day the vendor fixes their TLS, an
      upgrade must *improve* this app rather than break it), so nothing asserts its final scheme
      and nothing records it either. It is reported as `None` rather than echoing the policy
      value back as though it had been measured. What *is* guaranteed is narrower and stated in
      `basis`: `StrictRedirectHandler` permits only an `http -> https` upgrade and logs it, so a
      successful members call ended on http unless that log line is present.
    """
    members_host = getattr(api, "api_host", None) or MEMBERS_HOST
    logged_in = bool(api is not None and api.logged_in)
    return [
        {
            "host": OAUTH_HOST,
            "scheme": SCHEMES[OAUTH_HOST],
            "required_scheme": REQUIRED_SCHEMES.get(OAUTH_HOST),
            "final_scheme": SCHEMES[OAUTH_HOST] if logged_in else None,
            "basis": (
                "carries the password; a successful login proves the final scheme was https"
                if logged_in else "not observed yet — no successful login this session"
            ),
        },
        {
            "host": members_host,
            "scheme": SCHEMES.get(members_host, SCHEMES[MEMBERS_HOST]),
            "required_scheme": REQUIRED_SCHEMES.get(members_host),
            "final_scheme": None,
            "basis": (
                "http by policy: this host 301s every https request down to cleartext. No "
                "required scheme, so the reached scheme is neither asserted nor recorded; the "
                "transport permits only an http -> https upgrade and logs it if it happens."
            ),
        },
    ]


async def diagnostics(homey, **kwargs) -> dict:
    """Non-sensitive status for the settings page's 진단 section.

    Everything here is shaped by the assumption that its output gets pasted into a bug report:
    the account id is masked, the token is reported as presence and length, `memb_name` (a home
    address) is absent entirely, and no plate appears at all.
    """
    username = await compat.setting_get(homey, SETTING_USERNAME)
    api = None
    error = None
    if username:
        try:
            api = await compat.shared_api(homey)
        except Exception as exc:
            error = str(exc)
    token = getattr(api, "access_token", "") or ""
    return {
        "ok": True,
        "configured": bool(username),
        "username_masked": _mask(username),
        "ui_language": await compat.ui_language(homey),
        # Which CA store the TLS handshake to the login host will use. `None` here means
        # certifi did not import, and login is the request that then fails — the Homey Python
        # runtime ships no system CA bundle.
        "ca_source": tls.ca_file(),
        "logged_in": bool(api is not None and api.logged_in),
        # Presence and length. The value is a live 7-day credential that can register and
        # cancel vehicles at a real building, so it is never rendered.
        "token_present": bool(token),
        "token_length": len(token),
        "stores": len(api.auth_entries) if api is not None else 0,
        # Live, like `/status`. A `/diagnostics` that reported a stale flag would be the one
        # place a user looks to find out *why* the register card is disabled.
        "can_register": api.can_register if api is not None else None,
        "hosts": _host_report(api),
        "error": error,
    }


# --- GET /check-connection ----------------------------------------------------


async def check_connection(homey, **kwargs) -> dict:
    """연결 확인 — does the saved account still work, on the session everything else uses?

    Two explicit steps, and both are needed for this to check anything at all. The trap is
    that `shared_api` only logs in when there is no token yet, so once any caller has logged in
    it returns the live object untouched — a naive version of this handler would then read
    fields already in memory and answer `ok` unconditionally, doing real work only in the
    narrow window before the first login. That is worse than useless: it is
    non-deterministically wrong.

      * `login_if_stale(api.auth_gen)` forces the login this button exists to test. Handing it
        the *current* generation makes the "someone already logged in for us" shortcut
        unreachable, while still letting it skip if another caller logs in during the same
        instant. It re-uses the one session rather than opening a second, which is the whole
        point of running it on the shared object.
      * `enumerate_lots()` has to actually come back. That is the one call that proves the
        token the login just minted is *accepted* by the API host, and gating `ok` on it is
        what makes an unreachable server look different from an account with no lots.
    """
    username = await compat.setting_get(homey, SETTING_USERNAME)
    password = await compat.setting_get(homey, SETTING_PASSWORD)
    if not username or not password:
        return {"ok": False, "configured": False, "key": "need_credentials",
                "error": "저장된 계정이 없습니다."}
    try:
        api = await compat.shared_api(homey)
        await api.login_if_stale(api.auth_gen)
        lots = await api.enumerate_lots()
    except IparkingError as exc:
        return {"configured": True, **await _fail(homey, exc)}
    except Exception as exc:
        return {"ok": False, "configured": True, "error": f"연결에 실패했습니다: {exc}"}
    return {
        "ok": True,
        "configured": True,
        "lots": len(lots),
        "stores": len(api.auth_entries),
        "can_register": api.can_register,
    }


# --- POST /language -----------------------------------------------------------


async def set_language(homey, **kwargs) -> dict:
    """Let the settings webview report the language the user is actually looking at.

    Homey's Python i18n resolves the *app's* language rather than the viewer's, so without
    this every sentence `iparking_lib/i18n.py` renders would fall back to English. The webview
    is the only thing that knows.
    """
    body = _body(kwargs)
    language = str(body.get("language", "")).strip()
    if language:
        await compat.remember_ui_language(homey, language)
    return {"ok": True, "language": await compat.ui_language(homey)}


# --- GET /lots ----------------------------------------------------------------


async def get_lots(homey, **kwargs) -> dict:
    """Every lot on the account, across **every** authorization entry.

    Per-lot `can_register` rather than only the account-level summary: an account can hold
    several stores with the permission set differently on each, and the lot selector is where
    that difference has to be visible.
    """
    try:
        api = await compat.shared_api(homey)
        lots = await api.enumerate_lots()
    except IparkingError as exc:
        return await _fail(homey, exc)
    except Exception as exc:
        return {"ok": False, "error": f"주차장 목록을 가져오지 못했습니다: {exc}"}
    return {
        "ok": True,
        "can_register": api.can_register,
        "lots": [
            {
                "lot_id": lot.lot_id,
                "park_seq": lot.park_seq,
                "stor_seq": lot.stor_seq,
                "park_name": lot.park_name,
                "can_register": lot.can_register,
            }
            for lot in lots
        ],
    }


# --- POST /register -----------------------------------------------------------


async def register_visitor(homey, **kwargs) -> dict:
    """방문차량 등록. **This writes to a real building's access-control system.**

    Everything consequential lives in `client.register()` — zero retries, the write ceiling,
    the fresh-budget recovery re-query, the status-filtered existence predicate. This handler
    only shapes the request and the answer, and there are exactly two things it must not get
    wrong:

    * **`already_registered` is not a failure.** It comes back `ok: true` with a distinct
      `outcome`, because re-entering a plate that is already registered is the single most
      likely real result of a first use and reporting it as an error teaches the user the app
      is broken on their very first try.
    * **`RegisterUncertain` must not invite a retry.** It carries `uncertain: true` so the page
      can render it as its own state and withhold the retry affordance. A retry is what turns
      one uncertain write into two real registrations at a building.

    The response echoes the resolved date back (`date`, plus `ambiguous`) because a Homey Flow
    `date` argument in `mm-dd-yyyy` order is shape-identical to `dd-mm-yyyy` — showing which
    day was actually used is what makes a misparse visible on first use instead of at a closed
    gate.
    """
    body = _body(kwargs)
    car_number = str(body.get("car_number") or body.get("carNumber") or "")
    visit_date = str(body.get("visit_date") or body.get("visitDate") or "").strip()
    park_seq = _int(body.get("park_seq") or body.get("parkSeq"))
    stor_seq = _int(body.get("stor_seq") or body.get("storSeq"))
    if not park_seq or not stor_seq:
        return {"ok": False, "error": "주차장을 선택하세요."}

    try:
        api = await compat.shared_api(homey)
        result = await api.register(
            car_number=car_number,
            park_seq=park_seq,
            stor_seq=stor_seq,
            visit_date=visit_date or None,
            memo=str(body.get("memo") or ""),
            mobile=str(body.get("mobile") or ""),
        )
    except RegisterUncertain as exc:
        _log(homey, f"iparking: register outcome UNCERTAIN for {mask_plate(car_number)}")
        return {**await _fail(homey, exc), "uncertain": True}
    except (IparkingError, ValueError) as exc:
        # `ValueError` covers `InvalidPlateError` and `DateError`, which are user-input
        # verdicts rather than API failures and carry their own i18n keys all the same.
        return await _fail(homey, exc)
    except Exception as exc:
        return {"ok": False, "error": f"등록에 실패했습니다: {exc}"}

    outcome = str(result)
    payload = {
        "ok": outcome in (codes.OUTCOME_OK, codes.OUTCOME_ALREADY_REGISTERED),
        "outcome": outcome,
        # Stripped, not validated: the page echoes this back into the input so requirement 7's
        # whitespace removal is *visible*. Validation already happened inside `register`, so
        # doing it again here could only change which error the user sees.
        "car_number": strip_plate(car_number),
        "api_date": str(result.api_date),
        "date": dates.format_kst_human(result.api_date, await compat.ui_language(homey)),
        "ambiguous": result.ambiguous,
    }
    if outcome != codes.OUTCOME_OK:
        # `already_registered` and `register_failed` are themselves locale keys (item 8).
        payload["key"] = outcome
        payload["message"] = i18n.translate(outcome, await compat.ui_language(homey))
    _log(homey, f"iparking: register {mask_plate(car_number)} -> {outcome}")
    return payload


# --- GET /history -------------------------------------------------------------


def _api_date_or_none(value: str) -> str | None:
    """A `yyyy-mm-dd` window bound from the page → `yyyyMMdd`, or `None` for "use the default".

    `to_api_date` rather than `resolve_visit_date`: the 등록 내역 window legitimately reaches
    three months into the past, and the visit-date policy has no business being applied to a
    read.
    """
    text = (value or "").strip()
    return str(dates.to_api_date(text)) if text else None


async def get_history(homey, **kwargs) -> dict:
    """등록 내역 for one lot. The 취소 button hangs off `invt_seq` in these rows.

    `is_active` is computed per row and it is the field the table must key on, **not** the
    row's presence: `DELETE /invitations/{seq}` does not remove a row (verified live), it flips
    `inot_status` to `CANCEL` while the row keeps its `invt_seq` and stays in the list. A table
    that looked for a cancelled row to *disappear* would report a working 취소 as broken.

    Plates come back unmasked here, and only here: this is the user reading their own
    registrations, and a masked table cannot be acted on. Every log line still masks.

    Rows leave here **newest visit first** — see `_newest_first`. The vendor answers
    oldest-first, which puts the rows a user actually cares about (the visits that have not
    happened yet) at the bottom of the table.
    """
    query = _query(kwargs)
    park_seq = _int(query.get("park_seq") or query.get("parkSeq"))
    stor_seq = _int(query.get("stor_seq") or query.get("storSeq"))
    if not park_seq or not stor_seq:
        return {"ok": False, "error": "주차장을 선택하세요."}
    try:
        start_date = _api_date_or_none(str(query.get("start_date") or ""))
        end_date = _api_date_or_none(str(query.get("end_date") or ""))
    except ValueError as exc:  # DateError
        return await _fail(homey, exc)

    try:
        api = await compat.shared_api(homey)
        rows = await api.history(
            park_seq=park_seq,
            stor_seq=stor_seq,
            start_date=start_date,
            end_date=end_date,
            # Stripped so a trailing space in the filter box does not silently match nothing.
            car_number=strip_plate(str(query.get("car_number") or "")),
        )
    except IparkingError as exc:
        return await _fail(homey, exc)
    except Exception as exc:
        return {"ok": False, "error": f"등록 내역을 가져오지 못했습니다: {exc}"}

    language = await compat.ui_language(homey)
    return {
        "ok": True,
        "rows": [
            {
                "invt_seq": row.invt_seq,
                "car_number": row.car_number,
                "invitation_date": row.invitation_date,
                "invitation_date_human": _human_date(row.invitation_date, language),
                "status": row.status,
                "is_active": row.is_active,
                "park_name": row.park_name,
            }
            for row in _newest_first(rows)
        ],
    }


def _newest_first(rows: list) -> list:
    """등록 내역 ordered by visit date descending, `invt_seq` descending within a date.

    **Sorted here rather than in `settings/form.js`** so the order is a property of the
    handler's answer instead of one renderer's habit: the settings table today, the v0.1.1
    widget and any later Flow consumer all read this one list, and each of them re-deriving
    the order is how two views of the same account end up disagreeing about which
    registration is the latest. It also keeps `form.js` a renderer — `paginate()` slices
    whatever it is handed, so page 1 is now the newest page with no change there at all.

    `invitation_date` is the wire format `yyyyMMdd`, so a plain string sort **is** a date
    sort — no parsing, and therefore no way for the malformed row `_human_date` already
    tolerates to raise here or to be dropped.

    `invt_seq` descending is the tiebreaker, because several registrations on one date is the
    ordinary case (a household registering three cars for the same visit) and the vendor's
    own order within a date is not something to rely on. `invt_seq` is server-assigned and
    increasing, so the highest is the one registered last.
    """
    return sorted(
        rows,
        key=lambda row: (str(row.invitation_date), _int(row.invt_seq)),
        reverse=True,
    )


def _human_date(api_date: str, language: str) -> str:
    """`"20260805"` → `"2026-08-05 (수)"`, or the raw value if it will not parse.

    Falling back rather than raising: a row the vendor returns with an unexpected date is data
    we do not get to reject, and one malformed row must not empty the whole table.
    """
    try:
        return dates.format_kst_human(api_date, language)
    except Exception:
        return api_date


# --- POST /cancel -------------------------------------------------------------


async def cancel_visitor(homey, **kwargs) -> dict:
    """취소 one registration by `invt_seq`.

    The caller should re-read `/history` afterwards and look at `is_active`, not at whether the
    row is gone — see `get_history`. Cancelling is a write, but unlike registering it is the
    *removal* of an access grant, so it carries none of the register path's ceremony.
    """
    body = _body(kwargs)
    invt_seq = _int(body.get("invt_seq") or body.get("invtSeq"))
    if not invt_seq:
        return {"ok": False, "error": "취소할 등록을 선택하세요."}
    try:
        api = await compat.shared_api(homey)
        await api.cancel(invt_seq)
    except IparkingError as exc:
        return await _fail(homey, exc)
    except Exception as exc:
        return {"ok": False, "error": f"취소에 실패했습니다: {exc}"}
    return {"ok": True, "invt_seq": invt_seq}
