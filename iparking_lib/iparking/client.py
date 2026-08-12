"""`IparkingApi` — the whole vendor API, with no Homey SDK anywhere in it.

Shape mirrors `navien_lib/navien/api.py`: a dataclass holding one session, blocking work
pushed to an executor, one lock, a monotonic `auth_gen` so concurrent callers cannot
double-login, and a `transport` seam for tests. Read that file first if this one looks
unfamiliar.

## The three things in here that are not ordinary plumbing

**1. Transport is asymmetric by host, and the scheme never comes from the server.**
`oauth.parkingcloud.co.kr` is HTTPS-only (it carries the password, and it serves TLS
correctly). `members.iparking.co.kr` is addressed over plain HTTP *deliberately*, because
it 301s every HTTPS request — `/api/members/*` included — down to cleartext. The host is
read from `auth_data.operation_company[0].domain`; the **scheme never is**, because that
value literally reads `"http://members.iparking.co.kr"` and trusting it would let the
server pick our transport. See `const.SCHEMES`.

**2. The `access_token` is memory-only.** It is a 7-day credential that can register and
cancel vehicles at a real building, and it crosses the wire in cleartext. It lives on this
object and is never handed to `homey.settings`, so it cannot reach a hub backup or a
settings export. Nothing here logs its value — presence and length only.

**3. `register()` never retries — and everything else does.** Everything about that method is
shaped by one failure: a vehicle actually registered at a real building after the user was
told it failed. See its docstring. If you are here to "add a retry for reliability", read it
first; on that endpoint the retry is the failure, not the fix.

Everywhere else the retry *is* the fix, because the members host resets roughly **30 %** of
plain-HTTP connections (measured; `docs/RECON.md`). So every call to `transport.request` in
this file names its `attempts=` explicitly, and the number is chosen **per endpoint by
semantics, never per HTTP method** — this API serves reads over POST, so a method-shaped rule
would retry the register. The policy, in one place:

| Endpoint | Attempts | Why |
|---|---|---|
| oauth login | `LOGIN_ATTEMPTS` | a retry just mints another token |
| `POST /invitations/list` | `READ_ATTEMPTS` | read |
| `POST /parkinglot/list/{seq}` | `READ_ATTEMPTS` | read |
| `GET /invitations/{seq}` | `READ_ATTEMPTS` | read |
| `DELETE /invitations/{seq}` | `READ_ATTEMPTS` | idempotent — re-cancelling gives `13001` |
| recovery re-query | `RECOVERY_ATTEMPTS` | it resolves the uncertainty (3) creates |
| **`POST /invitations`** | **1** | a reset cannot tell "never arrived" from "arrived, reply lost" |

And a **timeout is not a reset**: `transport.ConnectionLost` is retryable because the socket
is gone and nothing is in flight, while a timeout means the request may still land. Widening
one into the other is what would make the write retryable by accident.

## Never logged

The password in any form. The `access_token` value. Request bodies, encrypted or plain.
`memb_name` — it is a home address. Plates are masked (`12가****`) because diagnostic
output gets pasted into issues. `_safe` and `plate.mask_plate` are the only routes from
this data to a log line.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from iparking_lib.const import (
    ACTIVE_STATUSES,
    API_VERSION,
    CLIENT_OS_TYPE,
    HISTORY_DAYS_AHEAD,
    HISTORY_DAYS_BACK,
    HISTORY_MAX_PAGES,
    HISTORY_PAGE_SIZE,
    LOGIN_ATTEMPTS,
    MAX_WRITES_PER_HOUR,
    MEMBERS_BASE_PATH,
    MEMBERS_HOST,
    OAUTH_HOST,
    OAUTH_PATH,
    READ_ATTEMPTS,
    RECOVERY_ATTEMPTS,
    RECOVERY_SLEEP_S,
    RECOVERY_TIMEOUT_S,
    REGISTER_TIMEOUT_S,
    REQUIRED_SCHEMES,
    SCHEMES,
    WRITE_WINDOW_S,
)
from iparking_lib.iparking import codes, crypto, dates
from iparking_lib.iparking.plate import mask_plate, normalize_plate, strip_plate
from iparking_lib.iparking.transport import (
    NetworkError,
    RedirectRefused,
    Response,
    Transport,
    TransportError,
)

LOGGED_OUT = "로그아웃되었습니다. 앱 설정에서 아이파킹 계정으로 다시 로그인하세요."


# --- errors -----------------------------------------------------------------


class IparkingError(Exception):
    """Base for everything this module raises.

    `key` is an i18n key in `locales/{ko,en}.json`, so the settings page and the Flow card
    can render a translated sentence without matching on message text.
    """

    key = "error.unknown"


class IparkingAuthError(IparkingError):
    """Credentials were rejected. Retrying will not fix it."""

    key = "error.login_error"


class IparkingApiError(IparkingError):
    """The server returned a verdict we are passing on.

    `code` is the vendor's `result` value, carried alongside the message rather than
    embedded in it, so callers can branch on the verdict without parsing prose.
    """

    def __init__(self, message: str, code: str = "", key: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.key = key or codes.result_key(code)


class NeedCredentialsError(IparkingError):
    """No id/password configured yet."""

    key = "need_credentials"


class NotPermittedError(IparkingError):
    """This account may not register visitor vehicles.

    `invitation_register_authorization_yn != "Y"`. Checked against the **live** login
    response rather than a value cached at pairing, because the building office can grant
    the permission later and a paired device must then start working without re-pairing.
    """

    key = "not_permitted"


class WriteBudgetError(IparkingError):
    """The in-process hourly write ceiling refused this attempt."""

    key = "write_budget"


class RegisterUncertain(IparkingError):
    """The registration's outcome is **unknown**, and this is not a failure report.

    Raised when the write errored or timed out and the recovery re-query could not settle
    the question — a miss, or a `CANCEL`-only match.

    Its message deliberately **does not invite a retry**, and that is the entire point of
    the class existing separately from `register_failed`. A retry is what converts one
    uncertain write into two real registrations at a building. So the text says the outcome
    is unknown and points at the vendor's web UI, which is the only surface that can
    actually answer the question.
    """

    key = "register_uncertain"


# --- value objects ----------------------------------------------------------


@dataclass(frozen=True)
class AuthEntry:
    """One `invitation_authorization_list` entry: a store, and whether it may register.

    Modelled per entry rather than collapsed to `[0]` because an account can hold several,
    with the permission set differently on each. Collapsing would silently gate a
    multi-store account on whichever store happened to sort first.
    """

    stor_seq: int
    can_register: bool


@dataclass(frozen=True)
class Lot:
    """One parking lot, as paired.

    `lot_id` is the vendor's globally-qualified identifier (`"1160009001"`) and becomes the
    device's `data.id`; bare `park_seq` uniqueness across stores is unestablished, so it is
    not used as the key.
    """

    lot_id: str
    park_seq: int
    park_name: str
    stor_seq: int
    can_register: bool


class RegisterResult(str):
    """The outcome word, which also remembers the date the write actually used.

    A `str` subclass for the same reason `dates.ApiDate` is one: every caller comparing
    `result == "ok"` keeps working, and so does anything that logs or serialises it. The
    attributes exist for one job — item 7's success notification echoes
    `dates.format_kst_human(result.api_date)` back to the user, which is what makes a
    misparsed Flow `date` argument visible on **first use** rather than at a closed gate.

    `ambiguous` is carried through rather than swallowed: a 2-2-4 Flow date whose two
    leading fields are both ≤ 12 was resolved day-first by policy, not by evidence, and the
    surface that shows it to the user is the mitigation.
    """

    api_date: str
    ambiguous: bool

    def __new__(cls, outcome: str, api_date) -> RegisterResult:
        self = super().__new__(cls, outcome)
        self.api_date = str(api_date)
        self.ambiguous = bool(getattr(api_date, "ambiguous", False))
        return self


@dataclass(frozen=True)
class HistoryRow:
    """One 등록 내역 row, with `car_number` already normalized for comparison.

    `car_number` went through `strip_plate` (never `normalize_plate`) on the way in: this is
    server data, and a plate shape the vendor accepts but our validator does not must not
    turn a status lookup into an exception.
    """

    invt_seq: int
    car_number: str
    invitation_date: str
    status: str
    park_name: str

    @property
    def is_active(self) -> bool:
        """Registered, as opposed to cancelled. `CANCEL` is excluded — see
        `const.ACTIVE_STATUSES` for both directions of harm in getting this wrong."""
        return self.status in ACTIVE_STATUSES


def count_registered_on(rows, api_date: str) -> int:
    """How many of `rows` are **registered vehicles for `api_date`** — the 오늘 등록 count.

    Two filters, and both are load-bearing:

    * **`is_active`**, i.e. `const.ACTIVE_STATUSES` — the same predicate the register path's
      recovery re-query uses, reusing that set rather than spelling it a second time. 취소 does
      not delete a row, it flips `inot_status` and the row stays, so a day's rows are frequently
      mostly `CANCEL`: on the maintainer's own account, counting them showed **6** where the
      honest answer was **1**.
    * **the date**, checked client-side even though the request already asks for a one-day
      window. Same reasoning as `_recover_register` re-checking the plate the server was asked
      to filter on: the vendor's filtering rules were never characterised, and a count is a bare
      number on a tile — nothing about it would reveal that it had quietly covered three months.

    `invitation_date` is the wire format `yyyyMMdd`, exactly what `dates.today_api()` returns, so
    this is a string comparison that cannot raise on a row the vendor sent malformed.

    Lives here, in the `homey`-free client, so the counting rule is unit-testable and has one
    home. It is the only counting logic in the app — `aggregate_counts` below is the vendor's own
    aggregate, which is empty in practice and is not it.
    """
    wanted = str(api_date)
    return sum(1 for row in rows if row.is_active and str(row.invitation_date) == wanted)


# --- the client -------------------------------------------------------------


@dataclass
class IparkingApi:
    """One logged-in session against one account.

    Constructed once per app and shared (`app.IparkingApp.shared_api`), because the token is
    per-account and a second login invalidates the first.
    """

    username: str = ""
    password: str = ""
    log: object = print

    #: **Memory-only.** Never persisted; see the module docstring.
    access_token: str = ""

    #: `memb_name` — a home address. Needed for the register body's `userName`, and
    #: therefore never logged.
    memb_name: str = ""

    #: Every `invitation_authorization_list` entry, in order.
    auth_entries: list[AuthEntry] = field(default_factory=list)

    #: Host for `/api/members/*`, taken from `operation_company[0].domain`. The **scheme**
    #: is not taken from there — see `const.SCHEMES`.
    api_host: str = MEMBERS_HOST

    #: Kill flag, set by `logout()` / `clear_credentials`. This object is the only seam the
    #: app has on a running device (`homey` exposes no device registry in this Python
    #: surface), and every device caches the session it was handed, so flipping this is what
    #: actually stops a logged-out account's traffic.
    disabled: bool = False

    #: Test seam. Replaces the socket, not the error handling: a `Transport` whose handler
    #: raises `URLError` still goes through the real `NetworkError` conversion, which is
    #: what makes the classification assertions meaningful.
    transport: Transport | None = None

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _auth_gen: int = 0
    _write_times: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        # One opener for the object's lifetime. Built here rather than lazily inside the
        # executor thread, where two requests starting together would each build their own.
        if self.transport is None:
            self.transport = Transport(log=self.log)

    # --- session ------------------------------------------------------------

    @property
    def auth_gen(self) -> int:
        """The login generation, captured *before* a request and handed back to
        `login_if_stale`. Public so the device layer can dedup without touching a private
        field."""
        return self._auth_gen

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)

    @property
    def stor_seq(self) -> int | None:
        """The default store — the first authorization entry. `None` before login."""
        return self.auth_entries[0].stor_seq if self.auth_entries else None

    @property
    def can_register(self) -> bool:
        """Whether the **default** store permits registration.

        Per-store gating goes through `_entry_for`; this is the settings page's summary for
        the single-store case, which is every account we can test live.
        """
        return bool(self.auth_entries and self.auth_entries[0].can_register)

    def _refuse_if_disabled(self) -> None:
        if self.disabled:
            raise IparkingError(LOGGED_OUT)

    def logout(self) -> None:
        """Drop the session and refuse further traffic from this object."""
        self.access_token = ""
        self.memb_name = ""
        self.auth_entries = []
        self.disabled = True
        self.log("iparking: logged out; session cleared")

    async def login(self) -> None:
        self._refuse_if_disabled()
        async with self._lock:
            await self._login_locked()

    async def login_if_stale(self, gen: int) -> None:
        """Log in unless someone already did it on our behalf since `gen`.

        Two callers that both see a `2031` in the same instant would otherwise each
        re-login, and the second login invalidates the first one's token — the exact failure
        the retry exists to fix. Unlike navien there is no freshness window: the token is
        the only thing a login mints here (no temporary AWS credentials to age out), so an
        advanced generation is sufficient evidence that a usable session exists.
        """
        self._refuse_if_disabled()
        async with self._lock:
            if self._auth_gen > gen and self.access_token:
                return
            await self._login_locked()

    async def ensure_session(self) -> None:
        """Log in if there is no token yet. Never re-logins an existing session."""
        self._refuse_if_disabled()
        if self.access_token:
            return
        await self.login()

    async def _login_locked(self) -> None:
        if not self.username or not self.password:
            raise NeedCredentialsError("아이파킹 아이디와 비밀번호를 입력하세요.")

        body = {
            "client_id": self.username,
            "client_pwd": self.password,
            # `client_device_id` / `client_device_token` are push-only and the bundle drops
            # them when empty, so they are omitted rather than sent blank (verified).
            "client_os_type": CLIENT_OS_TYPE,
        }
        url = self._oauth_url()
        # Retryable. A retried login mints another token and there is nothing to double —
        # the only cost is invalidating the token the failed attempt never gave us. Note
        # this is a POST that is *safe* to retry while `POST /invitations` is not, which is
        # why the policy is per endpoint and never per method.
        response = await self._run(self.transport.request, "POST", url, self._headers(),
                                   crypto.encode_body(body), attempts=LOGIN_ATTEMPTS)
        self._require_scheme(response, OAUTH_HOST)
        envelope = self._envelope(response.text)
        code = codes.normalize_code(envelope.get("result"))

        if not codes.is_success(code):
            # A rejected password is `2002`; anything else here is still a login failure the
            # user can act on, so it carries the vendor's own key rather than a generic one.
            message = envelope.get("resultMessage") or "로그인에 실패했습니다."
            if code in ("2002", "2001", "2042"):
                raise IparkingAuthError(message)
            raise IparkingApiError(f"login -> {code}: {message}", code)

        # `auth_data` is what the probed deployment returns; some return `resultData`. Both
        # are accepted because the difference is invisible until it happens in production.
        auth = envelope.get("auth_data") or envelope.get("resultData") or {}
        if not isinstance(auth, dict) or not auth.get("access_token"):
            raise IparkingAuthError("로그인 응답을 해석하지 못했습니다.")

        self.access_token = str(auth["access_token"])
        self.memb_name = str(auth.get("memb_name") or auth.get("stor_name") or "")
        self.auth_entries = self._parse_auth_entries(auth)
        self.api_host = self._parse_api_host(auth)
        # Advanced only after every field landed, so a half-parsed login never makes a
        # waiting `login_if_stale` believe a usable session exists.
        self._auth_gen += 1

        # Length, not value. `memb_name` is a home address and is not logged at all.
        self.log(
            f"iparking: logged in (token len={len(self.access_token)}, "
            f"stores={len(self.auth_entries)}, host={self.api_host})"
        )

    @staticmethod
    def _parse_auth_entries(auth: dict) -> list[AuthEntry]:
        """Every `invitation_authorization_list` entry, order preserved.

        An entry whose `invitation_register_authorization_yn` is not `"Y"` is still kept:
        the 주차장명 sensor is useful without the write permission, so such a store pairs
        and only `register()` is gated (criterion 11).
        """
        entries = []
        raw = auth.get("invitation_authorization_list")
        for item in raw if isinstance(raw, list) else ():
            if not isinstance(item, dict):
                continue
            seq = item.get("stor_seq") or item.get("storSeq")
            if seq is None:
                continue
            flag = item.get("invitation_register_authorization_yn")
            entries.append(AuthEntry(int(seq), str(flag).upper() == "Y"))
        return entries

    @staticmethod
    def _parse_api_host(auth: dict) -> str:
        """The API **host** from `operation_company[0].domain`.

        Host only. The value reads `"http://members.iparking.co.kr"`, so parsing a scheme
        out of it would hand the server our transport policy. `urlsplit` needs the `//`,
        which is why a bare hostname falls through to `path`.
        """
        companies = auth.get("operation_company")
        domain = ""
        if isinstance(companies, list) and companies and isinstance(companies[0], dict):
            domain = str(companies[0].get("domain") or "")
        if not domain:
            return MEMBERS_HOST
        parts = urllib.parse.urlsplit(domain if "//" in domain else f"//{domain}")
        return parts.hostname or MEMBERS_HOST

    # --- requests -----------------------------------------------------------

    def _headers(self) -> dict:
        """The three headers the API wants, and no more.

        `authorization` carries the **raw UUID with no `Bearer ` prefix** — the bundle sends
        the token verbatim and the server rejects the prefixed form. Omitted entirely when
        there is no token, so login does not send an empty credential.
        """
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "version": API_VERSION,
        }
        if self.access_token:
            headers["authorization"] = self.access_token
        return headers

    def _oauth_url(self) -> str:
        return f"{SCHEMES[OAUTH_HOST]}://{OAUTH_HOST}{OAUTH_PATH}"

    def _members_url(self, path: str) -> str:
        scheme = SCHEMES.get(self.api_host, SCHEMES[MEMBERS_HOST])
        return f"{scheme}://{self.api_host}{MEMBERS_BASE_PATH}{path}"

    def _require_scheme(self, response: Response, host: str) -> None:
        """Refuse a response that came back below the scheme this host requires.

        Asserts the **final**, post-redirect scheme: the requested one cannot answer "did
        this actually use TLS?". Only `oauth` has a requirement, because it carries the
        password. `members` has none on purpose — the day the vendor fixes their TLS, an
        upgrade to https must improve this app rather than break it, which is the same
        reason `StrictRedirectHandler` allows and logs `http -> https`.
        """
        required = REQUIRED_SCHEMES.get(host)
        if required and response.final_scheme != required:
            raise IparkingError(
                f"{host} answered over {response.final_scheme}, {required} required "
                f"(final url scheme after redirects)"
            )

    @staticmethod
    def _envelope(text: str) -> dict:
        """Responses are plaintext JSON (`dataType:'json'`) — nothing to decrypt.

        A non-dict or unparseable body becomes `{}` rather than raising, so the caller reads
        a missing `result` and reports a clean vendor-verdict error instead of a traceback
        from the JSON layer.
        """
        try:
            data = json.loads(text)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking call on the default executor.

        `functools.partial` rather than `run_in_executor(None, fn, *args)` because
        `run_in_executor` takes no keyword arguments, and every `transport.request` call in
        this file names its `attempts=` explicitly. That is not style: the retry policy is
        the difference between a read and a write here, and a bare positional integer buried
        in a five-argument call is exactly how one would end up on the wrong endpoint.
        """
        call = functools.partial(fn, *args, **kwargs)
        return await asyncio.get_running_loop().run_in_executor(None, call)

    async def _authed(
        self, method: str, path: str, payload: object = None, *,
        attempts: int = READ_ATTEMPTS,
    ) -> dict:
        """A `/api/members/*` call with **exactly one** re-login retry.

        The retry covers `2031`/`2041`/`1009` only — the codes a fresh token actually fixes.
        Everything else a re-login would merely repeat, so it is raised.

        **`register()` does not use this method**, and that is deliberate rather than an
        oversight: one retry of a read is free, while one retry of `POST /invitations` is a
        second vehicle registered at a building.

        That is also why `attempts` can default to `READ_ATTEMPTS` here without qualification:
        **every endpoint routed through this method is replay-safe**, and the one that is not
        does not come through here at all. The two distinct retries stack — up to `attempts`
        transport tries per re-login, twice — which is why the budgets in `const.py` carry
        slack rather than being sized to the mean.
        """
        self._refuse_if_disabled()
        await self.ensure_session()

        relogins = 1
        while True:
            gen = self._auth_gen
            response = await self._run(
                self.transport.request, method, self._members_url(path), self._headers(),
                None if payload is None else crypto.encode_body(payload),
                attempts=attempts,
            )
            self._require_scheme(response, self.api_host)
            envelope = self._envelope(response.text)
            code = codes.normalize_code(envelope.get("result"))

            if codes.is_success(code):
                return envelope
            if codes.is_auth_expired(code) and relogins > 0:
                relogins -= 1
                self.log(f"iparking: {code} on {method} {path}; re-logging in once")
                # Deduped on the generation captured before the request, so two callers
                # racing on the same expiry produce one login rather than two.
                await self.login_if_stale(gen)
                continue
            raise IparkingApiError(
                f"{method} {path} -> {code}: {envelope.get('resultMessage') or ''}", code
            )

    # --- reads --------------------------------------------------------------

    async def parking_lots(self, stor_seq: int) -> list[dict]:
        """Raw `resultData` rows from `POST /parkinglot/list/{stor_seq}`. No body."""
        envelope = await self._authed("POST", f"/parkinglot/list/{int(stor_seq)}")
        rows = envelope.get("resultData")
        return [row for row in (rows if isinstance(rows, list) else ()) if isinstance(row, dict)]

    async def enumerate_lots(self) -> list[Lot]:
        """Every lot across **every** authorization entry — what pairing iterates.

        One request per store, so a multi-store account generalizes with no special-casing
        (criterion 11). An entry that may not register still contributes its lots.
        """
        await self.ensure_session()
        if not self.auth_entries:
            raise IparkingApiError(
                "이 계정에는 등록된 주차장 스토어가 없습니다. 관리사무소에 문의하세요.",
                key="no_stores",
            )
        lots: list[Lot] = []
        for entry in self.auth_entries:
            for row in await self.parking_lots(entry.stor_seq):
                park_seq = row.get("park_seq") or row.get("parkSeq")
                if park_seq is None:
                    continue
                lot_id = str(row.get("lot_id") or row.get("lotId") or park_seq)
                lots.append(
                    Lot(
                        lot_id=lot_id,
                        park_seq=int(park_seq),
                        park_name=str(row.get("park_name") or row.get("parkName") or lot_id),
                        stor_seq=entry.stor_seq,
                        can_register=entry.can_register,
                    )
                )
        return lots

    async def history(
        self,
        *,
        park_seq: int,
        stor_seq: int,
        start_date: str | None = None,
        end_date: str | None = None,
        car_number: str = "",
        page_size: int = HISTORY_PAGE_SIZE,
        attempts: int = READ_ATTEMPTS,
    ) -> list[HistoryRow]:
        """등록 내역 rows for a window, in the server's own order (oldest visit first).

        Display order is not this method's business — `api._newest_first` reorders for the
        table, and doing it in one place is what keeps the settings page and the planned
        widget from drifting apart.

        `attempts` is exposed only so the register path's recovery re-query can raise it to
        `RECOVERY_ATTEMPTS` — see `_recover_register`. Ordinary callers (the settings table,
        the device poll) take the read default; a dropped history refresh costs a redraw.

        **The whole window is returned, paging if the server needs it to.** `page_size` is
        honoured verbatim (verified: 100 returned all 43 rows of a three-month window), so
        one request is the normal case. But the default window is now six months, and the
        vendor answers *oldest first* — so a window holding more rows than one page would
        drop the newest ones, which are precisely the upcoming visits the table exists to
        show. `totalCnt` says when that has happened and `current_page` fetches the rest,
        exactly as the vendor's own infinite-scroll UI does.

        A non-empty `carNumber` **does** narrow the result server-side — verified live
        2026-08-04, 43 rows down to 19 for one plate — which is what makes the register
        path's recovery re-query cheap.

        It is still only an **optimisation, never the guarantee**: every caller filters
        plate and date client-side regardless. The server filter's exact matching rule
        (trimming? partial match? case?) was not characterised, and the one thing the
        recovery query must not do is mistake a server-side filter quirk for "this car is
        not registered".
        """
        today = dates.today_api()
        payload = {
            "startDate": start_date or self._shift_days(today, -HISTORY_DAYS_BACK),
            # Forward as well as back. An `endDate` of today reads as "the whole history" but
            # silently excludes every visit that has not happened yet — which is most of what
            # the 등록 내역 table exists to show.
            "endDate": end_date or self._shift_days(today, HISTORY_DAYS_AHEAD),
            "carNumber": car_number,
            "storSeq": int(stor_seq),
            "parkSeq": int(park_seq),
            "current_page": 1,
            "page_size": int(page_size),
        }
        rows: list[HistoryRow] = []
        seen: set[int] = set()
        for page in range(1, HISTORY_MAX_PAGES + 1):
            payload["current_page"] = page
            envelope = await self._authed(
                "POST", "/invitations/list", payload, attempts=attempts
            )
            page_rows, total = self._parse_history(envelope)
            # A row with no `invt_seq` parses as 0 and cannot be told apart from another one,
            # so it is never treated as a duplicate — dropping a real row is the worse error.
            fresh = [row for row in page_rows if not row.invt_seq or row.invt_seq not in seen]
            seen.update(row.invt_seq for row in fresh if row.invt_seq)
            rows.extend(fresh)
            # Three independent stops, because only the first is a documented contract. A
            # server that ignores `current_page` would answer page 2 with page 1's rows
            # forever; `fresh` being empty catches that without needing to know it happens.
            if len(rows) >= total or not page_rows or not fresh:
                break
        return rows

    @staticmethod
    def _parse_history(envelope: dict) -> tuple[list[HistoryRow], int]:
        """One page's rows, plus `totalCnt` — how many the window holds in all.

        `totalCnt` defaults to the page's own length when it is absent or unparseable, which
        reads as "this is everything" and stops the paging loop. That is the safe default:
        the alternative is looping against a server that never said how much there was.
        """
        data = envelope.get("resultData")
        rows = data.get("invitationList") if isinstance(data, dict) else None
        out: list[HistoryRow] = []
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict):
                continue
            seq = row.get("invt_seq") or row.get("invtSeq")
            out.append(
                HistoryRow(
                    invt_seq=int(seq) if seq is not None else 0,
                    # `strip_plate`, never `normalize_plate`: server data we do not get to
                    # reject. Both sides of every later comparison are normalized this way.
                    car_number=strip_plate(str(row.get("car_number") or row.get("carNumber")
                                               or "")),
                    invitation_date=str(row.get("invitation_date")
                                        or row.get("invitationDate") or ""),
                    status=str(row.get("inot_status") or row.get("inotStatus") or "").upper(),
                    park_name=str(row.get("park_name") or row.get("parkName") or ""),
                )
            )
        try:
            total = int(envelope.get("totalCnt"))
        except (TypeError, ValueError):
            total = len(out)
        return out, total

    @staticmethod
    def aggregate_counts(envelope: dict) -> list[dict]:
        """`resultData.total`, which is optional display metadata and nothing more.

        It came back `[]` even on a range holding 43 records — verified twice — so it is
        never a status aggregate. Render a row if it is ever non-empty; per-row
        `inot_status` is the authoritative source, which is why the 오늘 등록 count is derived
        by `count_registered_on` from the rows themselves and never from this field.
        """
        data = envelope.get("resultData")
        total = data.get("total") if isinstance(data, dict) else None
        return [row for row in (total if isinstance(total, list) else ()) if isinstance(row, dict)]

    async def detail(self, invt_seq: int) -> dict:
        """`GET /invitations/{invt_seq}`. No body."""
        envelope = await self._authed("GET", f"/invitations/{int(invt_seq)}")
        data = envelope.get("resultData")
        return data if isinstance(data, dict) else {}

    async def cancel(self, invt_seq: int) -> None:
        """취소 — `DELETE /invitations/{invt_seq}`. No body.

        **This does not delete the row** (verified live 2026-08-04). It flips `inot_status`
        to `CANCEL`; the row keeps its `invt_seq` and stays in the 등록 내역 list. So the
        settings table must expect a cancelled row to still be there — a caller that re-reads
        the history and looks for the row's *disappearance* would report a working 취소 as
        broken. `HistoryRow.is_active` is how the table should tell them apart.

        Production code rather than test scaffolding: the settings page's per-row 취소 needs
        it anyway, which is what let the probe prove its own cleanup path with shipping code
        instead of a throwaway script.

        ## This one **may** retry, unlike `register`. Do not "fix" that into a non-retry.

        A cancel is **idempotent at the server**, which is measured rather than assumed:
        deleting an already-cancelled row returns `13001 alreadyDeleted` (verified live
        2026-08-04), and that is a no-op on a row that is already `CANCEL`. So the outcome a
        reset leaves ambiguous — "did the first DELETE land?" — has the *same end state*
        either way, and a second attempt cannot cancel a second thing.
        `POST /invitations` has no such property: its ambiguity is one registration versus
        two, at a real building. The asymmetry is the whole reason retry policy is decided per
        endpoint here, so leaving this at `READ_ATTEMPTS` is deliberate.

        (`13001` still surfaces as an `IparkingApiError` rather than being swallowed — a
        cancel of a row the user believes is active is worth reporting. It is only the
        *retry* that is safe, not the code that is meaningless.)
        """
        await self._authed("DELETE", f"/invitations/{int(invt_seq)}")
        self.log(f"iparking: cancelled invitation {int(invt_seq)}")

    # --- the register path --------------------------------------------------

    def _entry_for(self, stor_seq: int) -> AuthEntry:
        for entry in self.auth_entries:
            if entry.stor_seq == int(stor_seq):
                return entry
        raise IparkingApiError(
            f"스토어 {int(stor_seq)}에 대한 권한 정보가 없습니다. 다시 로그인하세요.",
            key="error.not_find_store",
        )

    def _write_budget_check(self) -> None:
        """The **secondary** ceiling on writes.

        Read `const.MAX_WRITES_PER_HOUR` before touching this. The real guarantee against
        runaway writes is **zero retries in `register`**; this is a second wall, and it is
        **reset by the restart-with-backoff loop** because it lives in process memory. That
        is accepted, not overlooked — and it is written down here so nobody "fixes" the
        limiter by adding the retries it was never meant to substitute for.
        """
        now = time.monotonic()
        self._write_times = [t for t in self._write_times if now - t < WRITE_WINDOW_S]
        if len(self._write_times) >= MAX_WRITES_PER_HOUR:
            raise WriteBudgetError(
                f"한 시간에 최대 {MAX_WRITES_PER_HOUR}건까지 등록할 수 있습니다. "
                "잠시 후 다시 시도하세요."
            )

    async def register(
        self,
        *,
        car_number: str,
        park_seq: int,
        stor_seq: int,
        visit_date: str | None = None,
        memo: str = "",
        mobile: str = "",
    ) -> RegisterResult:
        """Register one visitor vehicle. Returns `"ok"` or `"already_registered"`.

        ## Zero retries on the POST. Ever.

        `asyncio.wait_for` cancels the *await*, not the `run_in_executor` thread underneath
        it. So when the 20 s budget fires, the request is **still in flight** and may still
        land. The orphan that creates is a vehicle actually registered at a real building
        after the user was told it failed — which is why the answer is a *read* (the
        recovery re-query below) and never a second write.

        ## Two sequential budgets, not one nested pair

        `REGISTER_TIMEOUT_S` (20 s) bounds the attempt. `RECOVERY_TIMEOUT_S` (40 s) bounds
        the recovery, on a **fresh** clock. Wrapping both in a single outer wait would mean
        the timeout that fired *because* the attempt hung had already consumed the budget of
        the query sent to find out what the attempt did.

        The recovery's budget is the larger of the two, which looks backwards until you count
        what is inside it: the attempt sends **once**, while the recovery sends up to
        `RECOVERY_ATTEMPTS` times with backoff between. A budget sized for one leg would have
        cancelled the retries that exist to rescue it — the same self-defeating shape as
        nesting the two waits, one level down.

        ## What each outcome means

        * `"ok"` — the server said `SUCCESS` for this plate.
        * `"already_registered"` — `EXIST`, a top-level `10003`, or a recovery re-query that
          found an **active** row. Not an error: re-entering a registered plate is the most
          likely real outcome of a first use, and item 7 needs a Flow to treat it as benign.
        * `"register_failed"` — the server explicitly said `FAIL` for this plate. A verdict,
          not a guess.
        * `RegisterUncertain` — anything unresolved. Never `register_failed`, because that
          word invites the retry this whole method exists to avoid.

        The return is a `RegisterResult`, which is that word plus the `api_date` actually
        used — so item 7's notification can echo the date back and make a misparsed Flow
        argument visible immediately.
        """
        self._refuse_if_disabled()

        # 1. Gate on LIVE authorization. `ensure_session` logs in if needed, so the flag
        #    reflects the current login rather than whatever was true at pairing — the
        #    building office can grant the permission after a device was paired.
        await self.ensure_session()
        if not self._entry_for(stor_seq).can_register:
            raise NotPermittedError(
                "이 계정에는 방문차량 등록 권한이 없습니다. 관리사무소에 문의하세요."
            )

        # 2. Normalize the plate. Raises InvalidPlateError, which carries the site's own
        #    example hint — this is user input, so it gets a verdict.
        plate = normalize_plate(car_number)

        # 3. Resolve the date in KST. `resolve_visit_date` is the register path's entry
        #    point rather than `to_api_date`, because the 방문 예정일 window (not in the past,
        #    not beyond MAX_DAYS_AHEAD) belongs here and not to the history query, which
        #    legitimately asks for dates three months back. The default goes through the same
        #    function rather than short-circuiting to `today_api()`, so there is one parse
        #    path and one place the window is enforced.
        api_date = dates.resolve_visit_date(visit_date or dates.today_kst())
        if api_date.ambiguous:
            # Resolved day-first by policy, not by evidence. Logged so the on-device probe
            # at item 9 has something to read, and carried on `RegisterResult` so the
            # surface can show the user which date it picked.
            self.log(
                f"iparking: visit date {visit_date!r} was ambiguous; "
                f"read as {api_date.source_format} -> {dates.format_kst_human(api_date)}"
            )

        # 4. Rate-limit. Secondary to zero-retries; see `_write_budget_check`.
        self._write_budget_check()

        payload = {
            "parkSeq": int(park_seq),
            "storSeq": int(stor_seq),
            "userId": self.username,
            # `memb_name` — a home address. Required by the endpoint, never logged.
            "userName": self.memb_name,
            "invitationDate": api_date,
            "invitationInfoList": [self._car_entry(plate, memo, mobile)],
        }

        # Recorded before the attempt, not after it succeeds: an attempt that times out may
        # still have reached the server, and the ceiling has to count writes that *might*
        # have happened.
        self._write_times.append(time.monotonic())
        self.log(
            f"iparking: register {mask_plate(plate)} on {api_date} "
            f"(park={int(park_seq)}, zero retries)"
        )

        outcome, failure = await self._attempt_register(payload, plate)
        if outcome is not None:
            return RegisterResult(outcome, api_date)

        # 5. Recovery, on a FRESH budget. Sequential to the one above, never nested — the
        #    wait that fired *because* the attempt hung must not also bound the query sent
        #    to find out what the attempt did.
        recovered = await asyncio.wait_for(
            self._recover_register(plate, api_date, park_seq, stor_seq, failure),
            RECOVERY_TIMEOUT_S,
        )
        return RegisterResult(recovered, api_date)

    @staticmethod
    def _car_entry(plate: str, memo: str, mobile: str) -> dict:
        """One `invitationInfoList` row.

        `mobile1/2/3` are sent **only** when a phone number was entered, split the way the
        bundle splits it; the vendor omits all three otherwise rather than sending blanks.
        """
        entry: dict = {"carNumber": plate, "memo": memo or ""}
        digits = "".join(ch for ch in (mobile or "") if ch.isdigit())
        if len(digits) in (10, 11) and digits[:3] in ("010", "011", "016", "018", "019"):
            entry["mobile1"] = digits[:3]
            entry["mobile2"] = digits[3:-4]
            entry["mobile3"] = digits[-4:]
        return entry

    async def _attempt_register(self, payload: dict, plate: str) -> tuple[str | None, str]:
        """The single `POST /invitations`. Returns `(outcome_or_None, failure_note)`.

        `None` means "not settled" and routes to recovery. **No branch in here retries**,
        and none may be added: not on a network error, not on `2031`, not on a timeout, and
        **not on a connection reset** either. An expired token is handled by logging in
        *before* the write (step 1), never by re-sending it.
        """
        try:
            response = await asyncio.wait_for(
                self._run(self.transport.request, "POST", self._members_url("/invitations"),
                          self._headers(), crypto.encode_body(payload),
                          # ── ZERO RETRIES. `attempts=1`, written as a literal, at the one
                          # call site in this file that must never grow a retry.
                          #
                          # The members host resets ~30 % of connections, so the temptation
                          # here is real and it is the trap. A reset **cannot distinguish**
                          # "the request never arrived" from "it arrived, the server
                          # registered the vehicle, and the reply died with the socket." The
                          # transport is honest about this — it raises `ConnectionLost`, and a
                          # retry would be safe if nothing were in flight — but *this*
                          # endpoint's ambiguity is not about the socket. It is about whether
                          # a vehicle now has access to a real building's car park. Retrying
                          # risks registering it **twice**.
                          #
                          # The answer to the ambiguity is the *read* below (`_recover_register`),
                          # which retries hard precisely so this one never has to. There is
                          # deliberately no constant for this `1` in `const.py`: a named
                          # tunable is an invitation, and this is an invariant.
                          attempts=1),
                REGISTER_TIMEOUT_S,
            )
        except TimeoutError:
            # The thread is still running and the POST may still land — hence a read, not a
            # retry. (`asyncio.TimeoutError` is `TimeoutError` from 3.11 on.)
            self.log("iparking: register attempt timed out; re-querying to find out")
            return None, "timeout"
        except (NetworkError, RedirectRefused, TransportError) as exc:
            self.log(f"iparking: register attempt failed ({type(exc).__name__}); re-querying")
            return None, type(exc).__name__

        envelope = self._envelope(response.text)
        code = codes.normalize_code(envelope.get("result"))

        # A non-success code is not evidence of non-registration, so it goes to recovery
        # rather than straight to a failure report.
        if not (codes.is_success(code) or code == codes.REGISTERED_CAR):
            self.log(f"iparking: register answered {code}; re-querying")
            return None, f"result={code}"

        # ── The top-level `result` is the authority on success. VERIFIED LIVE 2026-08-04.
        #
        # The probe settled a question the recon could only guess at, and the answer inverts
        # what this code originally did. A successful `POST /invitations` returns exactly
        # `{"result":"0000", …, "resultData": null}` — there is **no `invitationInfoList`**,
        # no `SUCCESS`/`FAIL`/`EXIST` array, in any case the probe could produce.
        #
        # So `parse_per_car` finds nothing on a perfectly normal registration. Treating its
        # silence as "the response did not say" — which is the right reading for a *shape we
        # have not seen* — made **every successful registration** report as
        # `RegisterUncertain`, sending the user to the web UI after every single car.
        #
        # Hence the ordering below: an explicit per-car row still wins if one ever appears
        # (batch registration is a follow-up, and `invitationInfoList` is natively an array),
        # but its **absence on an 0000 means success**, not doubt.
        # `parse_per_car` no longer takes a `requested` list at all: it used to synthesize an
        # `already_registered` verdict for a top-level `10003` from the plates it was told
        # were requested, which made *this* function's `10003` branch below unreachable — an
        # unreachable guard is not a guard. (Found by mutation testing: changing that branch
        # to return `ok` left the entire suite green.) The client knows exactly what it sent,
        # so it owns the whole-request verdict; the parser only reads explicit per-car rows.
        per_car = codes.parse_per_car(envelope)
        outcome = per_car.get(plate)
        if outcome is not None:
            return outcome, ""

        if code == codes.REGISTERED_CAR:
            # 기등록 차량 — and per the probe, the **only** `EXIST` signal this service
            # actually emits: the per-car word never appeared in any response.
            #
            # The vendor's own `resultMessage` here reads
            # "방문차량 등록이 실패하였습니다. 다시 시도해주세요." — it calls a duplicate a failure
            # and tells the user to retry. It is deliberately NOT surfaced: this is a benign
            # third outcome, and the retry it invites is a write against a building.
            return codes.OUTCOME_ALREADY_REGISTERED, ""

        # `result == "0000"` and nothing per-car: the normal, verified success path.
        return codes.OUTCOME_OK, ""

    async def _recover_register(
        self, plate: str, api_date: str, park_seq: int, stor_seq: int, failure: str
    ) -> str:
        """Ask the server what actually happened, then answer honestly or say "unknown".

        The window is pinned to **`startDate == endDate == api_date`**. Left as a trailing
        range, a query for a future 방문 예정일 returns nothing and a *successful*
        registration reads as a failure.

        ## This is the one query in the app that retries hardest, and why

        `RECOVERY_ATTEMPTS` (5), one more than an ordinary read. Its entire job is resolving
        the uncertainty that zero-retries-on-the-write deliberately creates, so **its own
        failure is the expensive one**: it converts a knowable outcome into a bare error and
        sends the user off to the vendor's website to find out whether a car is registered.
        That happened live on 2026-08-04 — the write failed on a reset, the re-query hit
        another reset, and the user got nothing. A read against a host that drops ~30 % of
        connections is exactly the thing that must not be attempted once.
        """
        await asyncio.sleep(RECOVERY_SLEEP_S)
        try:
            rows = await self.history(
                park_seq=park_seq,
                stor_seq=stor_seq,
                start_date=api_date,
                end_date=api_date,
                attempts=RECOVERY_ATTEMPTS,
            )
        except (IparkingError, TransportError) as exc:
            # Logged, not just re-raised as a different type. The recovery giving up is the
            # most consequential failure this app has, and it used to leave no trace at all:
            # the register path logged three request lines and then nothing, so there was no
            # way to see *why* an outcome went unknown. Type only — the vendor's message can
            # echo request content back, and this one is already carrying a masked plate.
            self.log(
                f"iparking: recovery re-query failed for {mask_plate(plate)} on {api_date} "
                f"after {RECOVERY_ATTEMPTS} attempt(s) ({type(exc).__name__}); "
                f"outcome unknown (write cause={failure})"
            )
            raise RegisterUncertain(self._uncertain_text(plate, api_date)) from exc

        matching = self.matching_rows(rows, plate, api_date)

        # THE PREDICATE. Existential over every matching row — never "find the row, then
        # check its status", and never `if matching:`.
        #
        # **The coexistence this depends on was CONFIRMED LIVE on 2026-08-04**, so it is no
        # longer an inference to be "simplified" away. The probe established:
        #   * `DELETE /invitations/{seq}` does **not** remove the row. It flips `inot_status`
        #     to `CANCEL`; the row keeps its `invt_seq` and stays in the list.
        #   * A `CANCEL` row does **not** block re-registration: the same plate and date
        #     registers again and creates a **new row with a new `invt_seq`**.
        # So a plate+date really can have a `CANCEL` row and a `RESERVE` row at the same
        # time, and a single-row lookup really can land on the wrong one.
        #
        # Both directions of getting this wrong cause harm at a gate: a first-match lookup
        # hitting the `CANCEL` row reports a write that *succeeded* as failed (inviting the
        # retry that makes a second real registration), while accepting `CANCEL` as existence
        # reports an unregistered car as registered (a visitor at a gate that will not open).
        if any(row.is_active for row in matching):
            self.log(
                f"iparking: recovery found {mask_plate(plate)} active on {api_date} "
                f"({len(matching)} matching row(s)); the write landed"
            )
            return codes.OUTCOME_ALREADY_REGISTERED

        # A miss, or a CANCEL-only match. Both are *unknown*, not failure.
        self.log(
            f"iparking: recovery could not confirm {mask_plate(plate)} on {api_date} "
            f"({len(matching)} matching row(s), none active; cause={failure})"
        )
        raise RegisterUncertain(self._uncertain_text(plate, api_date))

    @staticmethod
    def matching_rows(rows: list[HistoryRow], plate: str, api_date: str) -> list[HistoryRow]:
        """Rows for this plate on this date — **all** of them, including `CANCEL`.

        Filtered client-side **unconditionally**, even though the server's own `carNumber`
        filter is now known to work (verified live 2026-08-04). It stays an optimisation
        rather than the guarantee: its matching rule was never characterised, and a quirk
        there would read as "this car is not registered".

        Returning every match rather than the first is what lets the existence predicate be
        existential — and `CANCEL` rows coexisting with active ones for the same plate and
        date is verified behaviour, not a hypothetical.

        The `plate` argument is stripped here as well as by the caller. Not redundant: this
        method is public and item 5's history filter passes raw user input, where a trailing
        space would silently match nothing.
        """
        wanted = strip_plate(plate)
        return [
            row for row in rows
            if row.car_number == wanted and row.invitation_date == api_date
        ]

    @staticmethod
    def _uncertain_text(plate: str, api_date: str) -> str:
        """The one message in this app that must **not** suggest trying again.

        A retry is what turns one uncertain write into two real registrations at a building,
        so the text names the uncertainty and points at the surface that can resolve it.
        """
        return (
            f"{mask_plate(plate)} {api_date} 등록 결과를 확인할 수 없습니다. "
            "등록되었을 수도 있으니 다시 등록하지 마시고, "
            "아이파킹 MEMBERS 웹사이트의 등록 내역에서 확인하세요."
        )

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _shift_days(api_date: str, days: int) -> str:
        """`api_date` moved by `days` (negative = earlier), still `yyyyMMdd`."""
        parsed = datetime.strptime(api_date, dates.API_DATE_FORMAT)
        return (parsed + timedelta(days=days)).strftime(dates.API_DATE_FORMAT)
