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

# --- Retry attempts, per endpoint semantics ----------------------------------
#
# `members.iparking.co.kr` resets roughly **30 %** of plain-HTTP connections. Measured
# 2026-08-04: 20 identical read-only requests → 14 answers, 6 dead sockets. Not
# header-dependent, not a rate limit, not a block; `curl` interleaved with `urllib` survives
# it only because `curl` retries internally.
#
# The arithmetic that sizes these numbers, at P(fail) = 0.3:
#
#   attempts │ 1     2    3     4     5
#   P(all)   │ 30 %  9 %  2.7 % 0.8 % 0.24 %
#
# **These are per *endpoint*, never per HTTP method.** This API serves reads over POST, so
# "retry POSTs" is not a usable rule — it would retry `POST /invitations`, which is a write
# against a real building. There is deliberately **no constant here for the register
# attempt**: its call site passes a literal `1` next to the comment explaining why, because a
# named tunable is an invitation and this one must not be tuned.

#: Read-only endpoints — `POST /invitations/list`, `POST /parkinglot/list/{seq}`,
#: `GET /invitations/{seq}`, and `DELETE /invitations/{seq}` (idempotent; see
#: `client.cancel`). Four attempts takes a 30 % failure to **0.8 %**.
READ_ATTEMPTS = 4

#: The oauth login. Retryable because a retry just mints another token — there is nothing
#: to double. Same four attempts as a read; the host is the reliable one, so this is
#: insurance rather than the fix.
LOGIN_ATTEMPTS = 4

#: The register path's **recovery re-query**. Five, one more than an ordinary read, because
#: this is the query that resolves the uncertainty zero-retries-on-the-write creates. When
#: *it* fails, a knowable outcome becomes a bare error and the user is told to go look at the
#: vendor's website — which is what happened live on 2026-08-04. Five attempts takes the
#: chance of that to **0.24 %**.
RECOVERY_ATTEMPTS = 5

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
# Derived, not chosen. One transport leg is `transport.DEFAULT_TIMEOUT_S` = 15 s. A leg that
# may re-login once is 30 s.
#
# **A retried leg costs backoff as well as legs**, and the measured fault fails *fast* — a
# reset arrives immediately, not after the 15 s timeout — so the honest worst case for N
# attempts is (N-1) instant failures plus their backoffs plus one full 15 s leg. From
# `transport.RETRY_BACKOFF_*`, the jittered ceiling is 3.15 s for 4 attempts and 6.15 s for 5.
# The pathological case where every leg *also* burns its full timeout is deliberately not
# budgeted for: it is not the fault that was measured, and sizing for it would mean a
# two-minute Flow card.

#: Budget for the single `POST /invitations` attempt. 20 s = one 15 s leg plus slack for
#: the executor hand-off. **There are no retries inside it**, so this number is untouched by
#: the retry work and that is the point — see `client.register`.
REGISTER_TIMEOUT_S = 20.0

#: Budget for the recovery re-query, **sequential to** `REGISTER_TIMEOUT_S` and never
#: nested inside it. The whole reason recovery exists is that the outer wait fired; making
#: it share the budget that just expired would leave it no time to answer the one question
#: that matters — did the write land?
#:
#: 40 s, raised from 25 s when the re-query gained `RECOVERY_ATTEMPTS` tries: 3 s pause +
#: 6.15 s of worst-case backoff + a 15 s leg = 24.2 s, and a budget that merely *fits* would
#: mean the retries meant to rescue the recovery get killed by the timeout bounding them.
#: That is the same self-defeating shape as nesting the two budgets, one level down.
RECOVERY_TIMEOUT_S = 40.0

#: Pause before the recovery re-query, so a write the server is still committing has time
#: to become visible to a read. Counted inside `RECOVERY_TIMEOUT_S`.
RECOVERY_SLEEP_S = 3.0

#: Pairing enumerates every authorization entry × every lot; 45 s of legs plus slack.
#:
#: 90 s, raised from 60 s for the same reason `RECOVERY_TIMEOUT_S` moved: every leg in that
#: chain is now a retrying read. Pairing is the *most* reset-exposed path in the app — one
#: `parkinglot/list` per store, each independently ~30 % likely to fail once — so it is where
#: the retries matter most and where a budget that ignored them would cancel them.
PAIR_TIMEOUT_S = 90.0

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

