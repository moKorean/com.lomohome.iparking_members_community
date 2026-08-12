"""Plate normalization — requirement 7, acceptance criterion 9.

Every separator is written as `chr(0x…)` rather than as a literal. That is not pedantry:
half of these characters are invisible, and a test whose input you cannot see is a test
whose failure you cannot read. The codepoint tables below are the actual specification of
what "strip all whitespace" means in this app.

`tests/conftest.py` puts the repo root on `sys.path`; nothing here does its own.
"""

import unicodedata

import pytest

from iparking_lib.iparking.plate import (
    PLATE_HINT,
    InvalidPlateError,
    mask_plate,
    normalize_plate,
    strip_plate,
)

PLATE = "12가1235"

# Characters `str.isspace()` returns True for. The point of the table is that it is much
# wider than "space and tab" — U+3000 is what a Korean IME emits in full-width mode, and
# U+00A0 is what a browser or a word processor substitutes for a typed space.
ISSPACE_CODEPOINTS = [
    (0x0009, "CHARACTER TABULATION"),
    (0x000A, "LINE FEED"),
    (0x000B, "LINE TABULATION"),
    (0x000C, "FORM FEED"),
    (0x000D, "CARRIAGE RETURN"),
    (0x001C, "INFORMATION SEPARATOR FOUR"),
    (0x001D, "INFORMATION SEPARATOR THREE"),
    (0x001E, "INFORMATION SEPARATOR TWO"),
    (0x001F, "INFORMATION SEPARATOR ONE"),
    (0x0020, "SPACE"),
    (0x0085, "NEXT LINE"),
    (0x00A0, "NO-BREAK SPACE"),
    (0x1680, "OGHAM SPACE MARK"),
    (0x2000, "EN QUAD"),
    (0x2001, "EM QUAD"),
    (0x2002, "EN SPACE"),
    (0x2003, "EM SPACE"),
    (0x2004, "THREE-PER-EM SPACE"),
    (0x2005, "FOUR-PER-EM SPACE"),
    (0x2006, "SIX-PER-EM SPACE"),
    (0x2007, "FIGURE SPACE"),
    (0x2008, "PUNCTUATION SPACE"),
    (0x2009, "THIN SPACE"),
    (0x200A, "HAIR SPACE"),
    (0x2028, "LINE SEPARATOR"),
    (0x2029, "PARAGRAPH SEPARATOR"),
    (0x202F, "NARROW NO-BREAK SPACE"),
    (0x205F, "MEDIUM MATHEMATICAL SPACE"),
    (0x3000, "IDEOGRAPHIC SPACE"),
]

# The zero-width `Cf` characters `str.isspace()` misses. These arrive by paste from web
# pages and messaging apps, and nothing anywhere renders them.
ZERO_WIDTH_CODEPOINTS = [
    (0x200B, "ZERO WIDTH SPACE"),
    (0x200C, "ZERO WIDTH NON-JOINER"),
    (0x200D, "ZERO WIDTH JOINER"),
    (0xFEFF, "ZERO WIDTH NO-BREAK SPACE"),
]


@pytest.mark.parametrize(
    "codepoint,name", ISSPACE_CODEPOINTS, ids=[n for _, n in ISSPACE_CODEPOINTS]
)
def test_isspace_characters_are_stripped(codepoint, name):
    ch = chr(codepoint)
    # Guards the table itself: if a future Python stops calling one of these a space, the
    # implementation would silently need the explicit set instead, and this is where that
    # shows up.
    assert ch.isspace(), f"U+{codepoint:04X} {name} is no longer isspace()"
    assert normalize_plate("12가" + ch + "1235") == PLATE


@pytest.mark.parametrize(
    "codepoint,name", ZERO_WIDTH_CODEPOINTS, ids=[n for _, n in ZERO_WIDTH_CODEPOINTS]
)
def test_zero_width_characters_are_stripped(codepoint, name):
    ch = chr(codepoint)
    # The whole reason ZERO_WIDTH exists: isspace() is False here, so relying on it alone
    # would reject these plates with nothing visible to explain why.
    assert not ch.isspace(), f"U+{codepoint:04X} {name} is now isspace(); simplify plate.py"
    assert unicodedata.category(ch) == "Cf"
    assert normalize_plate("12가" + ch + "1235") == PLATE


def test_criterion_9_examples():
    """The exact inputs named in acceptance criterion 9."""
    assert normalize_plate("12가 1235") == PLATE                    # U+0020
    assert normalize_plate("12가" + chr(0x3000) + "1235") == PLATE  # U+3000
    assert normalize_plate("12가" + chr(0x00A0) + "1235") == PLATE  # U+00A0
    assert normalize_plate("12가\t1235") == PLATE                   # U+0009
    assert normalize_plate("12가" + chr(0x200B) + "1235") == PLATE  # U+200B
    assert normalize_plate(unicodedata.normalize("NFD", PLATE)) == PLATE


def test_decomposed_jamo_is_composed_before_validation():
    decomposed = unicodedata.normalize("NFD", PLATE)
    # Without this the test would be vacuous on a Python whose NFD left the syllable alone.
    assert decomposed != PLATE
    assert len(decomposed) > len(PLATE)
    # U+AC00 가 decomposes to U+1100 ᄀ + U+1161 ᅡ, neither of which is in [가-힣].
    assert chr(0x1100) in decomposed and chr(0x1161) in decomposed
    assert normalize_plate(decomposed) == PLATE


