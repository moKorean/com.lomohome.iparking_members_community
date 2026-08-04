"""The register path — acceptance criteria 6 and 7.

This is the highest-consequence code in the app, and almost every assertion here is about
something that must **not** happen. That is why it lives in its own file: mixed in with
ordinary plumbing tests, a negative assertion reads like noise and gets deleted.

The harm being designed against, in both directions:

* **An orphaned write.** `asyncio.wait_for` cancels the *await*, not the
  `run_in_executor` thread beneath it, so a `POST /invitations` that "timed out" may still
  land. The orphan is a vehicle actually registered at a real building after the user was
  told it failed. The answer is a *read* — the recovery re-query — and never a second write.
* **A false verdict about whether a car is registered.** Reporting an unregistered car as
  registered puts a visitor in front of a gate that will not open. Reporting a registered
  car as failed invites a retry that creates a second real registration. The existence
  predicate has to get both directions right, and its first proposed form got one wrong.

`conftest.slow()` blocks the executor thread for real rather than faking a timeout, so the
POST genuinely is still in flight when the budget fires.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
from datetime import timedelta

import pytest
from conftest import (
    HISTORY_URL,
    LOTS_URL,
    OAUTH_URL,
    PARK_SEQ,
    REGISTER_URL,
    STOR_SEQ,
    envelope,
    history_ok,
    login_ok,
    lots_ok,
    slow,
)

from iparking_lib.const import MAX_WRITES_PER_HOUR, RECOVERY_SLEEP_S
from iparking_lib.iparking import client as client_module
from iparking_lib.iparking import crypto, dates
from iparking_lib.iparking.client import (
    IparkingError,
    RegisterUncertain,
    WriteBudgetError,
)
from iparking_lib.iparking.plate import InvalidPlateError

PLATE = "12가3456"


def _offset(days: int) -> tuple[str, str]:
    """`(input_form, wire_form)` for a date `days` from today **in KST**.

    Computed rather than hardcoded because `dates.resolve_visit_date` enforces a real window
    (not past, not beyond `MAX_DAYS_AHEAD` = 80). A literal `"2026-08-05"` would quietly turn
    this whole file red a few weeks from now, and the failure would look like a register-path
    bug rather than an expired fixture.

    The input form is `yyyy-mm-dd`, which is the one shape `to_api_date` can decide without
    ambiguity — the 2-2-4 ambiguity is `test_dates.py`'s subject, not this file's.
    """
    resolved = dates.now_kst().date() + timedelta(days=days)
    return resolved.isoformat(), resolved.strftime(dates.API_DATE_FORMAT)


DATE_INPUT, DATE = _offset(1)          # tomorrow — an ordinary 방문 예정일
FUTURE_INPUT, FUTURE = _offset(60)     # well ahead, still inside MAX_DAYS_AHEAD
PAST_INPUT, _PAST = _offset(-1)


def register(api, *, plate=PLATE, date=DATE_INPUT, **kwargs):
    return asyncio.run(
        api.register(car_number=plate, park_seq=PARK_SEQ, stor_seq=STOR_SEQ,
                     visit_date=date, **kwargs)
    )


def per_car(status, plate=PLATE):
    """A `POST /invitations` response carrying an explicit per-car verdict."""
    return envelope("0000", "성공", invitationInfoList=[{"carNumber": plate, "result": status}])


@pytest.fixture
def fast_budgets(monkeypatch):
    """Shrink the two budgets so the timeout paths run in milliseconds.

    Patched on the client module (which imported the names) rather than on `const`, and
    kept **asymmetric** — 0.05 s for the attempt, 1.0 s for the recovery — because the
    property under test is that the second budget is *fresh*, not merely that both exist.
    """
    monkeypatch.setattr(client_module, "REGISTER_TIMEOUT_S", 0.05)
    monkeypatch.setattr(client_module, "RECOVERY_TIMEOUT_S", 1.0)


# --- criterion 6: zero retries ------------------------------------------------


def test_a_successful_register_issues_exactly_one_post(make_api):
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS"),
    })

    assert register(api) == "ok"
    assert stub.count(REGISTER_URL) == 1


def test_a_network_error_on_the_write_is_never_retried(make_api, no_sleep):
    """One POST, then a **read** to find out what it did. Not a second POST.

    A `URLError` is exactly the case where retrying feels safe and is not: the request may
    have reached the server and failed on the way back.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("connection reset"),
        HISTORY_URL: history_ok(((PLATE, DATE, "RESERVE"),)),
    })

    assert register(api) == "already_registered"
    assert stub.count(REGISTER_URL) == 1, "the write must never be re-sent"
    assert stub.count(HISTORY_URL) == 1, "the recovery is a read"


