"""Tests for `settings/form.js` — the settings page's logic, exercised without a browser.

## Why these run under pytest and not a JS test runner

There is no JS toolchain in this repo and adding one for a single 700-line module would be a
worse trade than this: `settings/form.js` is UMD-wrapped, so `node` can `require()` it with no
package.json, no dependencies and no install step, and `uv run pytest -q` stays the one gate.
Every test here drives `node -` over stdin with an absolute path to the module and asserts on
JSON it prints back. If `node` is absent the module tests skip; the *parity* test that compares
JavaScript against Python does **not** get to skip silently on a machine that has it, which is
what makes it worth writing at all.

What is deliberately **not** covered: anything in `settings/index.html`. That file is markup and
event wiring, which is why all the logic was pushed into `form.js` in the first place. The two
assertions made about it here are structural (it loads the module; every `data-i18n` key it
references exists), not behavioural — the behaviour that matters is on the module side.

## The four things worth a test

* **Plate normalization must agree with `plate.strip_plate` exactly.** Two normalizers that
  disagree means the input echoes one plate while the server registers another, and the
  character sets are genuinely different between the languages: JavaScript's `\\s` misses U+0085
  and U+001C–U+001F, which `str.isspace()` includes. That is a real divergence, not a
  hypothetical, so it is tested against the Python implementation rather than against a
  hand-written expectation.
* **`uncertain` must never be classified as retryable**, including when the request fails in
  transport rather than answering. This is the assertion that stands between one uncertain
  registration and two real ones at a building.
* **`can_register: null` must not classify as `false`.** One is "no account saved yet", the
  other is the building office refusing.
* **The 취소 affordance keys on `is_active`.** A cancelled row stays in the list, so a test that
  only checked "cancelled rows are gone" would pass against the bug.
"""

import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

from iparking_lib.iparking.plate import strip_plate

REPO = Path(__file__).resolve().parents[1]
FORM_JS = REPO / "settings" / "form.js"
INDEX_HTML = REPO / "settings" / "index.html"
LOCALES = REPO / "locales"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; settings/form.js cannot be exercised without it",
)


def run_js(body: str):
    """Run `body` with `F` bound to the module and `out(value)` printing JSON back."""
    script = (
        f"const F = require({json.dumps(str(FORM_JS))});\n"
        "const out = (v) => console.log(JSON.stringify(v));\n"
        f"{body}\n"
    )
    done = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, f"node failed:\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def strings():
    return run_js("out(F.STR);")


# --- plate normalization: parity with plate.py ---------------------------------

#: Every shape requirement 7 has to survive, plus the two `str.isspace()` members that
#: JavaScript's `\s` does not cover. Written as escapes: half of these are invisible.
PLATE_CASES = [
    "12\uac004567",
    "12\uac00 4567",            # ASCII space — the form the vendor's site silently rejects
    "12\uac00\u30004567",       # IDEOGRAPHIC SPACE, what a Korean IME emits in full-width mode
    "12\uac00\u00a04567",       # NBSP
    "12\uac00\t4567",
    "12\uac00\n4567",
    "12\uac00\u200b4567",       # ZWSP — invisible in the input box and in every log line
    "12\uac00\u200c4567",       # ZWNJ
    "12\uac00\u200d4567",       # ZWJ
    "12\uac00\ufeff4567",       # BOM / ZWNBSP
    "12\uac00\u00854567",       # NEL: in str.isspace(), NOT in JavaScript's \\s
    "12\uac00\u001c4567",       # INFORMATION SEPARATOR FOUR — the same asymmetry
    "12\uac00\u001f4567",       # UNIT SEPARATOR
    "  34\ub0985678  ",
    unicodedata.normalize("NFD", "12\uac004567"),   # decomposed jamo, from some IMEs
    "\uc7780000",
    "\uc678\uad50123456",
    "12\uac00456",              # invalid: strip_plate only strips, it never validates
    "",
]


def test_normalize_plate_matches_python_exactly():
    """The JS normalizer and `plate.strip_plate` must agree character for character.

    Parametrizing this per case would be prettier and would cost one `node` process each; the
    table is the point, so it runs as one call and reports the first divergence.
    """
    got = run_js(f"out({json.dumps(PLATE_CASES)}.map(F.normalizePlate));")
    expected = [strip_plate(case) for case in PLATE_CASES]
    assert got == expected


