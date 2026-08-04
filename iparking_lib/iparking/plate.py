"""Car-number (차량번호) normalization and validation — requirement 7.

Three steps, and **the order is the point**: NFC-compose, then strip, then validate.

1. **NFC first.** Some Korean IMEs (and anything that has round-tripped through a
   filesystem or an API that decomposes) emit Hangul as conjoining jamo — `가` as
   U+1100 U+1161 rather than the precomposed U+AC00. A decomposed plate fails the
   `[가-힣]` class in the vendor's regex and is rejected with nothing on screen to
   explain why: the user is looking at a plate that *is* correct being refused. The
   real site has exactly this bug. We are not reproducing it, so composition happens
   before anything looks at the characters.
2. **Then strip.** `str.isspace()` covers more than people expect — U+3000
   IDEOGRAPHIC SPACE (the one a Korean IME produces in full-width mode) and U+00A0
   NBSP are both included, so it is the right primitive. What it does *not* cover is
   the zero-width `Cf` characters that arrive by paste from web pages and messaging
   apps: U+200B ZWSP, U+200C ZWNJ, U+200D ZWJ, U+FEFF BOM/ZWNBSP. Those are invisible
   both in the input box and in any log line, so a plate carrying one is refused with
   no observable difference from a plate that is fine. They are stripped explicitly.
3. **Then validate**, against the vendor's own regex (`docs/RECON.md` — the newest of
   the three in the site's bundle).

The site itself does **not** trim; it just rejects `"12가 4567"`. The stripping here is
therefore ours, which is why it is thorough rather than a `.strip()`.

`normalize_plate` validates and raises; `strip_plate` performs steps 1–2 only. Both
exist because §3.5's idempotency predicate has to normalize *both* sides before
comparing, and the server's side is data we do not get to reject — a surprising
`car_number` in a history row must not turn a status lookup into an exception.
"""

from __future__ import annotations

import re
import unicodedata

#: The hint the site shows under the input, reused verbatim so a user who has seen the
#: real UI recognises it.
PLATE_HINT = "예시) 12가1234, 임1234, 임123456, 외교123456"

#: Zero-width characters `str.isspace()` misses. All are `Cf` (format), all render as
#: nothing, and all are common in pasted text. Spelled as escapes on purpose — written
#: literally they would be invisible in this source file too.
ZERO_WIDTH = frozenset(
    (
        "\u200b",  # ZERO WIDTH SPACE
        "\u200c",  # ZERO WIDTH NON-JOINER
        "\u200d",  # ZERO WIDTH JOINER
        "\ufeff",  # ZERO WIDTH NO-BREAK SPACE (BOM)
    )
)

#: The vendor's client-side validator, transcribed from the bundle (`docs/RECON.md`).
#: `re.ASCII` is deliberate: JavaScript's `\d` is `[0-9]`, while Python's would also
#: accept e.g. Arabic-Indic digits, and a plate this app accepts but the server's own
#: validator rejects is a failure we would only see in production.
PLATE_RE = re.compile(
    r"^(?:(?:[가-힣]{2}|\d)\d{1,2})[가-힣]\d{4}$"
    r"|^임(?:\d{4}|\d{6})$"
    r"|^(?:(?:외교|영사|준외|준영|국기|협정|대표)\d{6})$",
    re.ASCII,
)


class InvalidPlateError(ValueError):
    """A plate that is not accepted by the vendor's validator.

    Carries `PLATE_HINT` in its message because this exception is rendered straight to
    the user on both surfaces, and "invalid" without an example is not actionable.
    """

    #: i18n key for the message (`locales/{ko,en}.json`).
    key = "bad_plate"

    def __init__(self, value: str) -> None:
        self.value = value
        super().__init__(f"차량번호 형식이 올바르지 않습니다. {PLATE_HINT}")


def strip_plate(value: str | None) -> str:
    """NFC-compose `value` and remove every space-like and zero-width character.

    Does **not** validate. Use this on values coming *back* from the server, where the
    goal is a comparable form rather than a verdict.
    """
    composed = unicodedata.normalize("NFC", value or "")
    return "".join(
        ch for ch in composed if not ch.isspace() and ch not in ZERO_WIDTH
    )


def mask_plate(value: str | None) -> str:
    """A plate safe to put in a log line or a diagnostic report: `12가3456` → `12가****`.

    Lives here rather than in `api.py` because the pure client logs plates too, and two
    implementations of a redaction rule is one too many — the day they diverge is the day
    the unmasked one is the copy that ships.

    Masks the **last four characters**, which is exactly the serial part of every shape the
    vendor's regex accepts (`12가1234`, `서울12가1234`, `임1234`, `외교123456`), leaving the
    region/class prefix legible so a user can still tell which of their entries a message is
    about. Anything shorter than five characters is replaced wholesale rather than partly
    revealed: a short string is either malformed or already too small to blur usefully.
    """
    stripped = strip_plate(value)
    if not stripped:
        return ""
    if len(stripped) <= 4:
        return "*" * len(stripped)
    return stripped[:-4] + "****"


def normalize_plate(value: str | None) -> str:
    """The plate as it must be sent: composed, stripped, and validated.

    Raises `InvalidPlateError` if the result does not match the vendor's regex.
    """
    stripped = strip_plate(value)
    # fullmatch, not match: Python's `$` also matches immediately before a trailing
    # newline, so `match()` would accept "임1234\n". Step 2 already removes newlines,
    # which makes this belt-and-braces — but the pattern is kept byte-identical to the
    # bundle's so it can be diffed against `docs/RECON.md`, and the guard is what lets
    # it stay that way.
    if not PLATE_RE.fullmatch(stripped):
        raise InvalidPlateError(stripped)
    return stripped