def test_a_timeout_on_the_write_is_never_retried(make_api, no_sleep, fast_budgets):
    """The orphan case, reproduced rather than simulated.

    The route blocks a real executor thread, so when the 0.05 s budget fires the POST is
    genuinely still in flight — which is the whole reason the recovery exists.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: slow(per_car("SUCCESS"), 0.15),
        HISTORY_URL: history_ok(((PLATE, DATE, "RESERVE"),)),
    })

    assert register(api) == "already_registered"
    assert stub.count(REGISTER_URL) == 1
    assert stub.count(HISTORY_URL) == 1


def test_an_expired_token_on_the_write_does_not_resend_it(make_api, no_sleep):
    """`_authed`'s one-re-login retry must NOT apply to this endpoint.

    A read is free to retry; a write is a second vehicle at a building. So `register` does
    not go through `_authed` at all, and a `2031` here goes to the recovery re-query like
    any other unsettled answer.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: envelope("2031", "tokenNotFind"),
        HISTORY_URL: history_ok(),
    })

    with pytest.raises(RegisterUncertain):
        register(api)

    assert stub.count(REGISTER_URL) == 1, "an expired token must not re-send the write"


def test_a_refused_redirect_on_the_write_is_never_retried(make_api, no_sleep):
    """A 301 on the POST is refused by `StrictRedirectHandler`, and that refusal is *not*
    an invitation to try again — urllib would have re-sent it as a bodyless GET."""
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: (301, {"location": f"{REGISTER_URL}/moved"}, b""),
        HISTORY_URL: history_ok(),
    })

    with pytest.raises(RegisterUncertain):
        register(api)

    assert stub.count(REGISTER_URL) == 1


def test_the_eleventh_write_in_an_hour_is_refused(make_api):
    """The **secondary** ceiling. Zero-retries is the actual guarantee; this is a second
    wall, and it is reset by the restart-with-backoff loop — accepted, not overlooked."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    for _ in range(MAX_WRITES_PER_HOUR):
        assert register(api) == "ok"
    sent = stub.count(REGISTER_URL)

    with pytest.raises(WriteBudgetError):
        register(api)

    assert sent == MAX_WRITES_PER_HOUR == 10
    assert stub.count(REGISTER_URL) == sent, "the refused attempt must not reach the network"


def test_an_attempt_that_may_have_landed_still_counts_against_the_ceiling(
    make_api, no_sleep, fast_budgets
):
    """The timestamp is recorded before the attempt, not after it succeeds.

    A write that timed out may have reached the server, so a ceiling that only counted
    confirmed successes would not bound the number of registrations actually created.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: slow(per_car("SUCCESS"), 0.15),
        HISTORY_URL: history_ok(((PLATE, DATE, "RESERVE"),)),
    })

    register(api)

    assert len(api._write_times) == 1


def test_the_write_budget_is_a_rolling_window(make_api, monkeypatch):
    """Old attempts age out, so a user is not locked out for the rest of the process's life."""
    api, _stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})
    for _ in range(MAX_WRITES_PER_HOUR):
        register(api)

    # Every recorded attempt is now older than the window.
    api._write_times = [t - 3601 for t in api._write_times]

    assert register(api) == "ok"


