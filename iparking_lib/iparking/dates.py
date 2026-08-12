"""Dates — KST is the sole authority, and the day is never guessed silently (§3.6).

**The host clock's timezone is never consulted.** A Homey Pro's clock need not be set to
Asia/Seoul, and "today" in this app does not mean today where the hub is: it means today at
a parking lot in Korea, which is the only thing the vendor's server will accept. So the
offset is a fixed `timezone(timedelta(hours=9))` — Korea has observed no DST since 1988,
which is why a fixed offset is correct here rather than merely convenient, and why this
module needs no tz database (the Homey runtime does not ship one).

## The three input shapes, and the one that is dangerous

| Shape | Source | Decidable? |
|---|---|---|
| `yyyy-mm-dd` | `<input type="date">` in `settings/form.js` | yes — first field is 4 digits |
| `dd-mm-yyyy` | the documented Homey Flow `date` argument | **shape-identical to the next row** |
| `mm-dd-yyyy` | a Homey build that sends US field order | **shape-identical to the previous row** |

Accepting two of them mitigates nothing against the third: `05-08-2026` is 5 August read one
way and 8 May read the other, both are real dates, and nothing anywhere reports an error. The
consequence is a visitor registered for the wrong day on a real building's access control,
discovered by the guest at a closed gate.

So the 2-2-4 shape is decided on the **values**, which narrows the undecidable window to the
case where both fields could be a month:

1. `field1 > 12` → field1 can only be a day → day-first.
2. `field2 > 12` → field2 can only be a day → month-first.
3. otherwise → **genuinely ambiguous.** Resolved as day-first, because that is the documented
   Homey format, and flagged `ambiguous=True` so the caller can put the interpretation in
   front of the user instead of hoping.

That flag is the actual mitigation, and it is why `to_api_date` returns an `ApiDate` rather
than a bare string: item 7's success notification echoes `format_kst_human()` back, so a
misparse is visible on **first use**. Note what this does and does not buy — roughly 60 % of
dates in a year fall in case 3, so the exposure is reduced, not removed. §3.6's five-minute
on-device probe at item 9 is still what settles the format; this module is what keeps a wrong
guess loud until then.

## Two entry points, deliberately

`to_api_date()` parses and disambiguates, and that is all. `resolve_visit_date()` adds the
visit-date window (not in the past, not beyond `MAX_DAYS_AHEAD`) and is what the register
path and the Flow card must call. They are separate because the window is a *policy about one
field*, not a property of the format: the 등록 내역 history query legitimately asks for dates
`HISTORY_DAYS_BACK` in the past *and* `HISTORY_DAYS_AHEAD` in the future — both outside what
`resolve_visit_date` allows, the second deliberately so (90 > `MAX_DAYS_AHEAD`) — and folding
the window into the parser would either break that query or push it into re-implementing date
parsing.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

#: Korea Standard Time. Fixed, not looked up: see the module docstring.
KST = timezone(timedelta(hours=9))

#: `invitationDate` / `startDate` / `endDate` on the wire (`docs/RECON.md` §3).
API_DATE_FORMAT = "%Y%m%d"

#: What `<input type="date">` reads and writes.
INPUT_DATE_FORMAT = "%Y-%m-%d"

#: How far ahead a 방문 예정일 may be. 80 rather than 90: the *read* window is documented as
#: 최근 3개월 (`docs/RECON.md` §4), and whether `POST /invitations` enforces the same bound has
#: never been exercised, so the margin is deliberate.
MAX_DAYS_AHEAD = 80

_WEEKDAYS_KO = ("월", "화", "수", "목", "금", "토", "일")
_WEEKDAYS_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class DateError(ValueError):
    """A date that cannot be used, carrying an i18n key for its message."""

    #: Key in `locales/{ko,en}.json`.
    key = "bad_date"


class PastDateError(DateError):
    key = "past_date"


class DateTooFarError(DateError):
    key = "date_too_far"

    def __init__(self, message: str, max_days: int = MAX_DAYS_AHEAD) -> None:
        super().__init__(message)
        #: Whoever renders `key` must pass this as the `days` placeholder.
        self.max_days = max_days


class ApiDate(str):
    """`yyyyMMdd` for the wire, which also remembers how it was read.

    A `str` subclass rather than a separate result object so that every caller and every
    `json.dumps` of a request body keeps working unchanged: `ApiDate(...) == "20260805"` is
    true, it hashes like that string, and it serialises as that string. The extra attributes
    exist for exactly one purpose — letting the caller show the user which date this is.
    """

    #: The parsed calendar date.
    resolved: date
    #: Which shape it was read as: `yyyy-mm-dd` / `dd-mm-yyyy` / `mm-dd-yyyy`.
    source_format: str
    #: True when a 2-2-4 input could equally have been the other field order (both ≤ 12).
    #: The caller is expected to say so, not to silently trust the resolution.
    ambiguous: bool

    def __new__(cls, resolved: date, source_format: str, *, ambiguous: bool) -> ApiDate:
        self = super().__new__(cls, resolved.strftime(API_DATE_FORMAT))
        self.resolved = resolved
        self.source_format = source_format
        self.ambiguous = ambiguous
        return self

    def __repr__(self) -> str:
        flag = ", ambiguous" if self.ambiguous else ""
        return f"ApiDate({str(self)!r}, {self.source_format}{flag})"


def now_kst() -> datetime:
    """The current instant as a KST-aware datetime."""
    return datetime.now(KST)


def today_kst() -> str:
    """Today in KST as `yyyy-mm-dd`.

    This is what `GET /status` reports and what `form.js` uses for both the `min` and the
    default of its date input. An `<input type="date">` value is a bare wall-clock string with
    no timezone attached, so handing the browser a KST date makes both surfaces agree for a
    user in any timezone.
    """
    return now_kst().strftime(INPUT_DATE_FORMAT)


def today_api() -> str:
    """Today in KST as `yyyyMMdd`, ready for the wire."""
    return now_kst().strftime(API_DATE_FORMAT)


def shift_api(api_date: str, days: int) -> str:
    """`api_date` moved by `days` (negative = earlier), still `yyyyMMdd`.

    Here rather than on the client because the device needs it too — today+1 for 내일 방문
    예정 and today+7 for 이번 주 — and a second copy is a second thing to get wrong at a
    month boundary.
    """
    parsed = datetime.strptime(str(api_date), API_DATE_FORMAT)
    return (parsed + timedelta(days=days)).strftime(API_DATE_FORMAT)


def to_api_date(value: str | None) -> ApiDate:
    """`yyyy-mm-dd`, `dd-mm-yyyy` or `mm-dd-yyyy` → `ApiDate` (`yyyyMMdd`).

    Disambiguates the 2-2-4 shape by value, per the module docstring, and flags the residual
    ambiguity rather than hiding it. Raises `DateError` on anything unparseable, including a
    real-looking date that does not exist (`31-02-2026`) — rejecting is safe here, guessing is
    not.

    Applies **no** window; `resolve_visit_date` is the one that does.
    """
    text = (value or "").strip()
    parts = text.split("-")
    # `isascii() and isdigit()` rather than `isdigit()` alone: "２０２６".isdigit() is True and
    # int() would happily accept it, while "²".isdigit() is True and int() would raise from
    # outside the try below.
    if len(parts) != 3 or not all(p.isascii() and p.isdigit() for p in parts):
        raise DateError(f"날짜 형식이 올바르지 않습니다: {text!r} (yyyy-mm-dd 또는 dd-mm-yyyy)")

    ambiguous = False
    if len(parts[0]) == 4:
        source_format = "yyyy-mm-dd"
        year, month, day = (int(p) for p in parts)
    elif len(parts[2]) == 4:
        first, second, year = (int(p) for p in parts)
        if first > 12:
            source_format, day, month = "dd-mm-yyyy", first, second
        elif second > 12:
            source_format, month, day = "mm-dd-yyyy", first, second
        else:
            # Undecidable from the values alone. Resolve day-first — the documented Homey
            # format — and make the caller responsible for showing its work.
            source_format, day, month, ambiguous = "dd-mm-yyyy", first, second, True
    else:
        # Neither end carries a four-digit year: a two-digit year would have to be guessed at
        # a century, and "05-08-26" is ambiguous three ways rather than two.
        raise DateError(f"연도를 판별할 수 없습니다: {text!r} (yyyy-mm-dd 또는 dd-mm-yyyy)")

    try:
        resolved = date(year, month, day)
    except ValueError as exc:
        raise DateError(f"존재하지 않는 날짜입니다: {text!r}") from exc
    return ApiDate(resolved, source_format, ambiguous=ambiguous)


def resolve_visit_date(value: str | None) -> ApiDate:
    """`to_api_date`, plus the 방문 예정일 window: today ≤ date ≤ today + `MAX_DAYS_AHEAD`.

    The entry point for the register path and the Flow card. Today itself is allowed — a visit
    later today is an ordinary thing to register.
    """
    api_date = to_api_date(value)
    today = now_kst().date()
    if api_date.resolved < today:
        raise PastDateError(f"방문 예정일이 지났습니다: {api_date.resolved.isoformat()}")
    if api_date.resolved > today + timedelta(days=MAX_DAYS_AHEAD):
        raise DateTooFarError(
            f"방문 예정일이 너무 멉니다: {api_date.resolved.isoformat()} "
            f"(최대 {MAX_DAYS_AHEAD}일 이후까지)"
        )
    return api_date


def is_past(api_date: str) -> bool:
    """True if `api_date` (`yyyyMMdd`) falls before today in KST.

    Zero-padded `yyyyMMdd` sorts chronologically, so the string comparison is exact and needs
    no parsing.
    """
    return api_date < today_api()


def format_kst_human(value: ApiDate | date | str, language: str = "ko") -> str:
    """`"2026-08-05 (수)"` — the date with its weekday, for showing back to the user.

    Item 7 puts this in the register success notification precisely so a misparsed Flow `date`
    argument is caught on first use: `2026-05-08 (금)` where the user meant 5 August is
    obvious at a glance, whereas `20260508` on the wire is not.

    Korean is the default because the service, the building and the maintainer are Korean; any
    other language code gets English, mirroring how `iparking_lib/i18n.py` falls back.
    """
    resolved = _as_date(value)
    names = _WEEKDAYS_KO if (language or "")[:2].lower() == "ko" else _WEEKDAYS_EN
    return f"{resolved.isoformat()} ({names[resolved.weekday()]})"


def _as_date(value: ApiDate | date | str) -> date:
    if isinstance(value, ApiDate):
        return value.resolved
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, API_DATE_FORMAT).date()
    except ValueError as exc:
        raise DateError(f"날짜를 해석할 수 없습니다: {text!r} (yyyyMMdd)") from exc
