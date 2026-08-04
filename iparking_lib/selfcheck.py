"""Startup self-check for the Homey Python runtime.

Mirrors `navien_lib/selfcheck.py`: every probe is read-only, and a failure is *reported*
rather than raised — a failing probe belongs in the log, not in the way of the app starting.
The point is that these four facts cannot be verified off-device, so they are established
once per start and left in the log where a support transcript will pick them up.

Two of the probes here are not routine, and both exist because of a specific way this app
can die silently.

**The CA bundle.** The Homey Python runtime ships **no system CA store**, so
`ssl.create_default_context()` there trusts nothing and every HTTPS request fails with
CERTIFICATE_VERIFY_FAILED. `certifi` (declared in `app.json`'s `pythonPackages`) supplies
one, and `iparking/tls.py` falls back to the empty default context rather than raising, so a
missing certifi does not stop the app — it just makes login the thing that breaks, which is
the whole app. This probe is what says *which* of the two happened.

**The certificate expiry.** `oauth.parkingcloud.co.kr` is the one host this app speaks TLS
to, and it is the one that carries the password. Its leaf certificate expires **2026-10-27**
(measured during recon, alongside the API host's, which expires the same day). `certifi` is
pinned in `pythonPackages` on a hub nobody updates routinely, so the ordinary outcome of a
CA rotation is that login simply stops working one day with a verify error and no hint as to
why. So this probe does a **real handshake** and reports the leaf's `notAfter`, warning when
it is within `WARN_WITHIN_DAYS` — turning a silent outage into a dated warning that shows up
weeks ahead of it, in a log the maintainer already reads.

The scheme probe reports no measurement at all, only policy — but it is the policy that is
most often misremembered (`oauth` is https, `members` is deliberately http because that
server 301s every https request down to cleartext), so having every session's log state it
verbatim is what stops the asymmetry being "fixed" from memory.
"""

from __future__ import annotations

import platform
import socket
import ssl
import sys
from datetime import UTC, datetime

from iparking_lib.const import MEMBERS_HOST, OAUTH_HOST, REQUIRED_SCHEMES, SCHEMES

#: How long before expiry the handshake probe starts warning. 30 days is chosen against the
#: update path rather than the certificate: a warning is only useful if it arrives while
#: there is still time to bump `certifi` and reinstall the app by hand, which is the only
#: remedy available on a self-installed app.
WARN_WITHIN_DAYS = 30

#: Budget for the handshake probe. This runs in `on_init`, so it is bounded well below
#: `transport.DEFAULT_TIMEOUT_S`: the probe is diagnostics, and a slow network must not
#: hold up the app's startup for as long as a real request is allowed to take.
HANDSHAKE_TIMEOUT_S = 8.0


def _probe(name, fn):
    try:
        return f"{name}: {fn()}"
    except Exception as exc:
        return f"{name}: FAILED ({type(exc).__name__}: {exc})"


def _interpreter():
    return f"{platform.python_version()} on {platform.machine()} ({sys.platform})"


def _ssl_context():
    from iparking_lib.iparking import tls

    cafile = tls.ca_file()
    ctx = tls.ssl_context()
    source = f"certifi ({cafile})" if cafile else "system default (no certifi!)"
    return f"TLS context ok (TLS {ctx.minimum_version.name}+), CA: {source}"


def _schemes():
    """The per-host transport policy, stated in full every start.

    Not a measurement — `client._require_scheme` is what asserts the *reached* scheme, on
    every response. This line exists so the asymmetry is on the record in its own words,
    because "surely both should be https" is the single most likely well-meant regression in
    this app and it would send the password to a host that answers a 301.
    """
    parts = []
    for host in (OAUTH_HOST, MEMBERS_HOST):
        required = REQUIRED_SCHEMES.get(host)
        note = f"required {required}" if required else "no floor, by policy"
        parts.append(f"{host} -> {SCHEMES[host]} ({note})")
    return "; ".join(parts)


def expiry_note(not_after: str, now: datetime | None = None) -> str:
    """`notAfter` from a peer certificate → a log line, warning when expiry is near.

    Pure, and split out from the handshake for exactly that reason: the warning threshold is
    the part of this probe with a bug worth catching (an off-by-one at the boundary, or a
    comparison that never fires), and it is the part that cannot be exercised by talking to
    a real server whose certificate is months from expiring.

    `notAfter` arrives in OpenSSL's own format (`"Oct 27 12:00:00 2026 GMT"`), so it is
    parsed with `ssl.cert_time_to_seconds` rather than a hand-written `strptime` — the
    field is always UTC regardless of the runtime's timezone.
    """
    reference = now or datetime.now(UTC)
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), UTC)
    days = (expires - reference).days
    stamp = f"leaf notAfter {expires.date().isoformat()} ({days}d)"
    if days < 0:
        return f"EXPIRED — {stamp}; login will fail until certifi is updated"
    if days <= WARN_WITHIN_DAYS:
        return (
            f"WARNING expiring soon — {stamp}; if login starts failing, "
            "bump certifi in app.json's pythonPackages and reinstall"
        )
    return stamp


def _handshake():
    """A real TLS handshake to the login host, reporting the negotiated version and expiry.

    Read-only: it connects, reads the certificate the server presents, and closes. No
    request is sent, so nothing is authenticated and no credential is involved — which is
    what makes it safe to run unconditionally at every app start.
    """
    from iparking_lib.iparking import tls

    context = tls.ssl_context()
    with socket.create_connection((OAUTH_HOST, 443), timeout=HANDSHAKE_TIMEOUT_S) as raw:
        with context.wrap_socket(raw, server_hostname=OAUTH_HOST) as sock:
            version = sock.version()
            cert = sock.getpeercert() or {}
    not_after = cert.get("notAfter")
    if not not_after:
        # Reachable only with a non-verifying context, which is what `tls.ssl_context()`
        # degrades to when certifi is missing: an unverified peer yields an empty cert dict.
        # Reported rather than raised, because the CA probe above has already named the
        # cause and a second traceback would only bury it.
        return f"{version} ok to {OAUTH_HOST}:443, but no peer certificate (verify disabled?)"
    return f"{version} ok to {OAUTH_HOST}:443, {expiry_note(not_after)}"


PROBES = (
    ("interpreter", _interpreter),
    ("tls-context", _ssl_context),
    ("transport-policy", _schemes),
    ("oauth-handshake", _handshake),
)


def run(log) -> None:
    """Run every probe, passing each result line to `log`."""
    log("--- runtime self-check ---")
    for name, fn in PROBES:
        log(_probe(name, fn))
    log("--- end self-check ---")