def test_the_recovery_runs_on_a_fresh_budget_not_the_expired_one(
    make_api, no_sleep, fast_budgets
):
    """Criterion 6's third clause, and the reason the budgets are **sequential**.

    The attempt's budget is 0.05 s and the recovery's query takes 0.15 s — three times
    longer. If the recovery were nested inside the attempt's `wait_for`, or if both shared
    one budget, it would be cancelled before it could answer. It completing is the proof.

    Which matters because the wait that fires *because* the attempt hung must not also bound
    the query sent to discover what the attempt did.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: slow(per_car("SUCCESS"), 0.15),
        HISTORY_URL: slow(history_ok(((PLATE, DATE, "RESERVE"),)), 0.15),
    })

    started = time.monotonic()
    outcome = register(api)
    elapsed = time.monotonic() - started

    assert outcome == "already_registered"
    assert stub.count(HISTORY_URL) == 1
    assert elapsed > 0.05, "the recovery outlived the attempt's budget, as it must"


def test_the_recovery_pauses_before_re_querying(make_api, no_sleep, fast_budgets):
    """Three seconds, so a write the server is still committing becomes visible to a read.

    Without the pause the re-query can miss a registration that did land, which reports a
    success as `RegisterUncertain` — safe, but needlessly alarming, and it sends the user to
    the web UI for nothing.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: slow(per_car("SUCCESS"), 0.15),
        HISTORY_URL: history_ok(((PLATE, DATE, "RESERVE"),)),
    })

    register(api)

    assert RECOVERY_SLEEP_S in no_sleep
    assert RECOVERY_SLEEP_S == 3.0


def test_the_kill_flag_stops_the_write_before_the_network(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})
    asyncio.run(api.login())
    api.disabled = True

    with pytest.raises(IparkingError):
        register(api)

    assert stub.count(REGISTER_URL) == 0


# --- criterion 7: the existence predicate ------------------------------------
#
# Four cases, and each one is a defect that was actually proposed and rejected during
# review. The predicate is `any(row.status in {RESERVE, IN, OUT} for row in matching)`:
# **existential**, over **all** matching rows, with `CANCEL` excluded.


def test_a_cancel_only_match_is_uncertain_and_never_already_registered(
    make_api, no_sleep
):
    """The gate-that-will-not-open case.

    `register → 취소 → re-register` is easy, given the per-row 취소 button. If `CANCEL`
    counted as existence, the second registration would report `already_registered` while
    **nothing is registered**, and a visitor would arrive at a closed gate.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok(((PLATE, DATE, "CANCEL"),)),
    })

    with pytest.raises(RegisterUncertain):
        register(api)


def test_a_cancel_coexisting_with_an_active_row_reads_as_registered(make_api, no_sleep):
    """The same harm with the sign flipped — and the defect that survived a review round.

    `CANCEL` rows **coexist** with active ones for the same plate and date. A "find the row,
    then check its status" lookup can land on the `CANCEL` row and report a write that
    *succeeded* as failed, which invites the retry that creates a second real registration.
    The `CANCEL` row is deliberately listed **first** here so a first-match implementation
    fails this test.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok((
            (PLATE, DATE, "CANCEL"),
            (PLATE, DATE, "RESERVE"),
        )),
    })

    assert register(api) == "already_registered"


@pytest.mark.parametrize("status", ["RESERVE", "IN", "OUT"])
def test_every_active_status_counts_as_registered(make_api, no_sleep, status):
    """`OUT` (출차) included: the visitor came and left, so the registration existed."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok(((PLATE, DATE, status),)),
    })

    assert register(api) == "already_registered"


def test_a_requery_miss_is_uncertain_and_never_register_failed(make_api, no_sleep):
    """`register_failed` would invite a retry, and a retry is what turns one uncertain
    write into two real registrations. So a miss gets its own outcome."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok(),
    })

    with pytest.raises(RegisterUncertain) as caught:
        register(api)

    assert caught.value.key == "register_uncertain"
    assert caught.value.key != "register_failed"