def test_normalize_plate_composes_before_stripping():
    """NFC first, so a decomposed-jamo plate is not rejected for no visible reason."""
    decomposed = unicodedata.normalize("NFD", "12가4567")
    assert decomposed != "12가4567"          # guard: the fixture is actually decomposed
    assert run_js(f"out(F.normalizePlate({json.dumps(decomposed)}));") == "12가4567"


def test_normalize_plate_tolerates_nullish():
    assert run_js("out([F.normalizePlate(null), F.normalizePlate(undefined)]);") == ["", ""]


# --- locale copies -------------------------------------------------------------

#: `STR` key in form.js → key in `locales/*.json` it is a verbatim copy of. The webview cannot
#: read those files and no endpoint serves them, so the strings are duplicated; this maps the
#: duplication so it can be checked instead of merely intended.
COPIED = {
    "cleartextNotice": "cleartext_notice",
    "notPermitted": "not_permitted",
    "alreadyRegistered": "already_registered",
    "registerUncertain": "register_uncertain",
}


@pytest.mark.parametrize("language", ["ko", "en"])
@pytest.mark.parametrize("js_key,locale_key", sorted(COPIED.items()))
def test_str_copies_locale_verbatim(strings, language, js_key, locale_key):
    locale = json.loads((LOCALES / f"{language}.json").read_text(encoding="utf-8"))
    assert strings[language][js_key] == locale[locale_key]


@pytest.mark.parametrize("language", ["ko", "en"])
def test_plate_hint_is_the_example_list_from_bad_plate(strings, language):
    """`plateHint` is the example half of `bad_plate`, so a user who has seen the vendor's own
    UI recognises the same four examples in the same order."""
    locale = json.loads((LOCALES / f"{language}.json").read_text(encoding="utf-8"))
    assert strings[language]["plateHint"] in locale["bad_plate"]


@pytest.mark.parametrize("language", ["ko", "en"])
def test_transport_disclosure_keeps_the_asymmetry(strings, language):
    """The notice must not flatten into "everything is insecure": it has to say the password
    travels over verified TLS *and* that the API host forces cleartext. Dropping either half
    turns a precise disclosure into either alarmism or a lie."""
    notice = strings[language]["cleartextNotice"]
    tls_claim = "검증된 HTTPS" if language == "ko" else "verified HTTPS"
    downgrade_claim = "평문 HTTP" if language == "ko" else "plain HTTP"
    assert tls_claim in notice
    assert downgrade_claim in notice


def test_both_languages_define_the_same_keys(strings):
    assert set(strings["ko"]) == set(strings["en"])


# --- pickLanguage / t / fmt ----------------------------------------------------

def test_pick_language_defaults_to_korean_for_anything_unknown():
    got = run_js(
        'out(["ko", "ko-KR", "en", "en-US", "nl", "", null, undefined, 7].map(F.pickLanguage));'
    )
    assert got == ["ko", "ko", "en", "en", "ko", "ko", "ko", "ko", "ko"]


def test_t_falls_back_through_english_to_the_key_itself():
    got = run_js('out([F.t("ko", "registerBtn"), F.t("nl", "registerBtn"), F.t("ko", "nope")]);')
    assert got == ["등록", "등록", "nope"]


def test_fmt_leaves_unknown_placeholders_alone():
    """A missing placeholder must render as itself rather than as `undefined` — a UI string
    reading "visit date undefined" is worse than one reading "visit date {date}"."""
    assert run_js('out(F.fmt("{a}/{b}", {a: 1}));') == "1/{b}"


def test_status_label_renders_unknown_codes_verbatim():
    """A vendor status this app has never seen must show as itself, not as an empty cell."""
    got = run_js(
        'out(["RESERVE","IN","OUT","CANCEL","SOMETHING_NEW",""].map(s => F.statusLabel("ko", s)));'
    )
    assert got == ["예약", "입차", "출차", "취소됨", "SOMETHING_NEW", ""]


# --- registerPermission: null is not false ------------------------------------

def test_register_permission_separates_unknown_from_denied():
    got = run_js(
        "out([F.registerPermission(null, null), F.registerPermission(undefined, undefined),"
        " F.registerPermission(false, null), F.registerPermission(true, null)]);"
    )
    assert got == ["unknown", "unknown", "denied", "allowed"]


