"""The vendor's request-body envelope: `base64(base64(AES-256-CBC(PKCS#7(json))))`.

This is **obfuscation, not confidentiality.** The key below is a literal in a
JavaScript bundle served to every visitor and the IV is sixteen zero bytes, so anyone
who can read the traffic can read the body. It is reproduced here only because the
server rejects anything else. Disclose it as such in the README and settings page;
never treat it as protecting the credentials it wraps.

WHY the base64 is applied **twice**. The bundle calls `window.btoa(AESEncode(data))`,
and `AESEncode` is GibberishAES `enc()`, which *already* base64s the raw ciphertext.
So the wire value is base64 of an ASCII base64 string — roughly 2.4 bytes on the wire
per plaintext byte. Encoding once produces a body the server silently fails to parse.

WHY `ensure_ascii=False` on the JSON. It reproduces the browser's `JSON.stringify`
byte for byte. `userName` carries `memb_name`, which is Hangul (and is a home
address — see the no-log rule), so `ensure_ascii=True` would emit `\\uXXXX` escapes:
a different plaintext, of a different length, hitting different PKCS#7 padding
behaviour. Together with `separators=(",", ":")` — `json.dumps` otherwise inserts a
space after every `:` and `,` — this is what makes the envelope byte-reproducible
against a recorded capture.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from iparking_lib.iparking import aes

# ASCII bytes of the key literal in the bundle's `Aes()` helper. Exactly 32 characters
# → 256-bit. Asserted at import because a single mistyped character here would not
# crash: it would encrypt fine and produce a body the server rejects as a login
# failure, sending the maintainer hunting for a wrong password that isn't wrong.
KEY = b"DlaCkdAnr!Qwer%@)*FronT$#~KinG!!"
if len(KEY) != 32:
    raise AssertionError(f"AES-256 key must be 32 bytes, got {len(KEY)}")

# Sixteen zero bytes, hardcoded in the bundle. Not a nonce, not random, never varied —
# which is one more reason the scheme provides no confidentiality: identical bodies
# produce identical ciphertext.
IV = bytes(16)


def json_bytes(payload: Any) -> bytes:
    """Serialise exactly as the browser's `JSON.stringify` does, then UTF-8 encode.

    Kept separate from `encode_body` so tests can pin the plaintext independently of
    the cipher — when an envelope assertion fails, this tells you which half broke.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def encrypt_envelope(raw: bytes) -> str:
    """`raw` bytes → the double-base64 ASCII string that goes on the wire."""
    ciphertext = aes.encrypt(KEY, IV, raw)
    inner = base64.b64encode(ciphertext)           # GibberishAES `enc()`
    return base64.b64encode(inner).decode("ascii")  # `window.btoa(...)`


def decrypt_envelope(envelope: str | bytes) -> bytes:
    """The inverse. Present for **error diagnostics**, not for the request path.

    Responses are plaintext JSON (jQuery `dataType:'json'`), so nothing in normal
    operation decrypts anything. What this is for is the case where a request is
    rejected and the question is whether we encoded the body we thought we did — being
    able to decode our own outgoing envelope turns "the server said no" into a
    specific answer. `validate=True` so a truncated or mangled envelope raises here
    rather than silently decoding a prefix into garbage that then fails as a padding
    error two frames away.
    """
    if isinstance(envelope, str):
        envelope = envelope.encode("ascii")
    inner = base64.b64decode(envelope, validate=True)
    ciphertext = base64.b64decode(inner, validate=True)
    return aes.decrypt(KEY, IV, ciphertext)


def encode_body(payload: Any) -> str:
    """A JSON-serialisable payload → the wire body. The one function callers want."""
    return encrypt_envelope(json_bytes(payload))


def decode_body(envelope: str | bytes) -> Any:
    """Wire body → the payload it encodes. Diagnostics only; see `decrypt_envelope`."""
    return json.loads(decrypt_envelope(envelope).decode("utf-8"))