def test_the_recovery_window_is_pinned_to_the_target_date_on_both_ends(make_api, no_sleep):
    """`startDate == endDate == target_date`.

    Left as a trailing window, a re-query for a **future** 방문 예정일 returns nothing and a
    successful registration reads as a failure.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok(((PLATE, FUTURE, "RESERVE"),)),
    })

    assert register(api, date=FUTURE_INPUT) == "already_registered"
    sent = crypto.decode_body(stub.bodies_for(HISTORY_URL)[0])

    assert sent["startDate"] == FUTURE
    assert sent["endDate"] == FUTURE
    assert FUTURE != DATE, "the window must be pinned to the target, not to today"


def test_both_sides_are_normalized_before_being_compared(make_api, no_sleep):
    """The plate is sent normalized; `car_number` comes back from the server.

    The server row here carries a **zero-width space** — invisible in the input box, in this
    file, and in any log line. Comparing raw strings would miss the match and report a
    successful registration as uncertain.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok((("12가​3456", DATE, "RESERVE"),)),
    })

    # User input carrying an ordinary space, too, so both sides are dirty.
    assert register(api, plate="12가 3456") == "already_registered"


def test_matching_is_client_side_and_ignores_unrelated_rows(make_api, no_sleep):
    """Server-side `carNumber` filtering is **unverified** — the one verified call sent `""`.

    So it is treated as an optimisation never relied upon: the client filters plate *and*
    date itself, unconditionally. This response deliberately includes another car and the
    right car on the wrong date.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok((
            ("12가4567", DATE, "RESERVE"),        # a different car, active
            (PLATE, FUTURE, "RESERVE"),            # right car, wrong date
        )),
    })

    with pytest.raises(RegisterUncertain):
        register(api)


def test_matching_rows_normalizes_the_plate_it_is_given_too():
    """`matching_rows` strips its `plate` argument, and that is **not** redundant.

    Found by mutation testing: replacing `strip_plate(plate)` with a raw `plate` left the
    whole suite green, because `register()` already calls `normalize_plate` before it gets
    here, so on that path the strip is a no-op. But this method is public, and item 5's
    history filter passes it **raw user input** — where a trailing space would silently match
    nothing and show the user an empty table for a car that is registered.

    So the line stays, and this test is what makes it load-bearing rather than decorative.
    """
    from iparking_lib.iparking.client import HistoryRow

    rows = [HistoryRow(1, PLATE, DATE, "RESERVE", "lot")]

    # A space, an ideographic space, and a zero-width space — none of them visible.
    for dirty in ("12가 3456", "12가　3456", "12가​3456"):
        matching = client_module.IparkingApi.matching_rows(rows, dirty, DATE)
        assert [r.invt_seq for r in matching] == [1], f"failed for {dirty!r}"


def test_matching_rows_returns_every_match_not_the_first(make_api):
    """The predicate can only be existential if the filter hands it all the rows."""
    from iparking_lib.iparking.client import HistoryRow

    rows = [
        HistoryRow(1, PLATE, DATE, "CANCEL", "lot"),
        HistoryRow(2, PLATE, DATE, "RESERVE", "lot"),
        HistoryRow(3, PLATE, FUTURE, "RESERVE", "lot"),
    ]

    matching = client_module.IparkingApi.matching_rows(rows, PLATE, DATE)

    assert [r.invt_seq for r in matching] == [1, 2]
    assert any(r.is_active for r in matching)


def test_a_failed_recovery_query_is_uncertain_not_a_failure(make_api, no_sleep):
    """If we cannot even ask, we certainly cannot report a verdict."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: urllib.error.URLError("still down"),
    })

    with pytest.raises(RegisterUncertain):
        register(api)


# --- the per-car verdicts ----------------------------------------------------


def test_an_explicit_success_returns_ok_without_a_recovery_query(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    assert register(api) == "ok"
    assert stub.count(HISTORY_URL) == 0, "a settled answer needs no recovery"


def test_exist_is_already_registered_and_not_a_failure(make_api):
    """Re-entering a registered plate is the most likely real outcome of a first use, so it
    is a third outcome rather than an error — which is also what lets a Flow treat a benign
    duplicate as benign (item 7)."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("EXIST")})

    assert register(api) == "already_registered"
    assert stub.count(HISTORY_URL) == 0


def test_the_real_success_response_carries_no_per_car_data_and_still_means_ok(make_api):
    """The live probe's actual success response, byte-shaped: `resultData` is **null**.

    Verified 2026-08-04. There is no `invitationInfoList` and no `SUCCESS` array — in any case
    the probe could produce. This test exists because the pre-probe contract read that silence
    as "the response did not say" and routed it to `RegisterUncertain`, which would have made
    **every normal registration** tell the user their car might not be registered and send
    them to the web UI to check. The top-level `result` is the authority on success.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: envelope("0000", "성공", resultData=None),
    })

    assert register(api) == "ok"
    assert stub.count(HISTORY_URL) == 0, "a plain 0000 is settled; no recovery needed"


