"""One blocking HTTP transport, built so no redirect can silently change what we sent.

Everything here exists because of two measured facts about the vendor's servers
(`docs/RECON.md`): `oauth.parkingcloud.co.kr` serves HTTPS correctly, and
`members.iparking.co.kr` answers **every** HTTPS request — `/api/members/*` included —
with a 301 down to `http://`. The scheme is therefore chosen per host by policy (see
`const.py` / `client.py`) and never parsed from `operation_company[0].domain`, which
literally reads `"http://members.iparking.co.kr"`.

Given a server that 301s by default, the redirect policy is the load-bearing part.
`StrictRedirectHandler` enforces **two independent predicates**, and conflating them
into one is the mistake this docstring exists to prevent:

1. **Scheme.** Refuse `https -> http` (`InsecureRedirect`) so no code can ever believe
   it used TLS when it did not. `http -> https` is *allowed* and logged: refusing an
   upgrade would break the app the day the vendor fixes their TLS, and that is a change
   we want to survive rather than fail on.

2. **Body.** Refuse **any** followable 3xx on a request carrying a body
   (`BodyRedirect`), **same-scheme included.** This one is not obvious, so here is the
   reasoning, verified against CPython 3.14's
   `urllib.request.HTTPRedirectHandler.redirect_request`: it permits
   `code in (301, 302, 303)` with `m == "POST"`, then returns
   `Request(newurl, method="HEAD" if m == "HEAD" else "GET", headers=newheaders, ...)`
   — with **no `data=`** and with `CONTENT_HEADERS = ("content-length", "content-type")`
   filtered out of the headers. The retry is a **bodyless GET**. Every endpoint in this
   API except `DELETE` is a POST whose entire payload is an encrypted body, so a followed
   redirect would send nothing and get back a plausible-looking response instead of an
   error. That is strictly worse than failing, and it is the same reason the plan rejected
   an "https-first, fall back to http" design: the fallback would appear to work.

Also deliberate:

* **One** opener, built once in `__init__`. navien learned this the hard way — an opener
  built lazily inside the executor thread let two concurrent requests each build one, and
  state carried on the opener silently split in two.
* **`timeout=15` on every `open()`.** urllib's default is `None`, i.e. block forever, and
  a hung socket is what strands the register path (see `§3.5` of the plan: an
  `asyncio.wait_for` cancels the *await*, not the thread).
* **`final_url` is the post-redirect URL** (`resp.geturl()`), because the acceptance
  criterion asserts the scheme actually reached per host, not the one requested.
* 4xx/5xx bodies are **returned, not raised**: this API reports its verdict in a
  `result` field inside the body, and an error status can still carry that envelope.
  Only a request that never got an answer raises (`NetworkError`).

## The third measured fact: `members.iparking.co.kr` resets ~30 % of connections

Measured 2026-08-04 (`docs/RECON.md`): 20 identical read-only requests to the cleartext
host gave **14 answers and 6 dead sockets**. On macOS the fault surfaces as
`ConnectionResetError` (errno 54); on the Homey hub's runtime the *same* fault surfaces as
`http.client.IncompleteRead(255 bytes read)`. It is not header-dependent, not
dev-vs-installed, not a rate limit and not a block: `curl` interleaved with `urllib`
survives it only because `curl` retries internally. Retrying is the entire difference.

Two things follow, and both are load-bearing:

1. **A dead socket is its own error class** (`ConnectionLost`), *not* a timeout. A reset
   means the connection is gone and nothing is in flight; a timeout means the request may
   still be **in flight and may still land**. `TimeoutError` is an `OSError` subclass, so
   an errno test alone would happily call a timeout a reset — which is why
   `_is_connection_lost` tests for a timeout **first** and returns `False`. Conflating the
   two is what would make `POST /invitations` look retryable, and it is not.
2. **`IncompleteRead` is not an `OSError`.** It subclasses `http.client.HTTPException`, so
   before this module classified it, it escaped the `except` clause below entirely — past
   `client._attempt_register`'s handlers too — which is why a failed registration logged
   three request lines and then nothing at all. `http.client.HTTPException` is now caught.

**`attempts` defaults to 1.** Retrying is opt-in per call site, decided by *semantics and
never by HTTP method*: this API uses POST for reads, so "retry POSTs" would retry the
register. See `client.py` for which endpoints opt in and, more importantly, which one
never may.

This module must never import the `homey` SDK — see this package's `__init__.py` for why
that is phrased the long way round rather than shown as the import statement it forbids.
"""

from __future__ import annotations

import http.client
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from iparking_lib.iparking import tls

