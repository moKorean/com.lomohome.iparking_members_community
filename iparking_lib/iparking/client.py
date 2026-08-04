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

**3. `register()` never retries.** Everything about that method is shaped by one failure:
a vehicle actually registered at a real building after the user was told it failed. See its
docstring. If you are here to "add a retry for reliability", read it first; the retry is
the failure, not the fix.

## Never logged

The password in any form. The `access_token` value. Request bodies, encrypted or plain.
`memb_name` — it is a home address. Plates are masked (`12가****`) because diagnostic
output gets pasted into issues. `_safe` and `plate.mask_plate` are the only routes from
this data to a log line.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from iparking_lib.const import (
    ACTIVE_STATUSES,
    API_VERSION,
    CLIENT_OS_TYPE,
    HISTORY_DAYS_BACK,
    HISTORY_PAGE_SIZE,
    MAX_WRITES_PER_HOUR,
    MEMBERS_BASE_PATH,
    MEMBERS_HOST,
    OAUTH_HOST,
    OAUTH_PATH,
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
        response = await self._run(self.transport.request, "POST", url, self._headers(),
                                   crypto.encode_body(body))
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

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def _authed(self, method: str, path: str, payload: object = None) -> dict:
        """A `/api/members/*` call with **exactly one** re-login retry.

        The retry covers `2031`/`2041`/`1009` only — the codes a fresh token actually fixes.
        Everything else a re-login would merely repeat, so it is raised.

        **`register()` does not use this method**, and that is deliberate rather than an
        oversight: one retry of a read is free, while one retry of `POST /invitations` is a
        second vehicle registered at a building.
        """
        self._refuse_if_disabled()
        await self.ensure_session()

        relogins = 1
        while True:
            gen = self._auth_gen
            response = await self._run(
                self.transport.request, method, self._members_url(path), self._headers(),
                None if payload is None else crypto.encode_body(payload),
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
    ) -> list[HistoryRow]:
        """등록 내역 rows for a window, newest-first as the server returns them.

        `car_number` is passed through when given, but **every caller filters client-side
        anyway**: that a non-empty `carNumber` narrows the result server-side is
        *unverified* (the one verified call sent `""`), so it is treated as an optimisation
        never relied upon.
        """
        today = dates.today_api()
        payload = {
            "startDate": start_date or self._days_before(today, HISTORY_DAYS_BACK),
            "endDate": end_date or today,
            "carNumber": car_number,
            "storSeq": int(stor_seq),
            "parkSeq": int(park_seq),
            "current_page": 1,
            # `page_size` is honoured verbatim, so one request covers the whole window.
            "page_size": int(page_size),
        }
        envelope = await self._authed("POST", "/invitations/list", payload)
        return self._parse_history(envelope)

    @staticmethod
    def _parse_history(envelope: dict) -> list[HistoryRow]:
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
        return out

    @staticmethod
    def aggregate_counts(envelope: dict) -> list[dict]:
        """`resultData.total`, which is optional display metadata and nothing more.

        It came back `[]` even on a range holding 43 records — verified twice — so it is
        never a status aggregate. Render a row if it is ever non-empty; per-row
        `inot_status` is the authoritative source. There is deliberately no counting logic
        anywhere in this app.
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

        Production code rather than test scaffolding: the settings page's per-row 취소 needs
        it anyway, which is what let item 3's probe prove its own cleanup path with shipping
        code instead of a throwaway script.
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

        `REGISTER_TIMEOUT_S` (20 s) bounds the attempt. `RECOVERY_TIMEOUT_S` (25 s) bounds
        the recovery, on a **fresh** clock. Wrapping both in a single outer wait would mean
        the timeout that fired *because* the attempt hung had already consumed the budget of
        the query sent to find out what the attempt did.

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
        and none may be added: not on a network error, not on `2031`, not on a timeout. An
        expired token is handled by logging in *before* the write (step 1), never by
        re-sending it.
        """
        try:
            response = await asyncio.wait_for(
                self._run(self.transport.request, "POST", self._members_url("/invitations"),
                          self._headers(), crypto.encode_body(payload)),
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
        # rather than straight to a failure report. `10003` is a *verdict* though, and
        # `parse_per_car` turns it into `already_registered` via `requested`.
        if not (codes.is_success(code) or code == codes.REGISTERED_CAR):
            self.log(f"iparking: register answered {code}; re-querying")
            return None, f"result={code}"

        per_car = codes.parse_per_car(envelope, requested=[plate])
        # Absent means "the response did not say" (worker-4's contract) — never success,
        # never generic failure. Recovery is exactly what resolves it.
        outcome = per_car.get(plate)
        if outcome is None:
            self.log("iparking: register response carried no verdict for this car; re-querying")
            return None, "no_per_car_result"
        return outcome, ""

    async def _recover_register(
        self, plate: str, api_date: str, park_seq: int, stor_seq: int, failure: str
    ) -> str:
        """Ask the server what actually happened, then answer honestly or say "unknown".

        The window is pinned to **`startDate == endDate == api_date`**. Left as a trailing
        range, a query for a future 방문 예정일 returns nothing and a *successful*
        registration reads as a failure.
        """
        await asyncio.sleep(RECOVERY_SLEEP_S)
        try:
            rows = await self.history(
                park_seq=park_seq,
                stor_seq=stor_seq,
                start_date=api_date,
                end_date=api_date,
            )
        except (IparkingError, TransportError) as exc:
            raise RegisterUncertain(self._uncertain_text(plate, api_date)) from exc

        matching = self.matching_rows(rows, plate, api_date)

        # THE PREDICATE. Existential over every matching row — never "find the row, then
        # check its status". `CANCEL` rows coexist with active ones for the same plate and
        # date, so a single-row lookup can land on the `CANCEL` row and report a write that
        # *succeeded* as failed; and accepting `CANCEL` as existence reports an
        # unregistered car as registered, putting a visitor at a gate that will not open.
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

        Filtered client-side unconditionally. Server-side `carNumber` narrowing is
        unverified, so it is never relied upon; and returning every match (rather than the
        first) is what makes the existence predicate able to be existential.

        Both sides are already normalized: the plate went through `normalize_plate` before
        being sent, and `HistoryRow.car_number` through `strip_plate` on the way in.
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
    def _days_before(api_date: str, days: int) -> str:
        parsed = datetime.strptime(api_date, dates.API_DATE_FORMAT)
        return (parsed - timedelta(days=days)).strftime(dates.API_DATE_FORMAT)