def test_the_real_duplicate_response_is_already_registered(make_api):
    """The probe's duplicate response: top-level `10003`, `resultData` null.

    `10003` is the only `EXIST` signal the service produces in practice — the per-car `EXIST`
    word never appeared. Mapping it to `already_registered` rather than to a failure is what
    makes a duplicate a benign third outcome.
    """
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: envelope("10003", "방문차량 등록이 실패하였습니다. 다시 시도해주세요.",
                               resultData=None),
    })

    assert register(api) == "already_registered"
    assert stub.count(HISTORY_URL) == 0


def test_the_vendors_retry_inviting_duplicate_message_is_never_surfaced(make_api):
    """The vendor's own `resultMessage` for `10003` calls a duplicate a failure and tells the
    user to **try again** — against an endpoint that writes to a building's access control.

    Ours says it is already registered instead. This asserts the vendor's sentence reaches
    neither the outcome nor the log.
    """
    vendor_text = "방문차량 등록이 실패하였습니다. 다시 시도해주세요."
    api, _stub, logs = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: envelope("10003", vendor_text, resultData=None),
    })

    outcome = register(api)

    assert outcome == "already_registered"
    assert "다시 시도" not in "\n".join(logs)
    assert vendor_text not in "\n".join(logs)


def test_an_explicit_per_car_row_still_wins_if_one_ever_appears(make_api):
    """The shape-tolerant parser stays a **fallback**, not dead code.

    Batch registration is a documented follow-up (`invitationInfoList` is natively an array),
    so a future response may well carry rows. When it does, an explicit verdict for our plate
    must beat the top-level `0000` — otherwise a per-car `FAIL` inside a `0000` envelope would
    be reported as a success.
    """
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("FAIL")})

    assert register(api) == "register_failed"
    assert stub.count(HISTORY_URL) == 0


def test_a_verdict_for_another_car_does_not_override_our_top_level_success(make_api):
    """A row for somebody else's plate says nothing about ours, and `0000` says ours worked."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: per_car("FAIL", plate="34나5678"),
    })

    assert register(api) == "ok"


def test_cancel_then_reregister_reproduces_the_probes_coexisting_rows(make_api, no_sleep):
    """The probe's exact sequence, as the history endpoint reports it afterwards.

    `DELETE` left a `CANCEL` row with its original `invt_seq`, and re-registering the same
    plate and date created a **new** row rather than being refused. So the recovery query sees
    both, and must read the pair as registered.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok((
            {"invt_seq": 3184551, "car_number": PLATE, "invitation_date": DATE,
             "inot_status": "CANCEL"},          # the DELETEd row, still present
            {"invt_seq": 3184552, "car_number": PLATE, "invitation_date": DATE,
             "inot_status": "RESERVE"},         # the re-registration, a new invt_seq
        )),
    })

    assert register(api) == "already_registered"


# --- the uncertain message ---------------------------------------------------


def test_the_uncertain_message_never_invites_a_retry(make_api, no_sleep):
    """The single most important sentence in the app's copy.

    A retry is what turns one uncertain write into two real registrations at a building, so
    the text must not contain the suggestion. It points at the vendor's web UI instead —
    the only surface that can actually answer the question.
    """
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok(),
    })

    with pytest.raises(RegisterUncertain) as caught:
        register(api)
    text = str(caught.value)

    assert "다시 등록하지 마" in text, "it must actively discourage a retry"
    assert "확인할 수 없습니다" in text, "it must say the outcome is unknown"
    assert "웹사이트" in text, "it must point at the surface that can answer"
    assert "다시 시도" not in text, "it must never suggest trying again"


def test_the_uncertain_message_masks_the_plate(make_api, no_sleep):
    """This message reaches logs and diagnostic reports, which get shared."""
    api, _stub, _ = make_api({
        OAUTH_URL: login_ok(),
        REGISTER_URL: urllib.error.URLError("reset"),
        HISTORY_URL: history_ok(),
    })

    with pytest.raises(RegisterUncertain) as caught:
        register(api)

    assert "12가****" in str(caught.value)
    assert PLATE not in str(caught.value)


