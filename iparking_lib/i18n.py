"""Translation for messages raised from Python.

Homey's own server-side i18n cannot be used for this: `homey.i18n.get_language()` returns
the *app's* language, which resolves to 'en' on this firmware even with `locales/ko.json`
loaded (established in the sibling app `com.lomohome.navien` — see `navien_lib/i18n.py` and
its `docs/PORTING.md`). The webviews do know the language the user is looking at, so they
report it (`POST /language` → `compat.remember_ui_language`) and it is stored; this module
reads the same `locales/*.json` files Homey itself uses and formats from them.

That is why translation lives here at all rather than being deferred to the SDK: every
user-visible sentence this app produces — a rejected plate, `register_uncertain`, a vendor
result code — is raised from Python and carries an `i18n` *key* (`IparkingError.key`,
`InvalidPlateError.key`, `codes.result_key`), never prose. This module is the one place
those keys become text.

English is the fallback at every step: an unknown language, a missing key, or a bad
placeholder all fall back rather than raise. A user-facing error message is the worst
possible place to add a second failure.

Copied, near-verbatim, from `navien_lib/i18n.py` — this layer is vendor-neutral.
"""

import json
from pathlib import Path

DEFAULT = "en"
_LOCALES = Path(__file__).parent.parent / "locales"
_cache: dict[str, dict] = {}


def _strings(language: str) -> dict:
    if language in _cache:
        return _cache[language]
    path = _LOCALES / f"{language}.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        loaded = {}
    _cache[language] = loaded
    return loaded


def _lookup(table: dict, key: str):
    node = table
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def translate(key: str, language: str = DEFAULT, **params) -> str:
    """The string for `key`, in `language` where available and English otherwise.

    Returns the key itself if it exists in neither, which is ugly but traceable — far
    better than an empty message the user cannot report.
    """
    code = (language or DEFAULT)[:2].lower()
    template = _lookup(_strings(code), key)
    if template is None and code != DEFAULT:
        template = _lookup(_strings(DEFAULT), key)
    if template is None:
        return key
    if not params:
        return template
    try:
        return template.format(**params)
    except Exception:
        return template
