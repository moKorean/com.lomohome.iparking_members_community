"""Byte-exact reproduction of the vendor's request-body envelope.

The envelope is `base64(base64(AES-256-CBC(PKCS#7(JSON.stringify(payload)))))`. Three
things can independently be wrong in it, and each fails in a way that looks like the
others from the outside — the server answers a malformed body with a generic rejection,
which reads exactly like a bad password:

  * the **cipher/padding** — covered by `test_aes.py` against openssl and NIST;
  * the **number of base64 layers** — `AESEncode` already base64s, and the bundle then
    calls `window.btoa` on the result, so there are two;
  * the **JSON serialisation** — `separators=(",", ":")` and `ensure_ascii=False`, both
    required to match `JSON.stringify` byte for byte.

This module pins the composition of all three against `tests/fixtures/envelope_kat.json`,
whose ciphertext came from `openssl` (see `scripts/gen_aes_fixtures.sh`) and which is
committed. The same values are recorded in `docs/RECON.md` Appendix A, and the last test
here asserts the document and the fixture still agree — otherwise the appendix would decay
into decoration while claiming to be the contract.

`decode_body`/`decrypt_envelope` appear only *after* the primary assertion in each test.
Our decoder inverting our encoder is not evidence of anything.
"""

import base64
import json
from pathlib import Path

import pytest

from iparking_lib.iparking import crypto

FIXTURE = Path(__file__).parent / "fixtures" / "envelope_kat.json"
KAT = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = KAT["cases"]
BY_NAME = {case["name"]: case for case in CASES}

RECON = Path(__file__).parents[1] / "docs" / "RECON.md"


def _payload(case: dict):
    """The recorded body text, parsed back into the payload that produced it.

    Bodies are stored as hex so neither the shell that generated them nor this file has
    to quote Hangul or JSON punctuation. Round-tripping through `json.loads` here is safe
    for the thing being tested: whatever ordering or spacing `crypto.json_bytes` then
    emits has to match the *recorded text*, which is asserted separately below.
    """
    return json.loads(bytes.fromhex(case["body_json_hex"]).decode("utf-8"))


# --- the serialisation half ---------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_json_bytes_matches_the_recorded_stringify_output(case):
    """Isolates JSON from crypto, so a failure names which half broke.

    Without this, a serialisation bug and a cipher bug both present as "the envelope is
    wrong" and the next hour goes into the wrong file.
    """
    expected = bytes.fromhex(case["body_json_hex"])

    assert crypto.json_bytes(_payload(case)) == expected


def test_json_bytes_uses_compact_separators():
    """`json.dumps` defaults to `", "` and `": "`. Those spaces change the length, which
    changes which PKCS#7 bucket the body lands in — so this is not cosmetic."""
    produced = crypto.json_bytes({"a": 1, "b": [2, 3]})

    assert produced == b'{"a":1,"b":[2,3]}'
    assert b", " not in produced
    assert b": " not in produced


def test_json_bytes_emits_hangul_as_utf8_not_backslash_escapes():
    """`ensure_ascii=False` is required, not preferred.

    `userName` carries `memb_name`, which is Hangul. With escaping on, `12가1235` becomes
    `12\\uac004567` — different bytes, and 8 bytes longer per syllable, so a body that was
    safely mid-block moves onto a boundary. That is how a padding bug and a serialisation
    bug conspire into a failure that only shows up for *some* plates.
    """
    produced = crypto.json_bytes({"carNumber": "12가1235"})

    assert produced == '{"carNumber":"12가1235"}'.encode()
    assert b"\\u" not in produced
    assert "가".encode() in produced