def test_no_log_line_from_the_register_path_carries_an_unmasked_plate_or_address(
    make_api, no_sleep
):
    """`userName` carries `memb_name` — a home address — into the register body, so the
    never-log rule applies to this path too, including the body-building step."""
    api, _stub, logs = make_api(
        {
            OAUTH_URL: login_ok(memb_name="999동9999호"),
            REGISTER_URL: urllib.error.URLError("reset"),
            HISTORY_URL: history_ok(((PLATE, DATE, "RESERVE"),)),
        },
        password="hunter2-secret",
    )

    register(api)
    blob = "\n".join(logs)

    assert PLATE not in blob
    assert "999동9999호" not in blob
    assert "hunter2-secret" not in blob
    assert "12가****" in blob, "the masked form is what should appear"


# --- input gates -------------------------------------------------------------


def test_a_bad_plate_is_refused_before_any_request(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    with pytest.raises(InvalidPlateError) as caught:
        register(api, plate="12가456")

    assert "예시)" in str(caught.value)
    assert stub.count(REGISTER_URL) == 0


def test_a_past_date_is_refused_before_any_request(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    with pytest.raises(dates.PastDateError) as caught:
        register(api, date=PAST_INPUT)

    assert caught.value.key == "past_date"
    assert stub.count(REGISTER_URL) == 0


def test_the_body_matches_the_recon_shape(make_api):
    """The register body, decoded off the wire and checked field by field."""
    api, stub, _ = make_api(
        {OAUTH_URL: login_ok(memb_name="999동9999호"), REGISTER_URL: per_car("SUCCESS")},
        username="iparking-dev",
    )

    register(api)
    sent = crypto.decode_body(stub.bodies_for(REGISTER_URL)[0])

    assert sent == {
        "parkSeq": PARK_SEQ,
        "storSeq": STOR_SEQ,
        "userId": "iparking-dev",
        "userName": "999동9999호",
        "invitationDate": DATE,
        "invitationInfoList": [{"carNumber": PLATE, "memo": ""}],
    }


def test_a_phone_number_is_split_into_three_fields_only_when_given(make_api):
    """`mobile1/2/3` are omitted entirely rather than sent blank — the bundle drops them."""
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    register(api, mobile="010-1234-5678")
    sent = crypto.decode_body(stub.bodies_for(REGISTER_URL)[0])
    entry = sent["invitationInfoList"][0]

    assert (entry["mobile1"], entry["mobile2"], entry["mobile3"]) == ("010", "1234", "5678")


def test_an_unrecognised_phone_number_is_dropped_rather_than_guessed(make_api):
    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    register(api, mobile="02-123-4567")
    entry = crypto.decode_body(stub.bodies_for(REGISTER_URL)[0])["invitationInfoList"][0]

    assert "mobile1" not in entry


def test_the_date_defaults_to_today_in_kst(make_api):
    """Not the host's today. A Homey Pro's clock need not be set to Asia/Seoul, and the only
    date the vendor accepts is the one at the parking lot."""
    from iparking_lib.iparking import dates

    api, stub, _ = make_api({OAUTH_URL: login_ok(), REGISTER_URL: per_car("SUCCESS")})

    asyncio.run(api.register(car_number=PLATE, park_seq=PARK_SEQ, stor_seq=STOR_SEQ))
    sent = crypto.decode_body(stub.bodies_for(REGISTER_URL)[0])

    assert sent["invitationDate"] == dates.today_api()


def test_register_logs_in_first_when_there_is_no_session(make_api):
    """The authorization gate has to read a **live** flag: the building office can grant the
    permission after a device was paired, and a cached `False` would keep it broken."""
    api, stub, _ = make_api({
        OAUTH_URL: login_ok(), LOTS_URL: lots_ok(), REGISTER_URL: per_car("SUCCESS"),
    })

    assert not api.logged_in
    assert register(api) == "ok"
    assert stub.count(OAUTH_URL) == 1