# urllib's default is None (block forever). One leg of every budget in the register path
# is derived from this number, so it is a named constant rather than a literal.
DEFAULT_TIMEOUT_S = 15.0

# --- retry shape -------------------------------------------------------------
#
# Sized against the *measured* fault, which fails fast: a reset arrives immediately, not
# after the 15 s timeout. Worst-case backoff totals are therefore what the register path's
# budgets in `const.py` are derived from — 3 backoffs (4 attempts) ≤ 3.15 s, 4 backoffs
# (5 attempts) ≤ 6.15 s. Jitter is real jitter, not decoration: several devices poll on the
# same hour and must not line their retries up.

RETRY_BACKOFF_BASE_S = 0.3
RETRY_BACKOFF_FACTOR = 2.0
RETRY_BACKOFF_MAX_S = 2.0

#: ± fraction applied to each backoff, so the worst case is `RETRY_BACKOFF_MAX_S * 1.5`.
RETRY_JITTER = 0.5


class TransportError(Exception):
    """Base for every failure this module raises."""


class NetworkError(TransportError):
    """The request never reached the server (DNS, TCP, TLS, timeout).

    Distinct from "the server answered with an error status", which is not an exception
    here at all — it comes back as a `Response` so the caller can read the envelope.
    """


class ConnectionLost(NetworkError):
    """The socket died mid-exchange. **Retryable**, and deliberately not a timeout.

    Raised for `ConnectionResetError` (macOS errno 54), `http.client.IncompleteRead` (how
    the hub's runtime reports the identical fault), `RemoteDisconnected`, a broken pipe, and
    the errnos that mean the peer tore the connection down instead of answering.

    The reason this is a separate class rather than a flag on `NetworkError`: it licenses a
    retry, and licensing a retry is only safe when **nothing is in flight**. A reset says
    the connection is gone, so the request either never arrived or was answered and the
    answer was lost with the socket — for a *read* both are resolved by asking again. A
    **timeout** says the opposite: the request may still be running and may still land, so
    it stays a plain `NetworkError` and no retry policy may key on it. Anyone widening this
    class to cover timeouts makes `POST /invitations` retryable by accident.
    """


class RedirectRefused(TransportError):
    """A redirect was refused by policy. Never followed, so nothing was re-sent."""


class InsecureRedirect(RedirectRefused):
    """Predicate 1: the server tried to move us from `https` down to `http`."""


class BodyRedirect(RedirectRefused):
    """Predicate 2: a 3xx arrived on a request carrying a body.

    Refused even when the scheme is unchanged, because urllib would retry it as a
    bodyless GET and this API's entire payload is the body.
    """


@dataclass(frozen=True)
class Response:
    """One HTTP answer.

    `final_url` is the URL that actually produced this response after any followed
    redirect, which is the only value that can honestly answer "did this request end up
    on http or https?".
    """

    status: int
    text: str
    final_url: str

    @property
    def final_scheme(self) -> str:
        return urllib.parse.urlsplit(self.final_url).scheme.lower()


def _scheme(url: str) -> str:
    return urllib.parse.urlsplit(url).scheme.lower()


# --- fault classification ----------------------------------------------------

#: The retryable set, as an explicit **allow-list of types**. Nothing is retried for
#: resembling a dead socket; it has to be one of these.
#:
#: An errno-based test was tried and deleted: every errno that means "the peer tore the
#: connection down" (`ECONNRESET`, `ECONNABORTED`, `EPIPE`, `ESHUTDOWN`) is already mapped by
#: CPython onto one of the types below, so the check could not classify anything this list
#: does not — removing it left all 59 relevant tests green. An unreachable guard is not a
#: guard, and here it was worse than useless: it was the branch that made the timeout
#: ordering in `_is_connection_lost` look like arithmetic on errnos rather than the
#: allow-list invariant it actually is.
_LOST_TYPES = (
    # macOS errno 54 / Linux 104. `http.client.RemoteDisconnected` subclasses this, and so
    # does anything CPython builds from `ECONNRESET`.
    ConnectionResetError,
    ConnectionAbortedError,
    # Also what CPython raises for `EPIPE` and `ESHUTDOWN`.
    BrokenPipeError,
    # The hub's report of the identical fault — `IncompleteRead(255 bytes read)`. Note this
    # is an `HTTPException`, **not** an `OSError`: it is why the fault used to escape
    # unclassified and unlogged.
    http.client.IncompleteRead,
)


