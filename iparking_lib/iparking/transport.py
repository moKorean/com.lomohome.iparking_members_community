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

This module must never import the `homey` SDK — see this package's `__init__.py` for why
that is phrased the long way round rather than shown as the import statement it forbids.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from iparking_lib.iparking import tls

# urllib's default is None (block forever). One leg of every budget in the register path
# is derived from this number, so it is a named constant rather than a literal.
DEFAULT_TIMEOUT_S = 15.0


class TransportError(Exception):
    """Base for every failure this module raises."""


class NetworkError(TransportError):
    """The request never reached the server (DNS, TCP, TLS, timeout).

    Distinct from "the server answered with an error status", which is not an exception
    here at all — it comes back as a `Response` so the caller can read the envelope.
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
    """

    def __init__(self, *, log=print, handlers=None) -> None:
        # Built once, here, rather than lazily inside the executor thread: two requests
        # starting together would otherwise each build their own.
        self._log = log
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
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> Response:
        """Issue one blocking request.

        Returns a `Response` for any answer the server gave, **including 4xx/5xx**, whose
        bodies this API uses to carry its `result` code. Raises `NetworkError` when there
        was no answer at all, and `RedirectRefused` when a redirect was refused by policy.
        """
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
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            self._log(f"iparking http: {method} {url} -> network error {exc}")
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
