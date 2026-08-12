"""Every sample plate in this repository must be a synthetic one.

## Why this is a test and not a convention

A Korean plate is `NN가NNNN`, and **any string of that shape may be a real car**. There is
nothing about `34나5678` that marks it as invented — it reads as placeholder-ish and is
indistinguishable from somebody's actual vehicle. This repository is public and the app is
distributed, so a plausible-looking plate in a doc, a UI hint or a test fixture is a licence
plate published on the internet next to the words "visitor parking".

The convention is therefore one canonical sample, `12가1234` (and `123가1234` where a
three-digit prefix is the point), with tests that need to tell two vehicles apart varying
**only the trailing digits** — `12가1235`, `12가1236`, and so on. A reader seeing the whole
family at a glance can tell it is generated rather than observed.

`12가1234` is the conventional Korean example plate, the one government forms use. That is a
convention, not a guarantee of non-existence — no syntactically valid plate can be guaranteed
unissued — but it is the string a Korean reader recognises as "the example", which is the
strongest available signal that no real vehicle is meant.

## Why a test rather than a one-time sweep

The plates in this repo were swept once already. Nothing stopped the next person from typing
a fresh plausible plate into a new fixture, and nothing would have noticed. This runs on
every commit, over the working tree, and names the file and line.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SUFFIXES = {".py", ".js", ".json", ".md", ".html", ".txt", ".svg", ".sh", ".yml", ".yaml"}

#: Every separator `plate.normalize_plate` strips, so a spaced sample is caught too — the
#: whitespace variants exist precisely to exercise stripping and are the easiest place for a
#: real-looking plate to hide.
SEPARATORS = " 　 ​‌‍﻿\t"

#: A plate literal. The lookarounds keep this off the deliberately invalid fixtures:
#: `1234가1234` ("four leading digits") contains `234가1234`, and `12가45678` ("five trailing
#: digits") contains `12가4567`. Both are negative tests whose whole point is the extra digit.
PLATE = re.compile(
    r"(?<![0-9])(?P<prefix>[0-9]{2,3})(?P<syllable>[가-힣])"
    r"(?P<separator>[" + SEPARATORS + r"]?)(?P<digits>[0-9]{4})(?![0-9])"
)

#: `999동9999호` and `101동0000호` are 세대 addresses standing in for `memb_name`, not plates.
#: They are already synthetic, and the trailing 호 is what tells them apart.
ADDRESS_SUFFIX = "호"

#: The synthetic family, once separators are stripped: `12가1234` and `123가1234` plus the
#: sequential variants a test uses to tell vehicles apart. The trailing digits are pinned to
#: `12__` rather than left free on purpose — `12가3456` would pass a prefix-only rule while
#: reading exactly like a real plate, which is the thing being prevented.
ALLOWED = re.compile(r"^(?:12|123)가12[0-9]{2}$")


def tracked_files() -> list[Path]:
    """Tracked plus untracked-but-not-ignored, the same union `check_no_personal_data.py`
    uses — a new fixture is untracked at exactly the moment it most needs checking."""
    out = []
    for args in (["git", "ls-files"], ["git", "ls-files", "--others", "--exclude-standard"]):
        result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True)
        out += [line for line in result.stdout.splitlines() if line]
    return [ROOT / name for name in sorted(set(out))]


def offenders() -> list[str]:
    found = []
    for path in tracked_files():
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        if path.name == Path(__file__).name:  # this file quotes counter-examples on purpose
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for match in PLATE.finditer(line):
                end = match.end()
                if line[end:end + 1] == ADDRESS_SUFFIX:
                    continue
                plate = (match.group("prefix") + match.group("syllable")
                         + match.group("digits"))
                if ALLOWED.match(plate):
                    continue
                relative = path.relative_to(ROOT)
                found.append(f"{relative}:{line_number}: {match.group(0)}")
    return found


def test_every_sample_plate_uses_the_synthetic_family():
    found = offenders()

    assert not found, (
        "these look like real licence plates — use 12가1234 (or 12가1235, 12가1236… when a "
        "test needs distinct vehicles):\n  " + "\n  ".join(found)
    )


@pytest.mark.parametrize(
    "line,expected",
    [
        ("34나5678", ["34나5678"]),
        ("12가 3456", ["12가 3456"]),
        ("12가1234", []),
        ("123가1299", []),
        ("999동9999호", []),          # a 세대 address, not a plate
        ("1234가1234", []),           # the "four leading digits" negative fixture
        ("12가45678", []),            # the "five trailing digits" negative fixture
        ("서울12가1234", []),          # a documented vendor form built on the canonical sample
    ],
)
def test_the_detector_reads_the_lines_it_claims_to(line, expected):
    """The scan is only as good as this regex, and a scanner that quietly matches nothing
    passes every repository. Each case here is a shape that actually appears in the tree.
    """
    matches = [
        match.group(0)
        for match in PLATE.finditer(line)
        if line[match.end():match.end() + 1] != ADDRESS_SUFFIX
        and not ALLOWED.match(
            match.group("prefix") + match.group("syllable") + match.group("digits")
        )
    ]

    assert matches == expected