def test_register_permission_lets_the_lot_override_the_account():
    """An account can hold several stores with the permission set differently on each, so the
    selected lot's own flag is the finer answer and wins whenever it is a boolean."""
    got = run_js(
        "out([F.registerPermission(true, false), F.registerPermission(false, true),"
        " F.registerPermission(null, true), F.registerPermission(null, false)]);"
    )
    assert got == ["denied", "allowed", "allowed", "denied"]


# --- classifyRegister: uncertain is its own state, and never retryable --------

def test_uncertain_is_classified_before_ok_and_is_never_retryable():
    """`RegisterUncertain` arrives with `ok: false`. Read in the wrong order it looks like a
    plain failure and the page offers a retry — which is what turns one uncertain registration
    into two real ones at a building."""
    response = {
        "ok": False, "uncertain": True, "key": "register_uncertain",
        "error": "등록 결과를 확인할 수 없습니다.",
    }
    got = run_js(f"out(F.classifyRegister({json.dumps(response)}));")
    assert got["state"] == "uncertain"
    assert got["retryable"] is False


def test_already_registered_is_its_own_state_not_a_failure():
    """`api.py` returns it `ok: true`. Re-entering a plate that is already registered is the
    most likely real result of a first use, and reporting it as an error teaches the user the
    app is broken on their very first try."""
    response = {"ok": True, "outcome": "already_registered", "car_number": "12가3456"}
    got = run_js(f"out(F.classifyRegister({json.dumps(response)}));")
    assert got["state"] == "already"
    assert got["retryable"] is False


def test_success_and_plain_failure_classify_as_themselves():
    ok = {"ok": True, "outcome": "ok"}
    failed = {"ok": False, "outcome": "register_failed", "message": "차량 등록에 실패했습니다."}
    got = run_js(
        f"out([F.classifyRegister({json.dumps(ok)}), F.classifyRegister({json.dumps(failed)})]);"
    )
    assert [v["state"] for v in got] == ["ok", "error"]
    assert [v["retryable"] for v in got] == [False, True]


def test_a_missing_or_junk_response_is_an_error_not_a_success():
    got = run_js("out([F.classifyRegister(null), F.classifyRegister(undefined),"
                 ' F.classifyRegister("boom")].map(v => v.state));')
    assert got == ["error", "error", "error"]


# --- registerToast: only a real registration, and only "오늘" when it is -------
#
# The toast is the page's own element (Homey's SDK has no transient-message call at all), so the
# only thing worth testing off-browser is the decision: which outcomes get one, and what it says.
# Both halves can go wrong quietly. A toast over `already` or `uncertain` would announce a
# registration this click did not make, and a toast saying 오늘 for a future visit date would be
# the same silent wrong-day error the date echo exists to catch.

STATUS_KST = {"today_kst": "2026-08-05", "max_date": "2026-09-04", "max_days_ahead": 30}


def _toast(verdict, status=None, lang="ko"):
    return run_js(
        f"out(F.registerToast({json.dumps(verdict)}, "
        f"{json.dumps(status if status is not None else STATUS_KST)}, {json.dumps(lang)}));"
    )


def test_only_a_real_registration_gets_a_toast():
    """`already` means the vehicle was registered *before* this click and `uncertain` means
    nobody knows — flattening either into a success toast would be a claim about a building's
    access control that this app cannot support."""
    for state in ("already", "uncertain", "error"):
        verdict = {
            "state": state,
            "response": {"ok": True, "car_number": "12가3456", "api_date": "20260805"},
        }
        assert _toast(verdict) == ""


def test_a_registration_for_today_says_today():
    """The wording the maintainer asked for, verbatim."""
    verdict = {"state": "ok", "response": {"car_number": "12가3456", "api_date": "20260805",
                                           "date": "2026년 8월 5일 (수)"}}

    assert _toast(verdict) == "12가3456 차량이 오늘 방문 등록되었습니다."


