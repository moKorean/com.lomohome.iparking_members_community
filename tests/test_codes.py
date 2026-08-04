"""Result codes and per-car outcomes — acceptance criterion 8.

The theme of this file is that `already_registered` is a **third outcome**, neither success
nor failure. Re-entering a plate that is already registered is the most likely real result
of the maintainer's first use of the app; calling it an error teaches them the app is broken
on day one, and (item 7) makes a Flow treat a benign duplicate as a failed action.

The second theme is that `parse_per_car` must answer "the response did not say" by
*omission*, because the write endpoint has never been exercised and the location of
`SUCCESS`/`FAIL`/`EXIST` in the body is genuinely unknown until item 3's probe runs. An
omitted plate routes to `RegisterUncertain` in §3.5; a guessed one routes to a wrong claim
about a real building's access control.

`tests/conftest.py` puts the repo root on `sys.path`; nothing here does its own.
"""

import json
from pathlib import Path

import pytest

from iparking_lib.iparking import codes

REPO_ROOT = Path(__file__).resolve().parents[1]

# `docs/RECON.md`'s table, transcribed. Written out here rather than iterated from
# `codes.RESULT_KEYS` on purpose: a test that reads the mapping it is checking cannot
# notice a dropped row.
RECON_CODES = [
    "0000",
    "1001",
    "1002",
    "1009",
    "2001",
    "2002",
    "2031",
    "2041",
    "2042",
    "10003",
    "12100",
    "12105",
    "13001",
    "13002",
]


def test_every_recon_code_has_an_i18n_key():
    for code in RECON_CODES:
        key = codes.result_key(code)
        assert key != codes.UNKNOWN_KEY, f"{code} is unmapped"
        assert key, code
    assert set(codes.RESULT_KEYS) == set(RECON_CODES)


def test_unknown_code_falls_back_rather_than_raising():
    """A code the vendor adds later must produce a message, not a traceback."""
    assert codes.result_key("9999") == codes.UNKNOWN_KEY
    assert codes.result_key(None) == codes.UNKNOWN_KEY
    assert codes.result_key("") == codes.UNKNOWN_KEY


@pytest.mark.parametrize(
    "code,expected",
    [
        ("2002", "error.login_error"),
        ("2042", "error.password_error"),
        ("12105", "error.not_allowed"),
        ("13001", "error.already_deleted"),
        ("13002", "error.cannot_delete"),
    ],
)
def test_representative_keys(code, expected):
    assert codes.result_key(code) == expected


def test_success_is_exactly_0000():
    assert codes.is_success("0000")
    assert codes.is_success(" 0000 ")           # whitespace-tolerant
    assert not codes.is_success("0")             # never zero-padded into a success
    assert not codes.is_success(0)
    assert not codes.is_success("")
    assert not codes.is_success(None)
    assert not codes.is_success("1001")


@pytest.mark.parametrize("code", ["2031", "2041", "1009", 2031])
def test_auth_expired_codes(code):
    assert codes.is_auth_expired(code)


@pytest.mark.parametrize("code", ["0000", "1001", "2002", "2042", "10003", "", None])
def test_codes_that_a_relogin_would_not_fix(code):
    """`2042` (passwordError) is the trap: it is about credentials but re-login cannot help."""
    assert not codes.is_auth_expired(code)


# --- criterion 8: EXIST and 10003 ------------------------------------------------


def test_exist_maps_to_already_registered():
    assert codes.per_car_outcome("EXIST") == codes.OUTCOME_ALREADY_REGISTERED
    assert codes.per_car_outcome("exist") == codes.OUTCOME_ALREADY_REGISTERED
    assert codes.per_car_outcome(" Exist ") == codes.OUTCOME_ALREADY_REGISTERED


def test_top_level_10003_maps_to_already_registered():
    assert codes.RESULT_KEYS["10003"] == codes.OUTCOME_ALREADY_REGISTERED
    assert codes.result_key("10003") == codes.OUTCOME_ALREADY_REGISTERED
    assert codes.per_car_outcome("10003") == codes.OUTCOME_ALREADY_REGISTERED


