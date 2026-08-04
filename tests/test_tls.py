"""Tests for `iparking_lib.iparking.tls`.

The behaviour worth pinning is the *fallback*: certifi being unimportable must not raise
out of `ssl_context()`, because that would take the whole app down at import time over a
condition that only breaks one host. Certs on the oauth host expire 2026-10-27, so this
module is also the one `selfcheck.py` leans on.
"""

from __future__ import annotations

import os
import ssl
import sys

from iparking_lib.iparking import tls


def test_ca_file_points_at_a_real_bundle():
    path = tls.ca_file()
    assert path is not None, "certifi is a declared dependency; it must be importable"
    assert os.path.isfile(path)


def test_ssl_context_verifies():
    ctx = tls.ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # The point of the module is a context that *verifies*. A context that trusted
    # nothing, or checked no hostname, would still be an SSLContext.
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ssl_context_loads_certifis_bundle():
    """Distinguishes "used certifi" from "fell back to an empty default store"."""
    assert tls.ssl_context().cert_store_stats()["x509"] > 0


def test_ca_file_returns_none_when_certifi_is_broken(monkeypatch):
    """Exercises the real `except` path rather than stubbing `ca_file` itself."""
    monkeypatch.setitem(sys.modules, "certifi", None)
    assert tls.ca_file() is None


def test_ssl_context_falls_back_instead_of_raising(monkeypatch):
    """A bad certifi import must never be able to take the app down."""
    monkeypatch.setattr(tls, "ca_file", lambda: None)
    assert isinstance(tls.ssl_context(), ssl.SSLContext)
