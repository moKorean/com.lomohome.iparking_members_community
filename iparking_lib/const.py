"""Constants shared by the pure client and the Homey-facing layers.

Lives at `iparking_lib/const.py` rather than inside `iparking_lib/iparking/` because the
driver, device and settings-page handlers need these too — same split as
`navien_lib/const.py`. Nothing here may pull in the Homey SDK; this module is imported by
the off-device test suite.

Two groups are worth reading before changing anything:

**The per-host scheme table** is a measured fact, not a preference. See `SCHEMES`.

**The register path's budgets** are derived arithmetic, not taste. See `REGISTER_TIMEOUT_S`.
"""

from __future__ import annotations

# --- Hosts and transport policy ---------------------------------------------

#: Login. Carries the password, and serves HTTPS correctly (verified: 200,
#: `ssl_verify_result=0`).
OAUTH_HOST = "oauth.parkingcloud.co.kr"

#: `/api/members/*` — the token and all plate/date data. Answers **every** HTTPS request
#: with a 301 down to `http://` (verified, `/api/members/*` included).
MEMBERS_HOST = "members.iparking.co.kr"

#: The scheme each host is addressed with. **Asymmetric because the servers removed the
#: choice**, and never parsed from `operation_company[0].domain` — that value literally
#: reads `"http://members.iparking.co.kr"`, so trusting it would also make the oauth
#: decision for us. Only the *host* is taken from it.
#:
#: `members` being `http` is not resignation: "https first, fall back" was rejected because
#: urllib strips the body when it follows the 301, so the fallback would appear to work
#: while sending nothing. Going straight to http is the honest version of the same result,
#: and `transport.StrictRedirectHandler` guarantees no request can end up on cleartext
#: while believing otherwise.
SCHEMES: dict[str, str] = {
    OAUTH_HOST: "https",
    MEMBERS_HOST: "http",
}

#: The scheme below which a host's response is refused outright. Only oauth has one: it
#: carries the password. `members` is deliberately absent — if the vendor ever fixes their
#: TLS, an upgrade to https must *improve* this app rather than break it, which is the same
#: reason `StrictRedirectHandler` allows (and logs) `http -> https`.
REQUIRED_SCHEMES: dict[str, str] = {
    OAUTH_HOST: "https",
}

OAUTH_PATH = "/api/oauth/store/authorize"
MEMBERS_BASE_PATH = "/api/members"

#: Sent on every request. A vendor bump here is a single-constant change whose failure mode
#: is a clean `login_failed` rather than a crash.
API_VERSION = "2.0.0"

#: `client_os_type` in the login body. `WEB` is what the bundle sends.
CLIENT_OS_TYPE = "WEB"

#: `page_size` is honoured verbatim (verified: 100 returned all 43 rows in one response),
#: so the whole 3-month history arrives in a single request and pagination is a display
#: concern rather than a fetch concern.
HISTORY_PAGE_SIZE = 100

#: How far back the default history window reaches. The server caps it at 최근 3개월
#: (the UI sets `minDate:'-3m'`), so asking for more returns no more.
HISTORY_DAYS_BACK = 90

# --- Register-path budgets --------------------------------------------------
#
# Derived, not chosen. One transport leg is `transport.DEFAULT_TIMEOUT_S` = 15 s. A leg
# that may re-login once is 30 s. Pairing's longest chain is 45 s, hence PAIR_TIMEOUT_S=60.

#: Budget for the single `POST /invitations` attempt. 20 s = one 15 s leg plus slack for
#: the executor hand-off. **There are no retries inside it** — see `client.register`.
REGISTER_TIMEOUT_S = 20.0

#: Budget for the recovery re-query, **sequential to** `REGISTER_TIMEOUT_S` and never
#: nested inside it. The whole reason recovery exists is that the outer wait fired; making
#: it share the budget that just expired would leave it no time to answer the one question
#: that matters — did the write land?
RECOVERY_TIMEOUT_S = 25.0

#: Pause before the recovery re-query, so a write the server is still committing has time
#: to become visible to a read. Counted inside `RECOVERY_TIMEOUT_S`.
RECOVERY_SLEEP_S = 3.0

#: Pairing enumerates every authorization entry × every lot; 45 s of legs plus slack.
PAIR_TIMEOUT_S = 60.0

#: In-process ceiling on `POST /invitations`. **Secondary.** The actual guarantee against
#: runaway writes is *zero retries*; this is a second wall, and it is **reset by the
#: restart-with-backoff loop**, which is accepted rather than overlooked. Stated here so
#: nobody "fixes" the limiter by adding the retries it was never meant to replace.
MAX_WRITES_PER_HOUR = 10

#: The window `MAX_WRITES_PER_HOUR` is counted over.
WRITE_WINDOW_S = 3600.0

# --- History row statuses ---------------------------------------------------

STATUS_RESERVE = "RESERVE"    # 미입차
STATUS_IN = "IN"              # 주차중
STATUS_OUT = "OUT"            # 출차
STATUS_CANCEL = "CANCEL"      # 취소

#: The statuses that constitute **evidence that a car is registered**, and the single most
#: consequential constant in this app.
#:
#: `CANCEL` is excluded. **Verified live 2026-08-04**, so the reasoning below rests on
#: measurement rather than inference: `DELETE /invitations/{seq}` does not remove a row, it
#: flips `inot_status` to `CANCEL` and the row keeps its `invt_seq`; and a `CANCEL` row does
#: not block re-registering the same plate and date, which creates a **new** row. A plate can
#: therefore hold a `CANCEL` row and a `RESERVE` row simultaneously.
#:
#: Both directions of getting this wrong cause real harm:
#:
#: * Counting `CANCEL` as existence reports an *unregistered* car as already registered —
#:   which puts a visitor in front of a gate that will not open. Reachable by
#:   register → 취소 → re-register, which the per-row 취소 button makes easy.
#: * Excluding it is only safe because the predicate is **existential over all matching
#:   rows** (`any(...)`) rather than "find the row, then check its status". Since `CANCEL`
#:   rows coexist with active ones, a single-row lookup can land on the `CANCEL` row and
#:   report a *succeeded* registration as failed — the same harm with the sign flipped, and
#:   the defect that survived a full review round.
ACTIVE_STATUSES = frozenset({STATUS_RESERVE, STATUS_IN, STATUS_OUT})

# --- Settings keys ----------------------------------------------------------
#
# The `access_token` is deliberately NOT here. It is memory-only: a 7-day credential that
# can register and cancel vehicles at a building, crossing the wire in cleartext. Keeping
# it out of `homey.settings` keeps it out of hub backups and settings exports
# (criterion 12).

SETTING_USERNAME = "iparking_id"
SETTING_PASSWORD = "iparking_pw"
SETTING_LANGUAGE = "language"

STORE_STOR_SEQ = "stor_seq"
STORE_PARK_SEQ = "park_seq"
STORE_PARK_NAME = "park_name"

CAPABILITY_PARK_NAME = "iparking_park_name"
