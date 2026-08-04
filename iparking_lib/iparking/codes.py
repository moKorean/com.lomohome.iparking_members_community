"""Vendor result codes → i18n keys, and per-car outcome parsing.

Two jobs, both of them translation from the vendor's vocabulary into ours.

**Result codes.** Every code `Ajax.resultCode` knows (`docs/RECON.md`) is mapped, so no
user-visible failure can degrade to a bare number. `2031` / `2041` / `1009` are singled
out as *token expired* because they are the only ones a re-login can fix — everything else
re-logging in would just repeat.

**Per-car outcomes.** `POST /invitations` takes `invitationInfoList` as an array and answers
per car with `SUCCESS` / `FAIL` / `EXIST`. `parse_per_car` is deliberately **shape-tolerant**:
`docs/RECON.md` establishes *that* those three values exist (they are in the bundle's own
strings) but **not where in the response body they appear** — the write endpoint has never
been exercised. Item 3's probe pins that down; until it does, guessing one shape and reading
`None` from the real one would silently look like "no result for this car". So several
plausible containers and key spellings are searched, and anything unrecognised is simply
**absent** from the returned mapping. Absent means *unknown*, and §3.5 requires the caller
to treat unknown as `RegisterUncertain` — never as success, never as generic failure.

`EXIST`, and a top-level `10003`, both map to a distinct **`already_registered`** outcome
rather than to failure. Re-entering a plate that is already registered is the single most
likely real outcome of the maintainer's first use of this app; reporting it as an error
would teach the user that the app is broken on their very first try, and (per item 7) would
make a Flow read a benign duplicate as a failed action.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .plate import strip_plate

#: `result` on a successful call.
SUCCESS = "0000"

#: 기등록 차량 — top-level form of the per-car `EXIST`.
REGISTERED_CAR = "10003"

#: Codes that mean the 7-day token is gone. The only codes a single re-login retry
#: answers; see §3.1 and criterion 5.
AUTH_EXPIRED = frozenset({"2031", "2041", "1009"})

#: Every code in `docs/RECON.md`'s table, mapped to a key in `locales/{ko,en}.json`.
#: `10003` is the one entry that is not an `error.*` key: it is not an error but a third
#: outcome, and it shares its key with the per-car `EXIST` path so both render the same
#: sentence.
RESULT_KEYS: dict[str, str] = {
    SUCCESS: "error.success",
    "1001": "error.fail",
    "1002": "error.db_error",
    "1009": "error.session_exit",
    "2001": "error.no_id",
    "2002": "error.login_error",
    "2031": "error.token_not_find",
    "2041": "error.token_user_not_find",
    "2042": "error.password_error",
    REGISTERED_CAR: "already_registered",
    "12100": "error.not_find_store",
    "12105": "error.not_allowed",
    "13001": "error.already_deleted",
    "13002": "error.cannot_delete",
}

#: Fallback key for a code the vendor added after this recon.
UNKNOWN_KEY = "error.unknown"

# --- Per-car outcomes -------------------------------------------------------------
# These strings are the app's own vocabulary, not the vendor's. The latter two double as
# i18n keys (item 8).
OUTCOME_OK = "ok"
OUTCOME_ALREADY_REGISTERED = "already_registered"
OUTCOME_FAILED = "register_failed"

#: The per-car status words the bundle knows.
PER_CAR_STATUS: dict[str, str] = {
    "SUCCESS": OUTCOME_OK,
    "EXIST": OUTCOME_ALREADY_REGISTERED,
    "FAIL": OUTCOME_FAILED,
}

# Containers `invitationInfoList` rows might arrive in, most specific first. Order barely
# matters because a candidate is only accepted if it actually looks like a list of car
# rows, but preferring the named key over the generic `resultData` keeps the intent clear.
_ROW_PATHS: tuple[tuple[str, ...], ...] = (
    ("invitationInfoList",),
    ("resultData", "invitationInfoList"),
    ("resultData",),
    ("resultData", "list"),
    ("list",),
)

_CAR_KEYS = ("carNumber", "car_number")
_STATUS_KEYS = ("result", "status", "resultCode")


def normalize_code(code: object) -> str:
    """A result code as the comparable string the API documents it to be.

    Tolerates an int (`10003`) because JSON numbers are plausible from a deployment that
    differs from the one we probed. Does **not** zero-pad: turning `0` into `"0000"` would
    be inventing a success out of a value nobody has observed.
    """
    return "" if code is None else str(code).strip()


def is_success(code: object) -> bool:
    """True only for exactly `0000`."""
    return normalize_code(code) == SUCCESS


def is_auth_expired(code: object) -> bool:
    """True for `2031` / `2041` / `1009` — re-login and retry once, no more."""
    return normalize_code(code) in AUTH_EXPIRED


def result_key(code: object) -> str:
    """The i18n key for `code`, or `UNKNOWN_KEY` if the vendor has grown a new one."""
    return RESULT_KEYS.get(normalize_code(code), UNKNOWN_KEY)


def per_car_outcome(status: object) -> str | None:
    """One row's status → an outcome, or `None` when it cannot be understood.

    Accepts the status words (`SUCCESS`/`FAIL`/`EXIST`, any case) and also a numeric
    result code, since the row key may be spelled `resultCode` and that name strongly
    suggests the response carries codes rather than words in at least one shape.

    `None` is the honest answer for anything else and must not be collapsed into a
    failure: the recovery re-query in §3.5 exists precisely to resolve "unknown".
    """
    text = normalize_code(status)
    if not text:
        return None
    word = PER_CAR_STATUS.get(text.upper())
    if word is not None:
        return word
    if is_success(text):
        return OUTCOME_OK
    if text == REGISTERED_CAR:
        return OUTCOME_ALREADY_REGISTERED
    if text in RESULT_KEYS:
        return OUTCOME_FAILED
    return None


def _dig(payload: dict, path: Sequence[str]) -> object:
    node: object = payload
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _row_lists(payload: dict) -> list[list]:
    """Every container in `_ROW_PATHS` that actually holds car rows."""
    found = []
    for path in _ROW_PATHS:
        node = _dig(payload, path)
        if not isinstance(node, list):
            continue
        if any(
            isinstance(row, dict) and any(k in row for k in _CAR_KEYS) for row in node
        ):
            found.append(node)
    return found


def parse_per_car(payload: object, requested: Iterable[str] = ()) -> dict[str, str]:
    """Per-car outcomes from a `POST /invitations` response, keyed by normalized plate.

    `requested` is the plates that were sent. It is used only for a top-level
    `10003`, which is a verdict on the whole request with no per-car rows to hang it on;
    an explicit row always wins over it.

    A plate missing from the returned mapping means the response did not say — the caller
    must re-query rather than assume either way.
    """
    if not isinstance(payload, dict):
        return {}
    outcomes: dict[str, str] = {}
    for rows in _row_lists(payload):
        for row in rows:
            if not isinstance(row, dict):
                continue
            raw_plate = next((row[k] for k in _CAR_KEYS if row.get(k)), None)
            if raw_plate is None:
                continue
            # Both sides normalized before they are ever compared (§3.5): the plate we
            # sent went through `normalize_plate`, and this one came back from the server.
            plate = strip_plate(str(raw_plate))
            if not plate:
                continue
            status = next((row[k] for k in _STATUS_KEYS if row.get(k) is not None), None)
            outcome = per_car_outcome(status)
            if outcome is not None:
                outcomes.setdefault(plate, outcome)
    if normalize_code(payload.get("result")) == REGISTERED_CAR:
        for plate in requested:
            stripped = strip_plate(plate)
            if stripped:
                outcomes.setdefault(stripped, OUTCOME_ALREADY_REGISTERED)
    return outcomes