def test_a_registration_for_another_day_names_that_day_instead_of_saying_today():
    """`api_date` is compared against `/status`'s `today_kst` — both server-side KST values, so
    `new Date()` stays uninvolved. A tile press is always today, which is where the 오늘 wording
    comes from; this page can register any date in the window, and calling next Tuesday "오늘"
    would send a visitor to a gate on the wrong day believing the app agreed."""
    verdict = {"state": "ok", "response": {"car_number": "12가3456", "api_date": "20260812",
                                           "date": "2026년 8월 12일 (수)"}}

    assert _toast(verdict) == "12가3456 차량이 2026년 8월 12일 (수) 방문 등록되었습니다."


def test_an_unknown_today_names_the_date_rather_than_guessing_it_is_today():
    """`/status` not having answered is not evidence that the visit is today. The fallback names
    the day the handler resolved, which is always true, instead of the one claim that might not
    be."""
    verdict = {"state": "ok", "response": {"car_number": "12가3456", "api_date": "20260805",
                                           "date": "2026년 8월 5일 (수)"}}

    assert _toast(verdict, status={}) == "12가3456 차량이 2026년 8월 5일 (수) 방문 등록되었습니다."


def test_a_toast_with_no_plate_to_name_is_not_shown_at_all():
    """An empty response field would render `" 차량이 오늘 방문 등록되었습니다."`, which reads
    like a registration with no vehicle."""
    assert _toast({"state": "ok", "response": {}}) == ""
    assert _toast({"state": "ok"}) == ""


def test_the_toast_is_translated_rather_than_hardcoded_korean():
    verdict = {"state": "ok", "response": {"car_number": "12가3456", "api_date": "20260805"}}

    assert _toast(verdict, lang="en") == "12가3456 is registered to visit today."


# --- messageOf: prefer the viewer's language ----------------------------------

def test_message_of_prefers_message_over_error():
    """`message` is the locale key rendered in the *viewer's* language; `error` is the specific
    Korean sentence the exception raised. Preferring `error` hands Korean to an English user."""
    res = {"ok": False, "key": "not_permitted",
           "error": "권한이 없습니다.", "message": "Not permitted."}
    assert run_js(f'out(F.messageOf({json.dumps(res)}, "en"));') == "Not permitted."


def test_message_of_falls_back_to_error_then_to_a_generic_sentence():
    got = run_js(
        'out([F.messageOf({ok: false, error: "주차장을 선택하세요."}, "ko"),'
        ' F.messageOf({ok: false}, "en")]);'
    )
    assert got[0] == "주차장을 선택하세요."
    assert got[1] == "An unknown error occurred."


def test_describe_walks_the_shapes_homey_actually_rejects_with():
    """`String(err)` printed `[object Object]` for these in a sibling app — a real defect, since
    `homey.js` rejects with the value the Python side sent, verbatim."""
    got = run_js(
        'out([F.describe({message: "m"}, "en"), F.describe({error: {reason: "deep"}}, "en"),'
        ' F.describe({odd: 1}, "en"), F.describe(null, "en")]);'
    )
    assert got == ["m", "deep", '{"odd":1}', "An unknown error occurred."]


# --- rowIsCancellable: keyed on is_active, not on presence -------------------

def test_cancel_is_offered_on_is_active_and_withheld_on_a_cancelled_row():
    """A cancelled registration keeps its row and its `invt_seq` — `DELETE` flips `inot_status`
    to `CANCEL` rather than removing anything (verified live). So presence carries no
    information, and a button rendered on presence would offer to cancel a cancelled row."""
    rows = [
        {"invt_seq": 1, "status": "RESERVE", "is_active": True},
        {"invt_seq": 2, "status": "CANCEL", "is_active": False},
        {"invt_seq": 3, "status": "OUT", "is_active": True},
        {"invt_seq": 0, "status": "RESERVE", "is_active": True},   # no handle to cancel with
    ]
    got = run_js(f"out({json.dumps(rows)}.map(F.rowIsCancellable));")
    assert got == [True, False, True, False]


# --- paginate: display-only ---------------------------------------------------

def test_paginate_slices_a_list_that_is_already_complete():
    """`page_size: 100` was verified to return all 43 rows of a three-month window in one
    response, so this pages over memory. 43 is that real row count, kept as the fixture size."""
    got = run_js("const rows = Array.from({length: 43}, (_, i) => ({i}));"
                 "out([F.paginate(rows, 1, 20), F.paginate(rows, 3, 20)]);")
    assert got[0]["pages"] == 3 and got[0]["total"] == 43 and len(got[0]["rows"]) == 20
    assert got[1]["page"] == 3 and len(got[1]["rows"]) == 3