def test_decomposed_and_whitespace_together():
    """The realistic paste: a decomposed plate that also picked up an invisible space."""
    messy = unicodedata.normalize("NFD", "12가") + chr(0x200B) + chr(0x3000) + "1235"
    assert normalize_plate(messy) == PLATE


def test_leading_trailing_and_repeated_whitespace():
    assert normalize_plate("  12가 1235\n") == PLATE
    assert normalize_plate("1 2 가 1 2 3 5") == PLATE
    assert normalize_plate(chr(0xFEFF) + "12가1235" + chr(0xFEFF)) == PLATE


@pytest.mark.parametrize(
    "value",
    [
        "12가1234",
        "123가1234",
        "서울12가1234",
        "임1234",
        "임123456",
        "외교123456",
        "영사123456",
        "준외123456",
        "준영123456",
        "국기123456",
        "협정123456",
        "대표123456",
    ],
)
def test_vendor_regex_accepts_every_documented_form(value):
    assert normalize_plate(value) == value


@pytest.mark.parametrize(
    "value,why",
    [
        ("12가456", "criterion 9: three trailing digits"),
        ("12가45678", "five trailing digits"),
        ("", "empty"),
        (None, "absent"),
        ("   ", "whitespace only"),
        (chr(0x200B) * 3, "zero-width only"),
        ("12A4567", "Latin letter where a Hangul syllable belongs"),
        ("임12345", "임 takes 4 or 6 digits, not 5"),
        ("외교12345", "diplomatic plates take exactly 6 digits"),
        ("외교1234567", "diplomatic plates take exactly 6 digits"),
        ("여권123456", "not one of the seven special prefixes"),
        ("1234가1234", "four leading digits"),
        ("１２가4567", "full-width digits: the vendor's \\d is [0-9]"),
        ("١٢가1234", "Arabic-Indic digits: the vendor's \\d is [0-9]"),
        (
            "12가1234" + chr(0x200B) + "5",
            "zero-width in the middle of a longer, still-invalid plate",
        ),
    ],
)
def test_invalid_plates_are_refused(value, why):
    with pytest.raises(InvalidPlateError) as excinfo:
        normalize_plate(value)
    assert "예시)" in str(excinfo.value), why
    assert PLATE_HINT in str(excinfo.value)


def test_error_is_a_valueerror_and_carries_context():
    with pytest.raises(InvalidPlateError) as excinfo:
        normalize_plate("12가 456")
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.key == "bad_plate"
    # The stripped attempt, not the raw input: that is what was actually rejected, and it
    # is what the settings page echoes back into the field.
    assert error.value == "12가456"


def test_strip_plate_does_not_validate():
    """Server-supplied values go through `strip_plate`, which must never raise.

    A history row carrying something the vendor's own validator would reject must still be
    comparable, or the idempotency predicate in §3.5 turns into an exception.
    """
    assert strip_plate("완전히 이상한 값") == "완전히이상한값"
    assert strip_plate("") == ""
    assert strip_plate(None) == ""
    assert strip_plate(unicodedata.normalize("NFD", "12가1236")) == "12가1236"


# --- mask_plate: the redaction rule for logs and diagnostics ----------------------


@pytest.mark.parametrize(
    "plate,masked",
    [
        ("12가1236", "12가****"),    # the example in CLAUDE.md's "never logged" rule
        ("12가1235", "12가****"),
        ("123가1234", "123가****"),
        ("서울12가1234", "서울12가****"),
        ("임1234", "임****"),
        ("임123456", "임12****"),
        ("외교123456", "외교12****"),
    ],
)
def test_mask_plate_hides_the_serial(plate, masked):
    """Masking exists because diagnostic reports get pasted into issues and chats.

    The prefix stays legible so a user can still tell which of their entries a line refers
    to; the four-character serial — the part that identifies a *guest's* car — never appears.
    """
    assert mask_plate(plate) == masked
    assert plate[-4:] not in mask_plate(plate)


def test_mask_plate_normalizes_before_masking():
    """Otherwise `"12가 1235"` would log with its whitespace, i.e. as a second spelling.

    Two spellings of one plate in a log is two chances for the unmasked characters to differ
    from what the mask assumed.
    """
    assert mask_plate("12가 1235") == "12가****"
    assert mask_plate(unicodedata.normalize("NFD", "12가1236")) == "12가****"
    assert mask_plate("12가" + chr(0x200B) + "1235") == "12가****"


@pytest.mark.parametrize(
    "value,masked",
    [
        ("", ""),
        (None, ""),
        ("   ", ""),
        ("가", "*"),
        ("1234", "****"),          # exactly four: nothing may be revealed
        ("완전히 이상한 값", "완전히****"),   # 7 chars once stripped, so 3 survive
    ],
)
def test_mask_plate_never_raises_and_never_under_masks(value, masked):
    """A redaction helper that raises turns a log line into an outage, so it cannot.

    A value of four characters or fewer is replaced wholesale rather than partly revealed: it
    is either malformed or already too small for a partial mask to hide anything.
    """
    assert mask_plate(value) == masked
