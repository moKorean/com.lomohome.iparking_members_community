"""A TLS context that actually has CA certificates to verify against.

Copied from the sibling app `com.lomohome.navien` (`navien_lib/navien/tls.py`), because
the constraint is a property of the platform rather than of either app: the Homey Python
runtime ships **no system CA bundle**, so `ssl.create_default_context()` there trusts
nothing and every HTTPS call fails with CERTIFICATE_VERIFY_FAILED. `certifi` — declared
in `app.json`'s `pythonPackages` — supplies the bundle, and this builds the one context
the transport binds its `HTTPSHandler` to.

Only one host in this app speaks TLS, and it is the one that matters:
`oauth.parkingcloud.co.kr` carries the account password. (`members.iparking.co.kr` 301s
every HTTPS request down to cleartext, so it is spoken to over plain HTTP by policy —
see `transport.py` and `docs/RECON.md`.) Without certifi, login is the request that
breaks, which is the whole app.

Falls back to the plain default context if certifi somehow isn't present, so a failed
import can never take the app down — the caller just sees the same verify error it would
have had anyway, and `selfcheck.py` is what reports *which* of the two happened.
"""

from __future__ import annotations

import ssl


def ca_file() -> str | None:
    """certifi's CA bundle path, or None if certifi isn't importable.

    `selfcheck.py` reports this path so a support transcript names the CA source
    instead of leaving it to be guessed.
    """
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def ssl_context() -> ssl.SSLContext:
    """A verifying SSL context, preferring certifi's bundle over the (absent) system one."""
    cafile = ca_file()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()
