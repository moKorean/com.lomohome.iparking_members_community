#!/usr/bin/env python3
"""Fail if real personal data reaches the repository or its history.

This app is developed against one real account at one real building, so real values
are always within reach: a login id, a unit number, the apartment's name, neighbours'
plate numbers, account sequence numbers, a bearer token. Several of them did reach a
commit once and had to be scrubbed with a history rewrite.

Hand review does not catch this. Captured API responses get pasted into docs and
fixtures verbatim, and the login id itself *encodes the address*, so masking the
address while leaving the id leaks it anyway. Hence a gate.

**The values are stored as salted hashes, never as plaintext.** The first version of
this script listed them literally, which meant the detector was itself the largest
disclosure in the repo — it published the apartment name, the neighbours' plates and
the password to anyone who opened it, and it could never pass its own scan. Excluding
the file from its own scan would have hidden that rather than fixed it. So instead:
tokenize each file, normalize each candidate the way `plate.py` normalizes a plate,
hash it, and look the hash up. Nothing here reveals what it is looking for.

The tradeoff is that this matches whole tokens only, not substrings. That is why
`_tokens` splits generously — digit runs, hex runs, Hangul runs, plate shapes with
separators stripped — so a real value is a token under at least one rule.

    python3 scripts/check_no_personal_data.py            # tracked files + all history
    python3 scripts/check_no_personal_data.py --tracked  # working tree only (for a hook)
    python3 scripts/check_no_personal_data.py --add VALUE # print the hash line to add

Use synthetic values in tests, fixtures and docs: `iparking-dev`, `999동9999호`,
`예시동 샘플아파트`, plates shaped like `12가3456`, account ids like `100001` / `9001`.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import unicodedata

SALT = "iparking-personal-data-v1"

# sha256(SALT + normalized value)[:32] -> what it is, and why it must not ship.
# Add with --add; never paste a real value into this file.
SECRETS: dict[str, tuple[str, str]] = {
    "2e67316124c6fb4ebfc13640c42742ff": (
        "login id",
        "encodes the building and unit, so it is the address in another form",
    ),
    "418a19578dae65f8dbe83902896301e2": (
        "password",
        "the development password; must never appear in the repo or its history",
    ),
    "bfc652ace8bc539e0a773b61c612b199": (
        "password stem",
        "as above, without the trailing punctuation",
    ),
    "a16c9c968828279ca28fc8bbaef3438e": ("home address", "the maintainer's unit"),
    "eb812b7d26757c82e43c1a066935260d": ("unit number", "the maintainer's unit"),
    "2aa89640a392439254456ceda799a6f7": (
        "building name",
        "identifies where the maintainer lives",
    ),
    "b5620eb839adb2481ea373b35c0d8874": (
        "neighbourhood",
        "identifies where the maintainer lives",
    ),
    "8ff250d4879c8e0baf98cfed2d55ade3": ("stor_seq", "ties a capture to the real account"),
    "4b67d59a0cc313cd076392232d3cf362": ("cmpy_seq", "ties a capture to the real account"),
    "556d58ff66607dfe0ffc7f60d40e5fe6": ("park_seq", "identifies the specific car park"),
    "df598cee340d85a9757a4550e9b7569b": ("lot_id", "identifies the specific car park"),
    "74c48e1ea9b68ea4a6f4ba8affc6707e": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "00aae1c73ade34877cdd038bad4314bd": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "367c59e08e6c01ab6716070b865250a6": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "39ec61d64731ab02e4ed52a955848bbe": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "a0696dce5fbecef2042e7e4d412a04d5": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "69872491de44a75487c4244a2a63c528": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "26dda82d86c8823b51fa8071cb95c4e6": (
        "resident plate",
        "third-party personal data: another resident's vehicle, never consented to",
    ),
    "dbb5d15f053e243300305a7425c4acbb": (
        "bearer token",
        "a 7-day credential that can register and cancel vehicles",
    ),
    "1528ce90edf95b9e57019d881b6a6f05": (
        "bearer token",
        "a 7-day credential that can register and cancel vehicles",
    ),
    "8a15af027fc7f0214682c512ab785614": (
        "operation mapping uuid",
        "an account-scoped identifier",
    ),
}

# Zero-width characters that `str.isspace()` misses. Same set as plate.py, and here for
# the same reason: `333러<U+200B>2655` is still a real plate.
_ZERO_WIDTH = "​‌‍﻿"

_TOKEN_RULES = (
    r"[0-9]{4,12}",  # account/park sequence numbers
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",  # a uuid
    r"[0-9a-fA-F]{8,}",  # a token fragment
    r"[가-힣]{2,10}",  # a place or building name
    r"[0-9]{1,4}\s*동\s*[0-9]{1,5}\s*호",  # a unit, e.g. 999동 9999호
    # A plate, tolerating the separators the plate tests deliberately interleave.
    r"(?:[가-힣]{2}|[0-9]{1,3})[\s 　​-‍﻿]*"
    r"[0-9]{0,2}[\s 　​-‍﻿]*"
    r"[가-힣][\s 　​-‍﻿]*[0-9]{4}",
    r"[A-Za-z][A-Za-z0-9]{4,24}",  # an id or password-like run
)
_TOKENS = re.compile("|".join(f"(?:{r})" for r in _TOKEN_RULES))

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2")


def normalize(value: str) -> str:
    """NFC-compose, drop whitespace and zero-width characters, casefold.

    Mirrors `plate.normalize_plate`'s first two steps on purpose: a value is the same
    secret whether or not somebody pasted a non-breaking space into the middle of it.
    """
    composed = unicodedata.normalize("NFC", value)
    stripped = "".join(c for c in composed if not c.isspace() and c not in _ZERO_WIDTH)
    return stripped.casefold()


def digest(value: str) -> str:
    return hashlib.sha256((SALT + normalize(value)).encode("utf-8")).hexdigest()[:32]


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, errors="replace").stdout


def _tracked() -> list[tuple[str, str]]:
    """Tracked files **and** untracked-but-not-ignored ones.

    The untracked half is not a nicety. An earlier version read only `git ls-files`, so a
    worker who had just created nine new files got a clean report that covered none of
    them — the check passed *vacuously*, which is worse than not running it, because the
    output looks like evidence. Anything git would include in the next `git add -A` is in
    scope here.
    """
    listed = _run("git", "ls-files").split("\n")
    listed += _run("git", "ls-files", "--others", "--exclude-standard").split("\n")
    out = []
    for path in dict.fromkeys(listed):
        if not path or path.endswith(SKIP_SUFFIXES):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                out.append((path, fh.read()))
        except OSError:
            continue
    return out


def _history() -> list[tuple[str, str]]:
    """Every blob reachable from any ref, deduplicated by sha.

    Needed because editing a file does not remove what an earlier commit recorded —
    which is exactly how this repo's first leak survived a redaction.
    """
    seen: dict[str, str] = {}
    for line in _run("git", "rev-list", "--objects", "--all").split("\n"):
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and not parts[1].endswith(SKIP_SUFFIXES):
            seen.setdefault(parts[0], parts[1])
    out = []
    for sha, path in seen.items():
        if _run("git", "cat-file", "-t", sha).strip() == "blob":
            out.append((f"{sha[:9]} ({path})", _run("git", "cat-file", "-p", sha)))
    return out


def scan(items: list[tuple[str, str]]) -> list[str]:
    findings = []
    for label, text in items:
        for match in _TOKENS.finditer(text):
            hit = SECRETS.get(digest(match.group(0)))
            if hit is None:
                continue
            what, why = hit
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"  {label}:{line}  [{what}]\n      {why}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail if real personal data is committed.")
    parser.add_argument("--tracked", action="store_true", help="skip history (faster)")
    parser.add_argument("--add", metavar="VALUE", help="print the hash entry for a value")
    args = parser.parse_args()

    if args.add:
        print(f'    "{digest(args.add)}": ("describe it", "say why it must not ship"),')
        return 0

    findings = scan(_tracked())
    scope = "tracked files"
    if not args.tracked:
        findings += scan(_history())
        scope = "tracked files and all reachable history"

    if not findings:
        print(f"No personal data found in {scope}.")
        return 0

    print(f"Personal data found in {scope}:\n", file=sys.stderr)
    for finding in dict.fromkeys(findings):
        print(finding, file=sys.stderr)
    print(
        "\nReplace each with a synthetic value. If it is already committed, editing the "
        "file is not enough — the blob has to go too: rewrite the history, then "
        "`git reflog expire --expire=now --all && git gc --prune=now`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
