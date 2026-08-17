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
import re
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

# --- the pair and repair views must not be Korean-only ------------------------


PAIR_VIEWS = ("drivers/visitcar/pair/start.html", "drivers/visitcar/repair/reconnect.html")

HANGUL = re.compile(r"[\uac00-\ud7a3]")


@pytest.mark.parametrize("name", PAIR_VIEWS)
def test_a_pair_view_ships_english_in_its_markup(name):
    """App Store review, 2026-08-17: "Your app contains pairing and repair views that are
    currently only shown in Korean. Please use English language in the main app UI and add
    translated files or translated strings for other languages."

    The **markup** is what this asserts, not the script: it is what a viewer sees before any
    language resolves, and before this fix it was Korean with no English anywhere. Korean is
    now applied on top once the viewer's language is known, so Hangul in the body means the
    default was written in the wrong language again.
    """
    text = (ROOT / name).read_text(encoding="utf-8")
    body = text.split("<script")[0]

    assert not HANGUL.search(body), (
        f"{name}'s markup contains Korean; English is the default and Korean is layered on "
        "by the STR table in the script below it"
    )


@pytest.mark.parametrize("name", PAIR_VIEWS)
def test_a_pair_view_carries_both_languages(name):
    """English alone would satisfy review and lose every Korean user this app is built for.
    Both tables must exist, and neither may be empty."""
    text = (ROOT / name).read_text(encoding="utf-8")

    assert "STR = {" in text, f"{name} has no string table"
    for language in ("en:", "ko:"):
        assert language in text, f"{name} has no {language.rstrip(':')} strings"
    assert HANGUL.search(text), f"{name} lost its Korean translations"


@pytest.mark.parametrize("name", PAIR_VIEWS)
def test_a_pair_view_falls_back_to_english(name):
    """`LANG` starts at `en` and every lookup ends there. A viewer whose language cannot be
    determined — the case that produced this review note — must not land on Korean."""
    text = (ROOT / name).read_text(encoding="utf-8")

    assert 'var LANG = "en";' in text
    assert "STR.en[key]" in text


def test_the_pairing_handlers_answer_in_english_with_a_key():
    """The pair view renders `reason_key` in the viewer's language and shows `reason` when it
    cannot. That fallback text is therefore the untranslated path, so it has to be English —
    and the key has to keep existing, or the view silently drops to it forever."""
    source = (ROOT / "iparking_lib/pairing.py").read_text(encoding="utf-8")
    driver = (ROOT / "iparking_lib/visitcar/driver.py").read_text(encoding="utf-8")

    assert '"reason_key"' in source, "the view needs a key to translate"
    for name, text in (("pairing.py", source), ("driver.py", driver)):
        for line in text.splitlines():
            stripped = line.strip()
            # Only the module-level user-facing constants; comments and docstrings may be
            # Korean, and `iparking_lib/i18n.py` owns the translations themselves.
            if stripped.startswith(("_NEED_LOGIN =", "_SLOW_LOGIN =", '    "No parking',
                                    "_NO_LOTS = (")):
                assert not HANGUL.search(line), f"{name}: {stripped[:60]} is not English"


@pytest.mark.parametrize("key", ("pair_need_login", "pair_slow_login", "pair_no_lots"))
def test_the_pairing_strings_are_translated_in_both_locales(key):
    """Review asked for "translated files or translated strings for other languages". The
    pair views carry their own copies out of necessity; these are the canonical ones, and a
    key present in only one locale is how a Korean hub silently falls back to English."""
    for language in ("ko", "en"):
        table = json.loads((ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8"))
        assert table.get(key), f"{language}.json is missing {key}"