def test_paginate_clamps_a_stale_page_number():
    """After a 취소 the list can shrink. A page number left over from the longer list must not
    render an empty table — that reads as "my registrations are gone"."""
    got = run_js("out(F.paginate([{a: 1}], 9, 20));")
    assert got["page"] == 1 and got["pages"] == 1 and len(got["rows"]) == 1


def test_paginate_reports_one_page_for_an_empty_list():
    got = run_js("out([F.paginate([], 1, 20), F.paginate(null, 1, 20)]);")
    assert [v["pages"] for v in got] == [1, 1]
    assert [v["total"] for v in got] == [0, 0]


# --- dateBounds: KST is the sole authority ------------------------------------

def test_date_bounds_come_from_status_only():
    status = {"today_kst": "2026-08-05", "max_date": "2026-10-24", "max_days_ahead": 80}
    got = run_js(f"out(F.dateBounds({json.dumps(status)}));")
    assert got == {"min": "2026-08-05", "value": "2026-08-05", "max": "2026-10-24", "days": 80}


def test_date_bounds_leave_the_input_unbounded_rather_than_guess_from_the_browser():
    """The browser's timezone is never consulted. An unbounded input is a problem the user meets
    at submit; a bound derived from their own timezone is a problem they meet at a closed gate."""
    got = run_js("out([F.dateBounds({}), F.dateBounds(null)]);")
    assert got == [
        {"min": "", "value": "", "max": "", "days": None},
        {"min": "", "value": "", "max": "", "days": None},
    ]


def code_only(source: str) -> str:
    """`source` with its comments removed.

    Needed because both files *discuss* `new Date()` at length — the prohibition is the point of
    those comments, and a grep that cannot tell a rule from its violation would fail on the
    documentation and then get deleted for crying wolf.
    """
    stripped = re.sub(r"/\*.*?\*/", " ", source, flags=re.S)
    stripped = re.sub(r"<!--.*?-->", " ", stripped, flags=re.S)
    return "\n".join(
        line for line in stripped.splitlines()
        if not line.lstrip().startswith(("//", "*"))
    )


def test_no_surface_constructs_a_date_from_the_browser_clock():
    """Grep-level, and on purpose: `new Date()` anywhere in the *code* of these two surfaces
    would reintroduce the browser's timezone as a second date authority, next to the KST one the
    server hands over in `/status`. The two would agree for most of the day, which is exactly
    what makes the disagreement worth a test."""
    for path in (FORM_JS, INDEX_HTML):
        code = code_only(path.read_text(encoding="utf-8"))
        assert "new Date(" not in code, f"{path.name} constructs a Date"
        assert "Date.now" not in code, f"{path.name} reads the browser clock"
        assert "getTimezoneOffset" not in code, f"{path.name} reads the browser timezone"


# --- request shaping ----------------------------------------------------------

def test_history_path_carries_the_lot_and_encodes_its_filters():
    lot = {"lot_id": "1160009001", "park_seq": 9001, "stor_seq": 100001}
    got = run_js(
        f"out(F.historyPath({json.dumps(lot)}, "
        '{start_date: "2026-08-01", car_number: "12가3456"}));'
    )
    assert got.startswith("/history?park_seq=9001&stor_seq=100001")
    assert "start_date=2026-08-01" in got
    assert "car_number=12%EA%B0%803456" in got


def test_history_path_omits_absent_filters():
    lot = {"park_seq": 9001, "stor_seq": 100001}
    got = run_js(f"out(F.historyPath({json.dumps(lot)}, {{}}));")
    assert got == "/history?park_seq=9001&stor_seq=100001"


def test_register_timeout_outlasts_the_python_side_budgets():
    """`client.register()` runs a 20 s write budget and then a **fresh** 25 s recovery re-query.
    A page timeout below 45 s would abandon the request while the recovery that determines the
    real outcome is still running — manufacturing an `uncertain` out of a knowable answer."""
    got = run_js("out(F.TIMEOUTS);")
    assert got["register"] >= 45000
    assert got["register"] > got["default"]


# --- controller ---------------------------------------------------------------