# --- Device poll cadence ----------------------------------------------------
#
# 24 requests/day/device + ~1 login/week. This is politeness enforced by arithmetic rather
# than asserted, and it is the number the counterparty risk in the plan is priced against:
# the vendor can rate-limit or block this client regardless of how it is distributed.
#
# **What is polled decides whether polling is justified at all**, and this app has now been on
# both sides of that. v0.1.3 polled to refresh 주차장명 — the lot's name, which is *also* the
# device's own name, and is fixed for the lifetime of a pairing. 24 requests a day to
# re-confirm a constant is waste, so both the sensor and the loop were deleted. v0.1.4 polls
# `CAPABILITY_TODAY_COUNT` instead, which changes whenever anybody registers a car — including
# on the vendor's own website, where this app cannot see it happen. That is the distinction to
# keep: polling a constant was waste, polling a count is what makes it true.

#: One hour. There is still **no `poll_interval` device setting** — deliberately. A knob here
#: would invite exactly the tightening this cadence exists to avoid, and it would buy very
#: little: the count is already updated the instant this app's own register, cancel or history
#: fetch answers, so the poll only has to catch registrations made elsewhere and the KST
#: midnight rollover.
POLL_INTERVAL_S = 3600.0

#: ± fraction applied to every poll sleep, so N paired lots do not tick in lockstep.
POLL_JITTER = 0.10

#: One-shot 0–10 % offset added to the *first* loop sleep only, so devices that all start at
#: app boot spread out instead of converging on the same second forever after.
POLL_START_JITTER = 0.10

#: Backoff walk for restarting a poll task that died on its own. Same shape as navien's
#: `MQTT_BACKOFF_S`: a crash loop costs 5 s once and 300 s thereafter, so a genuine bug
#: leaves a readable log rather than a flood.
POLL_BACKOFF_S = (5, 15, 30, 60, 120, 300)

#: Consecutive failed polls before the device goes unavailable. **Two, not one.** One is a
#: single dropped request on a cloud API reached over plain HTTP, which is ordinary; two is a
#: pattern. And the failure this guards against is not a loud one — the capability keeps the
#: last count it read, so without the transition a lot that stopped answering would sit on
#: screen looking exactly like a lot with no visitors today.
MAX_POLL_FAILURES = 2

# --- Flow cards -------------------------------------------------------------

#: `.homeycompose/flow/actions/register_visitor.json`. Plate + an **optional** 방문 예정일.
FLOW_REGISTER_VISITOR = "register_visitor"

#: `.homeycompose/flow/actions/register_visitor_today.json`. Plate only, always today in KST.
#:
#: Not a second code path — both cards bind to the same `VisitCarDevice_.flow_register`, and
#: this one simply passes no date. It exists because the card above, with its optional `date`
#: field, still *shows* that field in the Flow editor: an empty one already means today, but a
#: wrongly filled one silently registers the wrong day and the guest finds out at a closed
#: gate. This card removes the field, not the behaviour. Two cards, still one write path.
FLOW_REGISTER_VISITOR_TODAY = "register_visitor_today"

# --- Settings keys ----------------------------------------------------------
#
# The `access_token` is deliberately NOT here. It is memory-only: a 7-day credential that
# can register and cancel vehicles at a building, crossing the wire in cleartext. Keeping
# it out of `homey.settings` keeps it out of hub backups and settings exports
# (criterion 12).

SETTING_USERNAME = "iparking_id"
SETTING_PASSWORD = "iparking_pw"
SETTING_LANGUAGE = "language"

# --- Device store keys ------------------------------------------------------
#
# Everything a paired device needs *except* its identity. `data.id` holds `lot_id` and is
# **immutable after pairing**, so getting that wrong forces every user to re-pair; the store
# is mutable, which is why `stor_seq`, `park_seq` and `park_name` live here even though the
# first two never actually change. Nothing writes any of them at runtime (criterion 18).

STORE_STOR_SEQ = "stor_seq"
STORE_PARK_SEQ = "park_seq"

#: Written by the driver at pairing, and **read by nobody at runtime** since the 주차장명
#: sensor was removed. Kept because it is the value the pair view names the device with, and a
#: store key that already exists on every paired device costs nothing to leave in place.
STORE_PARK_NAME = "park_name"

#: The same value as `data.id`, kept in the store as well so the device layer can read its
#: lot id without `get_data()` — one accessor rather than two, and the store is the object
#: both the driver and the device already agree on.
STORE_LOT_ID = "lot_id"

#: The 주차장명 sensor v0.1.3 shipped. **Removed, and this id survives only so that already
#: paired devices can shed it** — `device._shed_park_name` calls `remove_capability` with it at
#: `on_init`, which is what spares every user a re-pair. There is no
#: `.homeycompose/capabilities/iparking_park_name.json` any more, so nothing can add it back.
#:
#: Why it went: its value was the lot's name, which is *also* the name Homey shows for the
#: device, so the tile printed the same string twice; it is constant for the lifetime of a
#: pairing; and the hourly poll existed only to refresh it. `CAPABILITY_TODAY_COUNT` replaced
#: it — same tile, same poll, a value that actually changes. Delete this constant once no hub in
#: the field can still be carrying the capability.
CAPABILITY_PARK_NAME = "iparking_park_name"

