"""Store-facing text must survive App Store review, checked rather than remembered.

The App Store rejects the word **"Homey"** in store-facing copy — app name, description,
changelog, and `README.txt` / `README.ko.txt`. `README.md` / `README.en.md` are GitHub
documentation, are never read by review, and use the word freely.

This rule was switched *off* for most of this app's life: the 2026-08-04 decision was
self-install only, and `docs/DISTRIBUTION.md` recorded — correctly, and verified — that
`homey app validate --level publish` does not check these files at all. That constraint
belongs to `homey app publish`, which had never been run. So nothing in the toolchain
catches a violation, which is exactly why it is worth a test now that a submission is
intended.

The second half of this file checks that the public-release cautions did not quietly
disappear. They are the app's answer to a stranger installing something that opens a real
barrier gate, and a README edit is a very easy place to lose them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Store copy. Not the GitHub READMEs — those may say "Homey" as often as they like.
STORE_TEXT_FILES = ("README.txt", "README.ko.txt")


@pytest.mark.parametrize("name", STORE_TEXT_FILES)
def test_store_text_never_says_homey(name):
    text = (ROOT / name).read_text(encoding="utf-8")

    assert "homey" not in text.lower(), (
        f"{name} is store copy and the App Store rejects the word 'Homey' in it. "
        "Say 'the app' or 'your device' instead."
    )


@pytest.mark.parametrize("name", STORE_TEXT_FILES)
def test_store_text_exists_and_is_substantial(name):
    """A store listing with two sentences is a store listing that reads as abandoned."""
    text = (ROOT / name).read_text(encoding="utf-8").strip()

    assert len(text) > 500, f"{name} is too thin to be a store description"


def test_the_manifest_description_never_says_homey():
    """Same rule, and this one *is* the store listing rather than a file beside it."""
    manifest = json.loads((ROOT / ".homeycompose/app.json").read_text(encoding="utf-8"))

    for field in ("name", "description"):
        for language, value in manifest[field].items():
            assert "homey" not in value.lower(), f"{field}.{language} says Homey"


def test_the_changelog_never_says_homey():
    """The changelog is shown in the store listing, so the same rule reaches it.

    It is also the file most likely to break this rule by accident: a release note about a
    Homey-specific behaviour is a very natural thing to write.
    """
    changelog = json.loads((ROOT / ".homeychangelog.json").read_text(encoding="utf-8"))

    for version, entry in changelog.items():
        for language, text in entry.items():
            assert "homey" not in text.lower(), f"{version}.{language} says Homey"


# --- the cautions a stranger has to be able to read --------------------------


@pytest.mark.parametrize(
    "name,phrases",
    [
        ("README.md", ("출입통제 시스템에 즉시 반영", "비공식 클라이언트",
                       "예고 없이 동작을 멈출 수 있", "암호화되지 않습니다")),
        ("README.en.md", ("real building's access-control system", "unofficial client",
                          "without notice", "not encrypted")),
    ],
)
def test_the_public_release_cautions_are_still_there(name, phrases):
    """Four claims a stranger must meet **before** installing, one per real risk.

    Not a style check. This app is distributed to people who have never spoken to its
    author, and the first thing it can do on their behalf is grant a vehicle entry to a
    residential building. Each phrase below stands in for a disclosure that was written
    deliberately: the write is real and immediate, the client is unofficial, the vendor can
    break it without warning, and the traffic after sign-in is in the clear.
    """
    text = (ROOT / name).read_text(encoding="utf-8")

    for phrase in phrases:
        assert phrase in text, f"{name} no longer warns: {phrase}"


@pytest.mark.parametrize("name", ("README.md", "README.en.md"))
def test_the_readmes_no_longer_claim_to_be_personal_use_only(name):
    """The reversed decision, pinned so a stale sentence cannot survive a later edit.

    "Personal use only" was withdrawn on evidence: the requirement is *an* iParking MEMBERS
    account, not the maintainer's. Leaving the phrase in a published app's README would tell
    every legitimate user they are not supposed to be there.
    """
    text = (ROOT / name).read_text(encoding="utf-8").lower()

    for stale in ("personal use only", "개인 용도 전용", "앱스토어에 올리지 않습니다"):
        assert stale not in text, f"{name} still says: {stale}"