CONTROLLER_HARNESS = """
function harness(script) {
  const calls = [];
  const api = (method, path, body, timeout) => {
    calls.push({ method, path, body, timeout });
    const reply = script[method + " " + path.split("?")[0]];
    if (typeof reply === "function") return reply();
    return Promise.resolve(reply);
  };
  const c = F.createController({ api, lang: "ko" });
  return { c, calls };
}
"""


def test_a_successful_register_immediately_refetches_history():
    """The requirement, and the reason it is a requirement: the table must not show stale rows
    right after the action that changed them. There is nothing else to refresh — the device's
    only capability is `park_name`, which a registration does not touch."""
    got = run_js(CONTROLLER_HARNESS + """
const { c, calls } = harness({
  "GET /lots": { ok: true, can_register: true,
                 lots: [{ lot_id: "1160009001", park_seq: 9001, stor_seq: 100001,
                          park_name: "예시동 샘플아파트[출입통제A]", can_register: true }] },
  "POST /register": { ok: true, outcome: "ok", car_number: "12가4567",
                      api_date: "20260805", date: "2026-08-05 (수)", ambiguous: false },
  "GET /history": { ok: true, rows: [{
    invt_seq: 5001, car_number: "12가4567", invitation_date: "20260805",
    status: "RESERVE", is_active: true, park_name: "예시동 샘플아파트[출입통제A]",
  }] },
});
c.loadLots()
  .then(() => c.register({ carNumber: "12가 4567", visitDate: "2026-08-05" }))
  .then((verdict) => out({
    state: verdict.state,
    retryable: verdict.retryable,
    rows: verdict.rows.length,
    sent: calls.filter((k) => k.path === "/register").map((k) => k.body.car_number),
    order: calls.map((k) => k.method + " " + k.path.split("?")[0]),
  }));
""")
    assert got["state"] == "ok"
    assert got["rows"] == 1
    # The plate is normalized before it is sent, not only when it is echoed back.
    assert got["sent"] == ["12가4567"]
    assert got["order"] == ["GET /lots", "POST /register", "GET /history"]


def test_a_register_that_fails_in_transport_is_uncertain_and_not_retryable():
    """If the POST never answers, the request may well have reached the vendor and registered
    the vehicle — the same orphan the Python side's recovery re-query exists to catch, one layer
    further out. Calling that a failure invites the retry that makes it two registrations."""
    got = run_js(CONTROLLER_HARNESS + """
const { c } = harness({
  "GET /lots": { ok: true, lots: [{ lot_id: "1160009001", park_seq: 9001, stor_seq: 100001,
                                    can_register: true }] },
  "POST /register": () => Promise.reject(new Error("Homey.api did not respond")),
  "GET /history": { ok: true, rows: [] },
});
c.loadLots()
  .then(() => c.register({ carNumber: "12가4567", visitDate: "2026-08-05" }))
  .then((v) => out({ state: v.state, retryable: v.retryable, detail: v.detail,
                     message: v.message }));
""")
    assert got["state"] == "uncertain"
    assert got["retryable"] is False
    assert "다시 시도하지 마세요" in got["message"]
    assert "did not respond" in got["detail"]


def test_an_uncertain_register_still_refetches_history_as_evidence():
    """A read costs nothing and may well answer the question the message tells the user to open
    the vendor's website for. The message stands regardless — the refresh is evidence, not a
    verdict, and it is emphatically not a retry."""
    got = run_js(CONTROLLER_HARNESS + """
const { c, calls } = harness({
  "GET /lots": { ok: true, lots: [{ lot_id: "1", park_seq: 9001, stor_seq: 100001 }] },
  "POST /register": { ok: false, uncertain: true, key: "register_uncertain",
                      message: "결과를 확인할 수 없습니다." },
  "GET /history": { ok: true, rows: [] },
});
c.loadLots()
  .then(() => c.register({ carNumber: "임0000", visitDate: "2026-08-05" }))
  .then((v) => out({ state: v.state,
                     refetched: calls.some((k) => k.path.startsWith("/history")) }));
""")
    assert got == {"state": "uncertain", "refetched": True}