def test_already_registered_is_neither_success_nor_generic_failure():
    """The distinction criterion 8 is actually about."""
    assert codes.OUTCOME_ALREADY_REGISTERED != codes.OUTCOME_OK
    assert codes.OUTCOME_ALREADY_REGISTERED != codes.OUTCOME_FAILED
    assert not codes.is_success("10003")
    assert codes.result_key("10003") != codes.result_key("1001")
    # And it does not hide under the error namespace, because it is not an error.
    assert not codes.result_key("10003").startswith("error.")


def _locale(language):
    """The committed locale table for `language`.

    Asserted rather than skipped-around: `locales/{ko,en}.json` is a shipped artefact, and
    a suite that quietly passes when it is missing is exactly the kind of evidence that is
    not evidence.
    """
    path = REPO_ROOT / "locales" / f"{language}.json"
    assert path.exists(), f"{path} is required (item 8)"
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(table, key):
    node = table
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def test_already_registered_locale_text():
    """Criterion 8 also requires the rendered text to contain `이미 등록된 차량`.

    The text lives in `locales/{ko,en}.json` (item 8); this reads what is committed there,
    so the code→key mapping and the key→text mapping are checked end to end.
    """
    table = _locale("ko")
    text = _resolve(table, codes.result_key("10003"))
    assert text, f"locales/ko.json needs a {codes.OUTCOME_ALREADY_REGISTERED!r} entry"
    assert "이미 등록된 차량" in text


@pytest.mark.parametrize("language", ["ko", "en"])
def test_every_mapped_key_has_text_in_both_locales(language):
    """No code may render as a bare key.

    This module decides *which* key a code gets; the locale files decide whether that key
    says anything. Split across two work items, the two halves can drift, and the symptom
    is a user staring at the literal string `error.db_error`.
    """
    table = _locale(language)
    missing = [
        key
        for key in sorted({*codes.RESULT_KEYS.values(), codes.UNKNOWN_KEY})
        if not _resolve(table, key)
    ]
    assert not missing, f"locales/{language}.json has no text for {missing}"


@pytest.mark.parametrize(
    "status,expected",
    [
        ("SUCCESS", codes.OUTCOME_OK),
        ("success", codes.OUTCOME_OK),
        ("0000", codes.OUTCOME_OK),
        ("EXIST", codes.OUTCOME_ALREADY_REGISTERED),
        ("10003", codes.OUTCOME_ALREADY_REGISTERED),
        ("FAIL", codes.OUTCOME_FAILED),
        ("fail", codes.OUTCOME_FAILED),
        ("1001", codes.OUTCOME_FAILED),
        ("12105", codes.OUTCOME_FAILED),
    ],
)
def test_per_car_outcome_known_values(status, expected):
    assert codes.per_car_outcome(status) == expected


@pytest.mark.parametrize("status", [None, "", "   ", "WAT", "PENDING", "9999", {}, []])
def test_per_car_outcome_is_none_when_it_cannot_tell(status):
    """`None`, never a guess. §3.5 turns this into `RegisterUncertain`."""
    assert codes.per_car_outcome(status) is None


# --- parse_per_car shape tolerance -----------------------------------------------

PLATE = "12가4567"


@pytest.mark.parametrize(
    "payload,shape",
    [
        ({"result": "0000", "invitationInfoList": [{"carNumber": PLATE, "result": "SUCCESS"}]},
         "invitationInfoList at the top level"),
        ({"result": "0000", "resultData": [{"carNumber": PLATE, "status": "SUCCESS"}]},
         "resultData is itself the list"),
        ({"result": "0000",
          "resultData": {"invitationInfoList": [{"carNumber": PLATE, "resultCode": "0000"}]}},
         "resultData.invitationInfoList"),
        ({"result": "0000", "list": [{"car_number": PLATE, "result": "SUCCESS"}]},
         "list, snake_case car key"),
        ({"result": "0000", "resultData": {"list": [{"car_number": PLATE, "status": "0000"}]}},
         "resultData.list"),
    ],
)
def test_parse_per_car_finds_rows_in_every_plausible_shape(payload, shape):
    assert codes.parse_per_car(payload) == {PLATE: codes.OUTCOME_OK}, shape


