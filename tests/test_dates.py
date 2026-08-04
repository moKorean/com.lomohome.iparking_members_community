"""Dates — §3.6, acceptance criterion 10, and task #12's three-shape disambiguation.

Three failures are being guarded against, and none of them announces itself:

* `dd-mm-yyyy` silently read as `mm-dd-yyyy`, or the reverse. The shapes are identical, the
  result is a valid date, and the consequence is a visitor registered for the wrong day on a
  real building's access control.
* "today" taken from the host clock's timezone. A Homey Pro in another timezone would then
  register for yesterday or tomorrow, and only near midnight, which is the worst possible
  reproduction rate.
* A date the *read* API tolerates but the *write* API might not (`MAX_DAYS_AHEAD`).

Inputs for the window tests are built relative to `now_kst()` rather than written as
literals. A suite that hardcodes "a date 90 days out" starts failing on a specific calendar
day for reasons that have nothing to do with the code, and a test that expires is worse than
no test — someone deletes it in a hurry.

`tests/conftest.py` puts the repo root on `sys.path`; nothing here does its own.
"""

import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from iparking_lib.iparking.dates import (
    KST,
    MAX_DAYS_AHEAD,
    ApiDate,
    DateError,
    DateTooFarError,
    PastDateError,
    format_kst_human,
    is_past,
    now_kst,
    resolve_visit_date,
    to_api_date,
    today_api,
    today_kst,
)