def test_a_failed_register_does_not_refetch_history():
    """A refused write changed nothing, so re-reading the table would be traffic for no reason —
    and this app's whole politeness budget is arithmetic rather than assertion."""
    got = run_js(CONTROLLER_HARNESS + """
const { c, calls } = harness({
  "GET /lots": { ok: true, lots: [{ lot_id: "1", park_seq: 9001, stor_seq: 100001 }] },
  "POST /register": { ok: false, outcome: "register_failed", message: "실패했습니다." },
  "GET /history": { ok: true, rows: [] },
});
c.loadLots()
  .then(() => c.register({ carNumber: "12가4567", visitDate: "2026-08-05" }))
  .then((v) => out({ state: v.state,
                     refetched: calls.some((k) => k.path.startsWith("/history")) }));
""")
    assert got == {"state": "error", "refetched": False}


def test_cancel_refetches_history_because_the_row_does_not_disappear():
    got = run_js(CONTROLLER_HARNESS + """
const { c, calls } = harness({
  "GET /lots": { ok: true, lots: [{ lot_id: "1", park_seq: 9001, stor_seq: 100001 }] },
  "POST /cancel": { ok: true, invt_seq: 5001 },
  "GET /history": { ok: true, rows: [{ invt_seq: 5001, status: "CANCEL", is_active: false }] },
});
c.loadLots()
  .then(() => c.cancel(5001))
  .then(() => out({ order: calls.map((k) => k.method + " " + k.path.split("?")[0]),
                    cancellable: c.pageRows().rows.map(F.rowIsCancellable) }));
""")
    assert got["order"] == ["GET /lots", "POST /cancel", "GET /history"]
    # The row is still there after a successful 취소 — and is no longer cancellable.
    assert got["cancellable"] == [False]


def test_register_refuses_without_a_selected_lot_and_sends_nothing():
    got = run_js(CONTROLLER_HARNESS + """
const { c, calls } = harness({});
c.register({ carNumber: "12가4567", visitDate: "2026-08-05" })
  .then((v) => out({ state: v.state, message: v.message, calls: calls.length }));
""")
    assert got["state"] == "error"
    assert got["calls"] == 0
    assert got["message"] == "주차장을 선택하세요."


def test_permission_is_read_from_status_and_never_cached_across_loads():
    """§3.8: re-read on every page load. A user the building office has just granted the
    permission must not have to re-pair anything to see the card come alive."""
    got = run_js(CONTROLLER_HARNESS + """
let canRegister = false;
const { c } = harness({
  "GET /status": () => Promise.resolve({ ok: true, configured: true, can_register: canRegister }),
  "GET /lots": { ok: true, lots: [] },
});
c.loadStatus()
  .then(() => { const before = c.permission(); canRegister = true;
                return c.loadStatus().then(() => out([before, c.permission()])); });
""")
    assert got == ["denied", "allowed"]


# --- index.html: structural only ----------------------------------------------

def test_index_html_loads_the_module_before_its_own_wiring():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.index('src="form.js"') < html.index("IparkingForm")


def test_every_data_i18n_key_in_the_page_exists_in_the_string_table(strings):
    """A typo'd `data-i18n` renders the key itself into the UI — visible, but only to whoever
    happens to look at that card in that language."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n="([^"]+)"', html))
    assert used, "no data-i18n attributes found — the i18n wiring is gone"
    assert used <= set(strings["ko"]), sorted(used - set(strings["ko"]))


def test_the_page_never_reimplements_a_verdict_locally(strings):
    """The three vendor-data verdicts must be asked of the module, not re-derived in the page —
    that is what makes the v0.1.1 widget a mount rather than a rewrite."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    for call in ("F.rowIsCancellable", "C.permission()", "C.dateBounds()", "C.toast(verdict)"):
        assert call in html, f"{call} is not what the page uses"
    # `is_active` may be read for styling, but never to decide whether 취소 is offered.
    assert "row.is_active ?" not in html


def test_the_page_owns_the_toast_element_and_its_fade():
    """The toast is the page's own element, because Homey's SDK exposes no transient-message call
    of any kind — `homey.js` has `alert`/`confirm`, which are modal and have to be dismissed. So
    what is asserted is that the element and its two CSS states exist: a missing `.show` rule
    leaves a permanently invisible toast, which no test of the *text* would notice."""
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="toast"' in html
    assert 'aria-live="polite"' in html
    assert ".toast {" in html and ".toast.show {" in html
    assert "transition:opacity" in html
    # Never intercepts a click on the card underneath it.
    assert "pointer-events:none" in html