def _is_connection_lost(exc: BaseException | None) -> bool:
    """Whether `exc` means the socket died mid-exchange — as opposed to timing out.

    The timeout test comes **first and wins**. No stdlib exception is both a `TimeoutError`
    and one of `_LOST_TYPES` — that was measured, not assumed — so on today's exceptions this
    line changes no answer. It is kept, and tested directly with a deliberately synthetic
    dual-nature exception, because it is the one line that keeps a *widened* allow-list from
    making writes retryable. Add `TimeoutError` (or `OSError`) to `_LOST_TYPES` and this
    ordering is what still returns `False`; delete both and `POST /invitations` starts
    retrying on a timeout, which registers a visitor vehicle twice at a real building.

    An ordering nobody exercises is an ordering nobody can trust, which is why
    `test_a_timeout_never_wins_the_race_against_the_retryable_types` constructs the shape the
    stdlib will not.

    urllib does not always let the socket error through directly — it wraps whatever it
    caught in `URLError.reason` — so the chain is unwrapped and re-tested. `reason` may be a
    plain string, which ends the walk.
    """
    while isinstance(exc, BaseException):
        if isinstance(exc, TimeoutError):
            return False
        if isinstance(exc, _LOST_TYPES):
            return True
        exc = exc.reason if isinstance(exc, urllib.error.URLError) else None
    return False


def _backoff_delay(attempt: int) -> float:
    """Seconds to wait after failed attempt number `attempt` (1-based).

    Exponential with a ceiling, then jittered by ±`RETRY_JITTER`. Clamped at zero so a
    future edit to the jitter fraction can never produce a negative sleep.
    """
    capped = min(
        RETRY_BACKOFF_BASE_S * RETRY_BACKOFF_FACTOR ** (attempt - 1), RETRY_BACKOFF_MAX_S
    )
    return max(0.0, capped * (1.0 + random.uniform(-RETRY_JITTER, RETRY_JITTER)))


class StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """`HTTPRedirectHandler` with the two predicates described in the module docstring.

    The predicates are checked *before* delegating, so `super()` only ever sees a
    redirect that has already been cleared: same-or-upgraded scheme, and no body to lose.
    Order matters — the scheme check runs first, so an `https -> http` 301 on a
    body-carrying request reports the downgrade (`InsecureRedirect`), which is the more
    specific diagnosis of the two.
    """

    def __init__(self, log=print) -> None:
        super().__init__()
        self._log = log

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = _scheme(req.full_url), _scheme(newurl)

        # Predicate 1 — scheme. Refuse the downgrade; allow and record the upgrade.
        if old == "https" and new == "http":
            raise InsecureRedirect(
                f"{code} redirect from https to http refused "
                f"({urllib.parse.urlsplit(req.full_url).netloc} -> "
                f"{urllib.parse.urlsplit(newurl).netloc}); not following"
            )

        # Predicate 2 — body. Independent of the scheme: urllib would re-send this as a
        # bodyless GET (verified in CPython 3.14; see the module docstring), and against
        # this API that yields plausible garbage rather than an error.
        if req.data is not None:
            raise BodyRedirect(
                f"{code} redirect refused on a {req.get_method()} carrying a body "
                f"(urllib would retry it as a bodyless GET); not following"
            )

        if old == "http" and new == "https":
            # Not an error: the vendor upgrading their TLS must not break the app. Logged
            # because it means the per-host scheme policy in const.py is now out of date.
            self._log(
                f"iparking http: {code} upgrade http -> https "
                f"{urllib.parse.urlsplit(newurl).netloc}; following"
            )

        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Transport:
    """A single urllib opener with the strict redirect policy and a real timeout.

    `handlers` is a **test seam**, not configuration. Passing a handler that subclasses
    `HTTPHandler`/`HTTPSHandler` makes `build_opener` substitute it for the default of
    the same kind, which is how the redirect tests stub responses without touching a real
    host. It replaces the socket, not the policy: `StrictRedirectHandler` is installed
    either way, so a stubbed 301 still exercises the code that refuses it.

    `sleep` is the second test seam, for the same reason: it lets the retry tests assert the
    backoff *happened* and how long it asked for, without the suite really waiting.
    """

    def __init__(self, *, log=print, handlers=None, sleep=time.sleep) -> None:
        # Built once, here, rather than lazily inside the executor thread: two requests
        # starting together would otherwise each build their own.
        self._log = log
        self._sleep = sleep
        self._opener = urllib.request.build_opener(
            StrictRedirectHandler(log),
            urllib.request.HTTPSHandler(context=tls.ssl_context()),
            *(handlers or ()),
        )

    def request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        body: bytes | str | None = None,
        attempts: int = 1,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> Response:
        """Issue a request, retrying **only** a dead socket, at most `attempts` times.

        Returns a `Response` for any answer the server gave, **including 4xx/5xx**, whose
        bodies this API uses to carry its `result` code. Raises `NetworkError` when there
        was no answer at all, and `RedirectRefused` when a redirect was refused by policy.

        `attempts` defaults to **1**, i.e. no retry. The default is the safe one on purpose:
        a call site that wants a retry has to say so, so nothing becomes retryable by
        inheriting a default. And the only fault that is ever retried is `ConnectionLost` —
        never a timeout (the request may still be in flight), never a refused redirect
        (nothing about re-sending would change the server's mind), and never an error
        *status*, which is an answer and belongs to the caller.

        Deciding `attempts` is the **caller's** job because it is a question about the
        endpoint's semantics, not about the transport: this API serves reads over POST, so
        no rule expressible here could separate `POST /invitations/list` from
        `POST /invitations`. See `client.py`.
        """
        if attempts < 1:
            raise ValueError(f"attempts must be >= 1, got {attempts}")

        for attempt in range(1, attempts + 1):
            try:
                return self._request_once(method, url, headers, body, timeout)
            except ConnectionLost as exc:
                # `exc` is logged by type, never by value beyond the message the socket layer
                # produced — it carries no plate, token or body. The give-up line exists
                # because its absence is a defect in its own right: a registration once
                # logged three request lines and then nothing at all, leaving no way to see
                # why it failed.
                if attempt == attempts:
                    self._log(
                        f"iparking http: {method} {url} -> gave up after {attempt} "
                        f"attempt(s): {type(exc).__name__}"
                    )
                    raise
                delay = _backoff_delay(attempt)
                self._log(
                    f"iparking http: {method} {url} -> {type(exc).__name__} "
                    f"on attempt {attempt}/{attempts}; retrying in {delay:.2f}s"
                )
                self._sleep(delay)

        # Unreachable: the loop either returns, raises, or sleeps and goes round again.
        raise AssertionError("retry loop fell through")

    def _request_once(
        self,
        method: str,
        url: str,
        headers: dict | None,
        body: bytes | str | None,
        timeout: float,
    ) -> Response:
        """One attempt. Every policy check lives here, so a retry re-runs all of them."""
        if isinstance(body, str):
            body = body.encode("utf-8")

        # Never the body, and never a header value: the request body is the encrypted
        # payload and `authorization` is a live 7-day credential (see plan §8, "never
        # logged"). Method and URL only.
        self._log(f"iparking http: {method} {url}")
        try:
            req = urllib.request.Request(url, data=body, method=method)
            for key, value in (headers or {}).items():
                req.add_header(key, value)
            # timeout is always passed; urllib's default of None blocks forever.
            with self._opener.open(req, timeout=timeout) as resp:
                response = Response(
                    status=resp.status, text=resp.read().decode("utf-8", "replace"),
                    final_url=resp.geturl(),
                )
        except urllib.error.HTTPError as exc:
            # Must stay above the URLError clause: HTTPError subclasses it, so swapping
            # the two would reclassify every 4xx/5xx as a network failure and discard the
            # envelope inside the body.
            self._log(f"iparking http: {method} {url} -> HTTPError {exc.code}")
            response = Response(
                status=exc.code, text=exc.read().decode("utf-8", "replace"),
                final_url=getattr(exc, "url", None) or url,
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            ssl.SSLError,
            OSError,
            # `http.client.HTTPException` is **not** an `OSError`, and its absence here is
            # the logging defect: `IncompleteRead` — the hub's report of the members host's
            # reset — escaped this clause entirely, so a failed registration produced no
            # error line and no traceback anywhere the maintainer could read one.
            http.client.HTTPException,
        ) as exc:
            lost = _is_connection_lost(exc)
            self._log(
                f"iparking http: {method} {url} -> "
                f"{'connection lost' if lost else 'network error'} "
                f"({type(exc).__name__}) {exc}"
            )
            if lost:
                # Retryable, and only because the socket is *gone*: nothing is still in
                # flight. A timeout takes the branch below instead, on purpose.
                raise ConnectionLost(str(exc)) from exc
            raise NetworkError(str(exc)) from exc

        # Predicate 2, completed. `redirect_request` covers the *followable* codes; a 3xx
        # that urllib will not follow (300, 304, 305, 306, or one with no Location) never
        # reaches it and arrives here as an HTTPError instead. Refusing it here too makes
        # "any 3xx on a body-carrying request is refused" literally true rather than
        # true-for-the-common-cases.
        if body is not None and 300 <= response.status < 400:
            raise BodyRedirect(
                f"{response.status} refused on a {method} carrying a body "
                f"(unfollowable 3xx); not retrying"
            )

        self._log(f"iparking http: {method} {url} -> {response.status}")
        return response