def _under_host_tz(tz_name, fn):
    """Run `fn` with the process's local timezone set to `tz_name`."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz_name
    time.tzset()
    try:
        return fn()
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _utc_plus_nine(fmt):
    return (datetime.now(UTC) + timedelta(hours=9)).strftime(fmt)


def _offset(days):
    """A date `days` from today in KST, as (yyyy-mm-dd, dd-mm-yyyy, yyyyMMdd)."""
    target = now_kst().date() + timedelta(days=days)
    return (
        target.strftime("%Y-%m-%d"),
        target.strftime("%d-%m-%Y"),
        target.strftime("%Y%m%d"),
    )


# --- to_api_date: the two originally specified formats ----------------------------


def test_criterion_10_both_input_formats():
    """The two assertions criterion 10 names, and the reason they are a pair.

    `05-08-2026` is 5 August in dd-mm-yyyy and 8 May in mm-dd-yyyy. Asserting both formats
    land on the same day is what proves the discriminator is width-based rather than
    US-centric — one assertion alone would pass under either reading.
    """
    assert to_api_date("05-08-2026") == "20260805"
    assert to_api_date("2026-08-05") == "20260805"
    assert to_api_date("05-08-2026") == to_api_date("2026-08-05")


@pytest.mark.parametrize(
    "flow_value,browser_value,expected",
    [
        ("05-08-2026", "2026-08-05", "20260805"),
        # Both fields ≤ 12, so a mm-dd reading would give 20260112 vs 20261201 — this pair
        # pins the day and the month independently.
        ("12-01-2026", "2026-01-12", "20260112"),
        ("01-12-2026", "2026-12-01", "20261201"),
        # A day > 12 cannot be a month; a reading that got here by luck breaks above.
        ("31-12-2026", "2026-12-31", "20261231"),
        ("29-02-2028", "2028-02-29", "20280229"),
    ],
)
def test_flow_and_browser_formats_agree(flow_value, browser_value, expected):
    assert to_api_date(flow_value) == expected
    assert to_api_date(browser_value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("5-8-2026", "20260805"),        # unpadded, as a hand-typed Flow value might be
        ("2026-8-5", "20260805"),
        ("  05-08-2026  ", "20260805"),  # surrounding whitespace
    ],
)
def test_tolerated_variations(value, expected):
    assert to_api_date(value) == expected


@pytest.mark.parametrize(
    "value,why",
    [
        ("31-02-2026", "February has no 31st — a valid shape is not a valid date"),
        ("2026-02-31", "same, browser format"),
        ("2026-13-01", "month 13"),
        ("29-02-2026", "2026 is not a leap year"),
        ("13-13-2026", "neither field can be a month"),
        ("00-08-2026", "day zero"),
        ("05/08/2026", "slashes are not the separator"),
        ("20260805", "no separators: not a format either surface produces"),
        ("05-08-26", "two-digit year is ambiguous three ways"),
        ("2026-08", "two fields"),
        ("2026-08-05-01", "four fields"),
        ("", "empty"),
        (None, "absent"),
        ("today", "not a date"),
        ("２０２６-08-05", "full-width digits: isdigit() is True and int() would accept"),
    ],
)
def test_unparseable_values_are_refused(value, why):
    with pytest.raises(DateError):
        to_api_date(value)
    # DateError is a ValueError, so callers that catch the broad type keep working.
    with pytest.raises(ValueError):
        to_api_date(value)


def test_round_trip_through_today():
    """`today_kst()` is what the browser holds; feeding it back must give `today_api()`."""
    assert to_api_date(today_kst()) == today_api()


# --- task #12: disambiguating the 2-2-4 shape by value ----------------------------


def test_field1_over_12_forces_day_first():
    """`25-12-2026`: 25 cannot be a month, so the shape is decidable — no flag needed."""
    resolved = to_api_date("25-12-2026")
    assert resolved == "20261225"
    assert resolved.resolved == date(2026, 12, 25)
    assert resolved.source_format == "dd-mm-yyyy"
    assert resolved.ambiguous is False


def test_field2_over_12_forces_month_first():
    """`12-25-2026`: the same day, arriving from a build that sends US field order."""
    resolved = to_api_date("12-25-2026")
    assert resolved == "20261225"
    assert resolved.resolved == date(2026, 12, 25)
    assert resolved.source_format == "mm-dd-yyyy"
    assert resolved.ambiguous is False


def test_the_two_orders_of_the_same_day_agree():
    """The point of the value-based rules: both spellings of 25 December land on it."""
    assert to_api_date("25-12-2026") == to_api_date("12-25-2026") == "20261225"


def test_both_fields_under_13_is_flagged_ambiguous():
    """`05-08-2026` is undecidable; it resolves day-first *and says so*.

    This flag is the whole mitigation. It is the difference between a wrong day discovered by
    a guest at a closed gate and a wrong day visible in the confirmation text on first use.
    """
    resolved = to_api_date("05-08-2026")
    assert resolved == "20260805"                     # documented Homey assumption
    assert resolved.source_format == "dd-mm-yyyy"
    assert resolved.ambiguous is True


def test_browser_format_is_never_ambiguous():
    """A four-digit first field decides the whole shape, whatever the other values are."""
    for value in ("2026-08-05", "2026-01-12", "2026-12-01"):
        assert to_api_date(value).ambiguous is False
        assert to_api_date(value).source_format == "yyyy-mm-dd"


@pytest.mark.parametrize(
    "value,expected_format,expected_ambiguous",
    [
        ("25-12-2026", "dd-mm-yyyy", False),
        ("12-25-2026", "mm-dd-yyyy", False),
        ("05-08-2026", "dd-mm-yyyy", True),
        ("13-01-2026", "dd-mm-yyyy", False),   # boundary: 13 is the first non-month
        ("01-13-2026", "mm-dd-yyyy", False),
        ("12-12-2026", "dd-mm-yyyy", True),    # boundary: 12 is the last month, both sides
        ("31-01-2026", "dd-mm-yyyy", False),
    ],
)
def test_ambiguity_boundaries(value, expected_format, expected_ambiguous):
    resolved = to_api_date(value)
    assert resolved.source_format == expected_format
    assert resolved.ambiguous is expected_ambiguous


def test_rule_one_wins_and_the_result_is_not_retried_the_other_way():
    """`25-13-2026` is day-first by rule 1, and 13 is then not a month, so it is refused.

    Stated as a test because the tempting alternative — falling back to the other order —
    would turn a typo into a silently different, perfectly plausible date.
    """
    with pytest.raises(DateError):
        to_api_date("25-13-2026")


# --- ApiDate behaves like the string it replaced ----------------------------------


def test_api_date_is_a_string_everywhere_it_matters():
    """The compatibility contract: existing callers and JSON bodies must not notice.

    `invitationDate` goes into the encrypted request body via `json.dumps`, so a subclass that
    serialised as an object would produce a body the vendor rejects.
    """
    resolved = to_api_date("2026-08-05")
    assert isinstance(resolved, str)
    assert isinstance(resolved, ApiDate)
    assert resolved == "20260805"
    assert "20260805" == resolved
    assert hash(resolved) == hash("20260805")
    assert {resolved: 1}["20260805"] == 1
    assert json.dumps({"invitationDate": resolved}) == '{"invitationDate": "20260805"}'
    assert f"{resolved}" == "20260805"
    assert resolved[:4] == "2026"
    # repr carries the provenance, because that is what a diagnostic line needs to show.
    assert "dd-mm-yyyy" in repr(to_api_date("05-08-2026"))
    assert "ambiguous" in repr(to_api_date("05-08-2026"))
    assert "ambiguous" not in repr(to_api_date("25-12-2026"))


# --- resolve_visit_date: the window ----------------------------------------------


def test_today_is_inside_the_window():
    """A visit later today is an ordinary registration, not a past date."""
    browser, flow, expected = _offset(0)
    assert resolve_visit_date(browser) == expected
    assert resolve_visit_date(flow) == expected


@pytest.mark.parametrize("days", [1, 2, 30, MAX_DAYS_AHEAD - 1, MAX_DAYS_AHEAD])
def test_dates_up_to_the_bound_are_accepted(days):
    browser, _flow, expected = _offset(days)
    assert resolve_visit_date(browser) == expected


@pytest.mark.parametrize("days", [MAX_DAYS_AHEAD + 1, MAX_DAYS_AHEAD + 10, 365])
def test_dates_beyond_the_bound_are_refused(days):
    browser, _flow, _expected = _offset(days)
    with pytest.raises(DateTooFarError) as excinfo:
        resolve_visit_date(browser)
    assert excinfo.value.key == "date_too_far"
    # The renderer needs the number for the `{days}` placeholder in locales/*.json.
    assert excinfo.value.max_days == MAX_DAYS_AHEAD


def test_the_bound_is_80_not_90():
    """Pinned as its own assertion because the *reason* is unverified, not arbitrary.

    `docs/RECON.md` §4 documents the 최근 3개월 window on the history *read*. Whether
    `POST /invitations` enforces the same bound has never been exercised, so the margin is
    deliberate and must not be "corrected" to 90 without a probe.
    """
    assert MAX_DAYS_AHEAD == 80


@pytest.mark.parametrize("days", [-1, -2, -400])
def test_past_dates_are_refused(days):
    browser, flow, _expected = _offset(days)
    for value in (browser, flow):
        with pytest.raises(PastDateError) as excinfo:
            resolve_visit_date(value)
        assert excinfo.value.key == "past_date"


def test_window_errors_are_distinguishable_from_format_errors():
    """Three different keys, because they need three different sentences to the user."""
    assert len({DateError.key, PastDateError.key, DateTooFarError.key}) == 3
    assert issubclass(PastDateError, DateError)
    assert issubclass(DateTooFarError, DateError)
    assert issubclass(DateError, ValueError)


def test_resolve_visit_date_still_reports_ambiguity():
    """The window must not swallow the flag — the confirmation text depends on it.

    Uses a day-of-month ≤ 12 within the window so that an ambiguous spelling exists at all;
    such a day always exists in the next 12 days, so this is a real assertion every day of
    the year rather than a conditional one.
    """
    today = now_kst().date()
    target = next(
        today + timedelta(days=n) for n in range(1, 13) if (today + timedelta(days=n)).day <= 12
    )
    resolved = resolve_visit_date(target.strftime("%d-%m-%Y"))
    assert resolved.ambiguous is True
    assert resolved == target.strftime("%Y%m%d")


def test_resolve_visit_date_rejects_garbage_before_checking_the_window():
    with pytest.raises(DateError):
        resolve_visit_date("nope")


# --- format_kst_human ------------------------------------------------------------


@pytest.mark.parametrize(
    "day,ko,en",
    [
        ("2026-08-03", "월", "Mon"),
        ("2026-08-04", "화", "Tue"),
        ("2026-08-05", "수", "Wed"),
        ("2026-08-06", "목", "Thu"),
        ("2026-08-07", "금", "Fri"),
        ("2026-08-08", "토", "Sat"),
        ("2026-08-09", "일", "Sun"),
    ],
)
def test_weekday_names_in_both_locales(day, ko, en):
    """A full week, so an off-by-one in the weekday table cannot hide."""
    resolved = to_api_date(day)
    assert format_kst_human(resolved) == f"{day} ({ko})"
    assert format_kst_human(resolved, "ko") == f"{day} ({ko})"
    assert format_kst_human(resolved, "en") == f"{day} ({en})"


def test_format_kst_human_matches_the_specified_example():
    assert format_kst_human(to_api_date("05-08-2026")) == "2026-08-05 (수)"


@pytest.mark.parametrize(
    "value",
    [
        "20260805",
        ApiDate(date(2026, 8, 5), "yyyy-mm-dd", ambiguous=False),
        date(2026, 8, 5),
    ],
)
def test_format_kst_human_accepts_wire_strings_api_dates_and_dates(value):
    assert format_kst_human(value) == "2026-08-05 (수)"


@pytest.mark.parametrize("language", ["en-US", "EN", "de", "", None])
def test_unknown_languages_fall_back_to_english(language):
    """Mirrors `iparking_lib/i18n.py`: an unknown language falls back rather than raising."""
    assert format_kst_human(to_api_date("2026-08-05"), language) == "2026-08-05 (Wed)"


def test_korean_is_the_default():
    assert format_kst_human(to_api_date("2026-08-05")) == "2026-08-05 (수)"


def test_format_kst_human_refuses_what_it_cannot_read():
    with pytest.raises(DateError):
        format_kst_human("2026-08-05")   # separators: this function takes the wire form


# --- the locale keys these errors name -------------------------------------------


@pytest.mark.parametrize("language", ["ko", "en"])
def test_every_date_error_key_has_text(language):
    """A raised key that renders as itself is a user-visible defect, not a missing nicety."""
    path = Path(__file__).resolve().parents[1] / "locales" / f"{language}.json"
    assert path.exists(), f"{path} is required (item 8)"
    table = json.loads(path.read_text(encoding="utf-8"))
    for key in (DateError.key, PastDateError.key, DateTooFarError.key):
        assert table.get(key), f"locales/{language}.json has no text for {key!r}"
    # `date_too_far` must keep its placeholder: the bound lives in MAX_DAYS_AHEAD, and text
    # that spelled "80" out would drift the moment a probe moves the bound.
    assert "{days}" in table[DateTooFarError.key]


# --- KST as the sole authority ----------------------------------------------------


def test_kst_is_a_fixed_nine_hour_offset():
    assert KST.utcoffset(None) == timedelta(hours=9)
    # Korea has observed no DST since 1988, which is why a fixed offset is correct here and
    # not merely convenient — midwinter and midsummer must give the same offset.
    for probe in (datetime(2026, 1, 15), datetime(2026, 7, 15)):
        assert KST.utcoffset(probe) == timedelta(hours=9)
    assert now_kst().utcoffset() == timedelta(hours=9)
    assert now_kst().tzinfo is KST


def test_today_is_derived_from_utc_plus_nine():
    """Computed from UTC arithmetic, which no host TZ setting can influence.

    The before/after pair exists only so that a run straddling KST midnight is reported as a
    pass rather than a flake; it cannot mask a wrong offset, because a wrong offset lands
    outside both bounds for all but a few seconds of the day.
    """
    before = _utc_plus_nine("%Y-%m-%d")
    value = today_kst()
    after = _utc_plus_nine("%Y-%m-%d")
    assert value in {before, after}
    assert today_api() in {before.replace("-", ""), after.replace("-", "")}


def test_host_timezone_does_not_change_today():
    """Honolulu is UTC-10 and Kiritimati is UTC+14 — 24 hours apart.

    If the host clock's timezone leaked into `today_kst()`, these would not agree.
    """
    before = _utc_plus_nine("%Y-%m-%d")
    values = {
        name: _under_host_tz(name, today_kst)
        for name in ("UTC", "Pacific/Honolulu", "Pacific/Kiritimati", "Asia/Seoul")
    }
    after = _utc_plus_nine("%Y-%m-%d")
    assert set(values.values()) <= {before, after}, values


def test_host_timezone_does_not_change_the_window():
    """The same guarantee for the bound, which is computed from `now_kst()` too.

    A leaked host timezone would push the far edge of the window a day either way, so a date
    the maintainer can legitimately register would be refused on a hub set to Honolulu.
    """
    browser, _flow, expected = _offset(MAX_DAYS_AHEAD - 1)
    for name in ("UTC", "Pacific/Honolulu", "Pacific/Kiritimati"):
        assert _under_host_tz(name, lambda: resolve_visit_date(browser)) == expected


def test_api_format_has_no_separators():
    assert today_api() == today_kst().replace("-", "")
    assert len(today_api()) == 8 and today_api().isdigit()


# --- is_past ---------------------------------------------------------------------


def test_is_past():
    assert is_past("19700101") is True
    assert is_past(today_api()) is False       # today is not past — a visit today is legal
    assert is_past("29991231") is False
    yesterday = (now_kst() - timedelta(days=1)).strftime("%Y%m%d")
    assert is_past(yesterday) is True