#: **오늘 등록된 차량 수** — the number of vehicles registered for today at this lot, and the one
#: capability every paired device carries. `.homeycompose/capabilities/iparking_today_count.json`.
#:
#: It replaced 주차장명 because it is the value a resident actually wants from a glance at a
#: tile, and because it earns the poll that the constant string did not.
#:
#: **It carries `insights: true`, and that is the opposite decision from the one 주차장명 got —
#: for the same reason.** A near-constant string logged over time produces an empty graph;
#: a count of registrations per day is a real measurement, and the graph answers a real question
#: (how often does this household have visitors?).
#:
#: **`CANCEL` rows are not counted.** See `ACTIVE_STATUSES`: 취소 does not delete a row, it flips
#: `inot_status`, so a day's rows are frequently mostly cancellations — on the maintainer's own
#: account, counting them showed 6 where the honest answer was 1. The count is the same
#: existential predicate over `ACTIVE_STATUSES` that the register path's recovery re-query uses,
#: and it is deliberately the *same set object* rather than a second spelling of it.
CAPABILITY_TODAY_COUNT = "iparking_today_count"

# --- 자주 오는 차량: the device's own tile buttons ----------------------------
#
# Twenty device settings (10 names + 10 plates) and ten capabilities, and the asymmetry between
# those two numbers is the whole mechanism. Homey allows exactly one interactive control on a
# tile — a `boolean` capability with `uiComponent: "button"` — and it has **no
# dynamic-capability declaration**: a capability that is not in `app.json` cannot be added to
# a device at all. So ten schemas are declared up front and each device adds or removes them
# at runtime (`add_capability` / `remove_capability`), which is what lets a lot with two
# favourites show exactly two buttons instead of ten, eight of them dead.
#
# The button's **label** is not its schema title. It is overwritten per device with
# `set_capability_options(..., {"title": …})` as a plain string (verified on hardware), because
# `엄마차` is user input and no static manifest can carry it.
#
# The schemas are `getable: false`, exactly like Homey's own `button`. That is what makes the
# tile draw a momentary push button rather than a latching switch, and it means **nothing in
# this app ever writes a value to one of them**: a press arrives at the capability listener and
# there is no state left behind to reset.

#: How many favourite slots a device has. Ten is a manifest fact before it is a preference:
#: raising it means more capability JSON files and two more settings fields per slot, so it is
#: **not** a tunable — `MAX_FAVORITES` and `.homeycompose/capabilities/iparking_quick_*.json`
#: have to be changed together or a device asks for a capability the app never declared, which
#: fails only on a hub. `tests/test_visitcar.py` compares this number against the files on disk
#: in both directions so a disagreement fails off-device instead.
MAX_FAVORITES = 10

#: How many times to retry writing a normalized plate back into the device settings.
#:
#: `Device.set_settings` raises `HomeyError("Cannot set settings while on_settings is still
#: pending")` while the SDK's `_on_settings` is running, and that method holds the flag across
#: `await self.on_settings(...)` — which our handler exits only after awaiting the button
#: reconcile. So a write scheduled from inside the handler reliably wakes up inside the forbidden
#: window. The flag is cleared in a `finally` right after the handler returns, so one short retry
#: is normally enough; the rest exist so a slow reconcile cannot silently drop the write.
#:
#: Verified against the runtime's own source, extracted from
#: `ghcr.io/athombv/python-homey-app-runner` (`site-packages/homey/device.py`), not inferred from
#: behaviour.
SETTINGS_WRITE_ATTEMPTS = 6
SETTINGS_WRITE_RETRY_S = 0.25


def favorite_name_setting(index: int) -> str:
    """The device-settings key holding 자주 오는 차량 이름 `index` (1-based)."""
    return f"fav_name_{int(index)}"


def favorite_plate_setting(index: int) -> str:
    """The device-settings key holding 차량번호 `index` (1-based)."""
    return f"fav_plate_{int(index)}"


def quick_capability(index: int) -> str:
    """The tile-button capability id for favourite slot `index` (1-based).

    Spelled here rather than inline anywhere so the device, the tests and the ten JSON
    schemas cannot drift: a typo produces a capability the app never declared, which the SDK
    refuses at `add_capability` time — i.e. only on a hub.
    """
    return f"iparking_quick_{int(index)}"