def test_parse_per_car_reads_each_status_spelling():
    for key in ("result", "status", "resultCode"):
        payload = {"invitationInfoList": [{"carNumber": PLATE, key: "EXIST"}]}
        assert codes.parse_per_car(payload) == {PLATE: codes.OUTCOME_ALREADY_REGISTERED}, key


def test_parse_per_car_normalizes_the_returned_plate():
    """The server's spelling is normalized before it becomes a key (§3.5, both sides)."""
    payload = {
        "invitationInfoList": [
            {"carNumber": "12가" + chr(0x00A0) + "4567", "result": "SUCCESS"},
            {"carNumber": "34나" + chr(0x200B) + "1234", "result": "EXIST"},
        ]
    }
    assert codes.parse_per_car(payload) == {
        PLATE: codes.OUTCOME_OK,
        "34나1234": codes.OUTCOME_ALREADY_REGISTERED,
    }


def test_parse_per_car_handles_a_batch():
    payload = {
        "result": "0000",
        "resultData": {
            "invitationInfoList": [
                {"carNumber": PLATE, "result": "SUCCESS"},
                {"carNumber": "임0000", "result": "EXIST"},
                {"carNumber": "56다7890", "result": "FAIL"},
            ]
        },
    }
    assert codes.parse_per_car(payload) == {
        PLATE: codes.OUTCOME_OK,
        "임0000": codes.OUTCOME_ALREADY_REGISTERED,
        "56다7890": codes.OUTCOME_FAILED,
    }


def test_top_level_10003_with_no_rows_is_not_this_functions_call():
    """The verified real shape of a duplicate: a bare top-level code and nothing else.

    `parse_per_car` no longer synthesizes a verdict from a top-level code — that used to be
    driven by a `requested` parameter, removed because passing it made the caller's own
    `10003` branch unreachable (mutation testing: `client.py`'s `_attempt_register` owns the
    whole-request verdict now; see its docstring). Omission here is the correct, honest
    answer, and the caller is the one that reads `result` itself.
    """
    payload = {"result": "10003", "resultMessage": "기등록 차량"}
    assert codes.parse_per_car(payload) == {}


def test_an_explicit_row_wins_over_the_top_level_code():
    payload = {
        "result": "10003",
        "invitationInfoList": [{"carNumber": PLATE, "result": "SUCCESS"}],
    }
    assert codes.parse_per_car(payload) == {PLATE: codes.OUTCOME_OK}


@pytest.mark.parametrize(
    "payload,why",
    [
        ({"result": "0000"}, "success with no per-car rows at all"),
        ({"result": "1001"}, "generic failure, no rows: not our call to guess"),
        ({"result": "0000", "resultData": {"total": []}}, "a dict where rows were hoped for"),
        ({"result": "0000", "invitationInfoList": []}, "an empty list"),
        ({"result": "0000", "invitationInfoList": [{"carNumber": PLATE}]}, "row with no status"),
        ({"result": "0000", "invitationInfoList": [{"carNumber": PLATE, "result": "WAT"}]},
         "row with an unrecognised status"),
        ({"result": "0000", "invitationInfoList": [{"result": "SUCCESS"}]}, "row with no plate"),
        ({"result": "0000", "invitationInfoList": ["12가4567"]}, "list of strings"),
        (None, "no payload"),
        ([], "a list where an object was expected"),
        ("0000", "a bare string"),
    ],
)
def test_parse_per_car_omits_what_it_cannot_read(payload, why):
    """Omission is the contract. Nothing here may produce a success or a failure claim."""
    assert codes.parse_per_car(payload) == {}, why
