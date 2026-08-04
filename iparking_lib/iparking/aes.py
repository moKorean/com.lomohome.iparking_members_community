"""AES-256-CBC with PKCS#7 padding, in pure Python.

WHY this file exists at all. The vendor encrypts every request body with
AES-256-CBC under a key shipped to every browser, and the Homey Python runtime has
no stdlib AES. The two candidates were a `pythonPackages` dependency
(`cryptography` ships prebuilt abi3 wheels for both hub architectures, ~38 MB each)
and ~200 lines of a fully specified primitive at 1.3 MB of total install budget.
Vendoring won on size, and the correctness risk it would normally carry is retired
by `tests/test_aes.py`, which pins this implementation to ciphertext produced
*outside* it: nine `openssl` fixtures plus the NIST SP 800-38A F.2.1/F.2.2
known-answer vectors. The decision is deliberately cheap to reverse — swapping in
`cryptography` is a ~20-line diff in `crypto.py` plus deleting fixtures.

Read that as a standing instruction: **do not "verify" a change to this file with a
round-trip test.** `encrypt` checked against `decrypt` passes even when both are
wrong in the same direction — a flipped PKCS#7 pad byte, or a self-cancelling
transform, round-trips perfectly at every length. Only external vectors can see it.

Scope is deliberately narrow: one cipher, one mode, one padding, no key sizes other
than 256-bit. This is not a crypto library and must not grow into one. It provides no
authentication (CBC is malleable) and makes no constant-time claim; it reproduces a
vendor's obfuscation scheme, which is all it is here to do.
"""

from __future__ import annotations

BLOCK_SIZE = 16

# FIPS 197 Figure 7, one string per row exactly as the standard prints it so it can be
# diffed against the published table by eye. Hardcoded rather than derived from the
# GF(2^8) multiplicative inverse: a transcription slip here fails every external vector
# loudly, whereas a subtle bug in a clever derivation is exactly the kind of thing that
# yields a *plausible* cipher. The inverse box is derived from this one, so the two can
# never drift apart.
_SBOX_ROWS = (
    "637c777bf26b6fc53001672bfed7ab76",
    "ca82c97dfa5947f0add4a2af9ca472c0",
    "b7fd9326363ff7cc34a5e5f171d83115",
    "04c723c31896059a071280e2eb27b275",
    "09832c1a1b6e5aa0523bd6b329e32f84",
    "53d100ed20fcb15b6acbbe394a4c58cf",
    "d0efaafb434d338545f9027f503c9fa8",
    "51a3408f929d38f5bcb6da2110fff3d2",
    "cd0c13ec5f974417c4a77e3d645d1973",
    "60814fdc222a908846eeb814de5e0bdb",
    "e0323a0a4906245cc2d3ac629195e479",
    "e7c8376d8dd54ea96c56f4ea657aae08",
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a",
    "703eb5664803f60e613557b986c11d9e",
    "e1f8981169d98e949b1e87e9ce5528df",
    "8ca1890dbfe6426841992d0fb054bb16",
)
if len(_SBOX_ROWS) != 16 or any(len(row) != 32 for row in _SBOX_ROWS):
    raise AssertionError("S-box must be 16 rows of 16 bytes")
SBOX = bytes.fromhex("".join(_SBOX_ROWS))

_inverse = bytearray(256)
for _index, _value in enumerate(SBOX):
    _inverse[_value] = _index
INV_SBOX = bytes(_inverse)
if sorted(SBOX) != list(range(256)):
    raise AssertionError("S-box is not a permutation of 0..255 — a byte was mistyped")


def _xtime(byte: int) -> int:
    """Multiply by x (i.e. by 0x02) in GF(2^8) modulo the AES polynomial 0x11b."""
    byte <<= 1
    if byte & 0x100:
        byte ^= 0x11B
    return byte & 0xFF