# --- the envelope --------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_encode_body_reproduces_the_recorded_envelope_byte_exactly(case):
    """THE test this module exists for. Payload in, exact wire string out."""
    assert crypto.encode_body(_payload(case)) == case["envelope"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_envelope_is_base64_applied_exactly_twice(case):
    """Pins the layer count from the inside, so "off by one layer" cannot pass.

    The inner value must itself be valid base64 *text*, and decoding it once more must
    yield the ciphertext — whose length must be the padded plaintext length. One layer too
    few or too many still base64-decodes to *something*; only these three facts together
    exclude it.
    """
    envelope = crypto.encode_body(_payload(case))

    inner = base64.b64decode(envelope, validate=True)
    assert inner.decode("ascii") == case["inner_b64"]

    ciphertext = base64.b64decode(inner, validate=True)
    body_len = case["body_len"]
    assert len(ciphertext) == body_len + (16 - body_len % 16)
    assert len(ciphertext) % 16 == 0


def test_block_aligned_body_gains_a_whole_extra_padding_block():
    """The quiet bug, stated as an assertion.

    `register_block_aligned` is 224 bytes = 14 blocks exactly; correct PKCS#7 makes that
    15 blocks of ciphertext, not 14. An implementation that "optimises away" the padding
    on aligned input round-trips perfectly against itself and breaks `POST /invitations`
    only for plates whose JSON hits a 16-byte multiple.
    """
    case = BY_NAME["register_block_aligned"]
    assert case["body_len"] % 16 == 0, "fixture no longer exercises the aligned case"

    ciphertext = base64.b64decode(base64.b64decode(case["envelope"], validate=True))

    assert len(ciphertext) == case["body_len"] + 16


def test_the_fixture_still_covers_both_sides_of_the_boundary():
    """Guards the matrix itself: if every recorded body drifted to the same alignment
    the suite would stay green while testing half of what it claims to."""
    remainders = {case["body_len_mod_16"] for case in CASES}

    assert 0 in remainders, "no block-aligned body left"
    assert remainders - {0}, "no unaligned body left"


def test_hangul_bodies_are_present_in_the_matrix():
    hangul_cases = [c for c in CASES if "가" in bytes.fromhex(c["body_json_hex"]).decode()]

    assert len(hangul_cases) >= 2


# --- constants ----------------------------------------------------------------


def test_key_is_the_bundle_literal_at_exactly_32_bytes():
    assert crypto.KEY == b"DlaCkdAnr!Qwer%@)*FronT$#~KinG!!"
    assert len(crypto.KEY) == 32


def test_iv_is_sixteen_zero_bytes():
    """Not a nonce and never varied — which is why identical bodies produce identical
    ciphertext, and one more reason this scheme is obfuscation rather than encryption."""
    assert crypto.IV == b"\x00" * 16


def test_fixture_key_material_matches_the_client_constants():
    """Ties the external anchor to the shipping constants. Without it the fixtures could
    be regenerated under a drifted key and still agree with a drifted `crypto.py`."""
    assert bytes.fromhex(KAT["key_hex"]) == crypto.KEY
    assert bytes.fromhex(KAT["iv_hex"]) == crypto.IV


# --- the diagnostic decrypt path ----------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_decode_body_recovers_the_payload_from_the_recorded_envelope(case):
    """Secondary, and driven by the *recorded* envelope rather than by our own output —
    so passing means inverting what openssl produced, not agreeing with ourselves."""
    assert crypto.decode_body(case["envelope"]) == _payload(case)
    assert crypto.decrypt_envelope(case["envelope"]) == bytes.fromhex(case["body_json_hex"])


def test_decrypt_envelope_rejects_a_mangled_envelope():
    """This path exists to answer "did we encode the body we thought we did?" when a
    request is rejected. Returning a partial decode would answer it wrongly.

    `ValueError` covers both failure shapes on purpose: `binascii.Error` from
    `validate=True` and the `ValueError` from `pkcs7_unpad`. Naming the base class is what
    lets these two tests assert "rejected" without pinning *which* layer noticed.
    """
    with pytest.raises(ValueError):
        crypto.decrypt_envelope("not base64 at all!!")


def test_decrypt_envelope_rejects_a_single_layer_of_base64():
    """The most likely real mistake — one `b64encode` too few — must not decode."""
    single_layer = base64.b64encode(b"\x00" * 32).decode("ascii")

    with pytest.raises(ValueError):
        crypto.decrypt_envelope(single_layer)


# --- the document -------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_recon_appendix_records_the_same_envelope_as_the_fixture(case):
    """`docs/RECON.md` Appendix A is the human-readable copy of this contract.

    Asserted by substring rather than by parsing the document's structure, so reformatting
    the prose cannot break the build — only changing a recorded value can, which is
    precisely when it should.

    **Both halves of each case, not just the ciphertext.** This test used to check the
    envelope alone, so a sweep that renamed the sample plate in the document — the plaintext
    is prose, the ciphertext is not — left the appendix stating that one body encrypts to
    another body's envelope, and the suite stayed green. An appendix that is wrong in a way
    the tests cannot see is worse than no appendix: it is the thing a future reader would
    reimplement against.
    """
    text = RECON.read_text(encoding="utf-8")
    body = bytes.fromhex(case["body_json_hex"]).decode("utf-8")

    assert case["envelope"] in text, f"{case['name']} envelope missing from docs/RECON.md"
    assert body in text, f"{case['name']} plaintext missing from docs/RECON.md"


def test_recon_no_longer_only_describes_the_method():
    """The recon originally stated the envelope *method* with no recorded value, so
    criterion 3 ("reproduce the recon envelope byte-exactly") had no target. Appendix A
    supplies one; this keeps it from being deleted as redundant."""
    text = RECON.read_text(encoding="utf-8")

    assert "Appendix A" in text
    assert "openssl enc -aes-256-cbc" in text