def _mul(a: int, b: int) -> int:
    """Multiply two GF(2^8) elements. Used for the MixColumns constants only."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


# Precomputed multiplication tables for the constants MixColumns and its inverse use.
# 0x02/0x03 for the forward direction, 0x09/0x0b/0x0d/0x0e for the inverse.
_MUL = {c: bytes(_mul(x, c) for x in range(256)) for c in (0x02, 0x03, 0x09, 0x0B, 0x0D, 0x0E)}


def _expand_key(key: bytes) -> list[bytes]:
    """FIPS 197 §5.2 key expansion, 256-bit keys only (Nk=8, Nr=14 → 60 words).

    The Nk=8 branch that Nk=4 does not have is `i % Nk == 4 → SubWord`; omitting it
    yields a cipher that still round-trips, which is why this is pinned externally.
    """
    if len(key) != 32:
        raise ValueError(f"AES-256 needs a 32-byte key, got {len(key)}")
    nk, nr = 8, 14
    words = [bytearray(key[4 * i : 4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = bytearray(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]                    # RotWord
            temp = bytearray(SBOX[b] for b in temp)       # SubWord
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            temp = bytearray(SBOX[b] for b in temp)       # SubWord, AES-256 only
        words.append(bytearray(a ^ b for a, b in zip(words[i - nk], temp, strict=True)))
    return [bytes(w) for w in words]


def _add_round_key(state: bytearray, words: list[bytes], rnd: int) -> None:
    for col in range(4):
        word = words[rnd * 4 + col]
        for row in range(4):
            state[row + 4 * col] ^= word[row]


def _sub_bytes(state: bytearray, box: bytes) -> None:
    for i in range(16):
        state[i] = box[state[i]]


def _shift_rows(state: bytearray) -> None:
    for row in range(1, 4):
        original = [state[row + 4 * col] for col in range(4)]
        for col in range(4):
            state[row + 4 * col] = original[(col + row) % 4]


def _inv_shift_rows(state: bytearray) -> None:
    for row in range(1, 4):
        original = [state[row + 4 * col] for col in range(4)]
        for col in range(4):
            state[row + 4 * ((col + row) % 4)] = original[col]


def _mix_columns(state: bytearray) -> None:
    mul2, mul3 = _MUL[0x02], _MUL[0x03]
    for col in range(4):
        base = 4 * col
        a0, a1, a2, a3 = state[base], state[base + 1], state[base + 2], state[base + 3]
        state[base] = mul2[a0] ^ mul3[a1] ^ a2 ^ a3
        state[base + 1] = a0 ^ mul2[a1] ^ mul3[a2] ^ a3
        state[base + 2] = a0 ^ a1 ^ mul2[a2] ^ mul3[a3]
        state[base + 3] = mul3[a0] ^ a1 ^ a2 ^ mul2[a3]


def _inv_mix_columns(state: bytearray) -> None:
    mul9, mul11, mul13, mul14 = _MUL[0x09], _MUL[0x0B], _MUL[0x0D], _MUL[0x0E]
    for col in range(4):
        base = 4 * col
        a0, a1, a2, a3 = state[base], state[base + 1], state[base + 2], state[base + 3]
        state[base] = mul14[a0] ^ mul11[a1] ^ mul13[a2] ^ mul9[a3]
        state[base + 1] = mul9[a0] ^ mul14[a1] ^ mul11[a2] ^ mul13[a3]
        state[base + 2] = mul13[a0] ^ mul9[a1] ^ mul14[a2] ^ mul11[a3]
        state[base + 3] = mul11[a0] ^ mul13[a1] ^ mul9[a2] ^ mul14[a3]


def encrypt_block(words: list[bytes], block: bytes) -> bytes:
    """One 16-byte AES-256 block forward, given expanded key words."""
    state = bytearray(block)
    _add_round_key(state, words, 0)
    for rnd in range(1, 14):
        _sub_bytes(state, SBOX)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, words, rnd)
    _sub_bytes(state, SBOX)
    _shift_rows(state)
    _add_round_key(state, words, 14)
    return bytes(state)


def decrypt_block(words: list[bytes], block: bytes) -> bytes:
    """One 16-byte AES-256 block backward, given expanded key words."""
    state = bytearray(block)
    _add_round_key(state, words, 14)
    for rnd in range(13, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, INV_SBOX)
        _add_round_key(state, words, rnd)
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _sub_bytes(state, INV_SBOX)
    _add_round_key(state, words, 0)
    return bytes(state)


def pkcs7_pad(data: bytes) -> bytes:
    """Append 1..16 bytes so the length is a multiple of 16.

    An already-aligned input gains a **whole extra block** of 0x10 bytes; that is not
    an edge case to optimise away, it is what makes the padding unambiguous. Lengths
    16 and 32 are in the fixture matrix precisely to hold this behaviour still.
    """
    pad = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding, rejecting anything malformed.

    Every branch here is a real failure mode of a wrong key or a truncated response,
    and this is a *diagnostic* path (see `crypto.decrypt_envelope`), so it must raise
    rather than return something that looks like a plaintext.
    """
    if not data or len(data) % BLOCK_SIZE:
        raise ValueError("ciphertext length is not a positive multiple of 16")
    pad = data[-1]
    if not 1 <= pad <= BLOCK_SIZE:
        raise ValueError("bad PKCS#7 padding length")
    if data[-pad:] != bytes([pad]) * pad:
        raise ValueError("bad PKCS#7 padding bytes")
    return data[:-pad]


def cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """CBC-encrypt already-padded plaintext. Raises if it is not block-aligned."""
    if len(iv) != BLOCK_SIZE:
        raise ValueError(f"IV must be {BLOCK_SIZE} bytes, got {len(iv)}")
    if len(plaintext) % BLOCK_SIZE:
        raise ValueError("plaintext must be padded to a multiple of 16 before CBC")
    words = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(plaintext), BLOCK_SIZE):
        block = plaintext[offset : offset + BLOCK_SIZE]
        xored = bytes(a ^ b for a, b in zip(block, previous, strict=True))
        previous = encrypt_block(words, xored)
        out += previous
    return bytes(out)


def cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """CBC-decrypt to still-padded plaintext. Raises if it is not block-aligned."""
    if len(iv) != BLOCK_SIZE:
        raise ValueError(f"IV must be {BLOCK_SIZE} bytes, got {len(iv)}")
    if not ciphertext or len(ciphertext) % BLOCK_SIZE:
        raise ValueError("ciphertext length is not a positive multiple of 16")
    words = _expand_key(key)
    out = bytearray()
    previous = iv
    for offset in range(0, len(ciphertext), BLOCK_SIZE):
        block = ciphertext[offset : offset + BLOCK_SIZE]
        decrypted = decrypt_block(words, block)
        out += bytes(a ^ b for a, b in zip(decrypted, previous, strict=True))
        previous = block
    return bytes(out)


def encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """PKCS#7-pad then CBC-encrypt — the vendor's `rawEncrypt`."""
    return cbc_encrypt(key, iv, pkcs7_pad(plaintext))


def decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """CBC-decrypt then strip PKCS#7 padding."""
    return pkcs7_unpad(cbc_decrypt(key, iv, ciphertext))
