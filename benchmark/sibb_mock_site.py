#!/usr/bin/env python3
"""
Mock website fixture for Safari password autofill testing.
==========================================================

Spins up a host-side HTTP server that the iOS simulator's Safari
can reach. Exposes login + signup forms and records every POST
submission in plaintext, so the verifier can match against
expected credentials *without ever needing to decrypt the
keychain*.

Why this exists
---------------
The keychain encrypts password values (`SFAuthenticatedCiphertext`).
We can verify username existence via SHA-1 hash equality against
the `acct`/`srvr` columns (see `IOS_SIM_QUIRKS.md` §13), but the
password value itself is unrecoverable from inside the keychain.

By having the agent autofill into a controlled website, the password
arrives at *our* server in plaintext on form submission. The mock
site is the source of truth for password verification — it's also
where we can verify the *session/effect* (did the credentials
actually let the user reach a protected page).

Lifecycle
---------
    fixture = MockSite(site_id="ep-42",
                       credentials={"alice": "hunter2"})
    fixture.start()                # binds to 127.0.0.1:<random>
    open_in_safari(udid, fixture.login_url)
    # ... agent does its thing ...
    assert fixture.has_successful_login("alice", "hunter2")
    fixture.stop()

Verification surfaces
---------------------
- `submissions()` → list of dicts with mode/username/password/success/timestamp
- `has_successful_login(user, pass)` → bool
- `mock_site.submissions` resource fetcher (in `sibb_verify`) for
  declarative task verification

Routes
------
- GET  /login      sign-in form (autocomplete="current-password")
- POST /login      validates against `credentials`, records, redirects
                   to /dashboard on success
- GET  /signup     create-account form (autocomplete="new-password",
                   which triggers iOS "Suggest Strong Password")
- POST /signup     adds the (user, pass) pair to `credentials`,
                   records, redirects to /dashboard
- GET  /dashboard  200 if a valid session cookie is presented;
                   401 otherwise (the "did the login actually work
                   from Safari's POV" surface)
- GET  /status     JSON dump of state — handy for debugging from
                   a terminal during development
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


# ─────────────────────────── module-level registry ────────────────────
#
# The verifier needs to read submissions from a running fixture without
# the fixture being threaded through every call site. A small process-
# local registry keyed by `site_id` solves this: `MockSite.start()`
# inserts itself; `stop()` removes itself; `get_site(site_id)` returns
# the live instance or None.
#
# Duplicate `site_id` is a hard error at `start()` — leaks across
# episodes would silently route submissions to the wrong fixture.

_REGISTRY: Dict[str, "MockSite"] = {}


def get_site(site_id: str = "default") -> Optional["MockSite"]:
    return _REGISTRY.get(site_id)


def list_sites() -> List[str]:
    return list(_REGISTRY)


# ─────────────────────────── MockSite ─────────────────────────────────


class MockSite:
    """A controllable login site for end-to-end credential testing.

    Each instance binds to 127.0.0.1 on an OS-chosen free port.
    Configure the accepted credentials at construction time (or
    via `add_credentials` / signup-form posts).

    Thread-safety: all mutable state (`_submissions`, `_sessions`,
    `credentials`) is guarded by `_lock`. The HTTP server runs on
    a background thread; the test harness reads via the public
    methods on its own thread.
    """

    def __init__(
        self,
        *,
        site_id: str = "default",
        credentials: Optional[Dict[str, str]] = None,
        sign_in_path: str = "/login",
        sign_up_path: str = "/signup",
        dashboard_path: str = "/dashboard",
        status_path: str = "/status",
        static_pages: Optional[Dict[str, Any]] = None,
    ):
        self.site_id = site_id
        # Bind explicitly to 127.0.0.1 rather than "localhost" to
        # avoid IPv6 vs IPv4 resolution surprises on macOS.
        self.host: str = "127.0.0.1"
        self.port: Optional[int] = None
        self.credentials: Dict[str, str] = dict(credentials or {})
        self.sign_in_path = sign_in_path
        self.sign_up_path = sign_up_path
        self.dashboard_path = dashboard_path
        self.status_path = status_path
        # Static-page templates for Phase 4+ harness generators
        # (event detail, recipe, business card, multi-step checkout
        # etc.). Each value is either:
        #   - a `str` of fully-rendered HTML (served verbatim), or
        #   - a `Callable[[random.Random], str]` invoked PER REQUEST
        #     with a per-page-seeded RNG, so layouts can randomize
        #     (button position, distractor buttons, page length,
        #     form-field order) without breaking determinism. The
        #     page seed derives from MockSite.page_seed XOR the
        #     hash of the path, so the same path always returns the
        #     same rendered HTML within an episode.
        #
        # POSTs to ANY path land in `_submissions` (no need to declare
        # form-target paths up front). GETs to static-page paths
        # land in `_visits` the same way signin/signup do.
        self.static_pages: Dict[str, Any] = dict(static_pages or {})
        # Per-episode page seed (set at construction time so seeded
        # pages are stable for the duration of the episode but
        # vary across episodes / generator seeds).
        self.page_seed: int = 0

        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._submissions: List[Dict[str, Any]] = []
        self._visits: List[Dict[str, Any]] = []
        # Full HTTP request log — every GET and POST regardless of
        # status code. Used by episode debugging: when a verifier
        # fails because the agent's submission didn't land, this log
        # tells us whether the agent's tap actually fired a POST or
        # whether iOS routed the tap somewhere else.
        self._request_log: List[Dict[str, Any]] = []
        self._sessions: set = set()
        self._lock = threading.Lock()

    # ─────────────────────────── lifecycle ────────────────────────────

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError(
                f"MockSite[{self.site_id!r}] already started "
                f"on port {self.port}; call stop() first")
        if self.site_id in _REGISTRY:
            raise RuntimeError(
                f"MockSite site_id {self.site_id!r} is already registered. "
                f"Stop the previous instance or pick a different id.")

        self._server = ThreadingHTTPServer(
            (self.host, 0), self._make_handler())
        self.port = self._server.server_port
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"MockSite[{self.site_id}]")
        self._thread.start()
        _REGISTRY[self.site_id] = self

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None
        _REGISTRY.pop(self.site_id, None)

    # ─────────────────────────── inspection ───────────────────────────

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError(
                f"MockSite[{self.site_id!r}] not started; call start() first")
        return f"http://{self.host}:{self.port}"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}{self.sign_in_path}"

    @property
    def signup_url(self) -> str:
        return f"{self.base_url}{self.sign_up_path}"

    def submissions(self, *,
                     include_decoys: bool = False
                     ) -> List[Dict[str, Any]]:
        """Return all recorded form submissions.

        By default DECOY submissions (clicks on distractor buttons
        from `harness_layout.distractor_buttons`) are filtered out
        so verifiers asserting "the agent submitted exactly one form"
        aren't fooled by a stray test-tap on a decoy. Pass
        `include_decoys=True` to see them.
        """
        with self._lock:
            rows = [dict(s) for s in self._submissions]
        if not include_decoys:
            rows = [s for s in rows if not s.get("is_decoy")]
        return rows

    def last_submission(self, *,
                         include_decoys: bool = False
                         ) -> Optional[Dict[str, Any]]:
        rows = self.submissions(include_decoys=include_decoys)
        return rows[-1] if rows else None

    def request_log(self) -> List[Dict[str, Any]]:
        """Full HTTP request log. Each entry has `method`, `path`,
        `query`, `content_length`, `content_type`, `response_code`,
        `epoch`, `user_agent`. Use for episode debugging — answers
        "did the agent's tap actually fire a POST?"."""
        with self._lock:
            return [dict(r) for r in self._request_log]

    def visits(self) -> List[Dict[str, Any]]:
        """List every GET that hit this site. Each entry has `path`,
        `query`, `epoch` (unix seconds), `user_agent`. Used by the
        `mock_site.visited` verifier to assert "the agent navigated
        Safari to this URL" — the keystone for reverse-direction
        generators (Calendar.url → Safari, Reminders.url → Safari,
        etc.)."""
        with self._lock:
            return [dict(v) for v in self._visits]

    def has_successful_login(self, username: str, password: str) -> bool:
        return any(
            s["username"] == username
            and s["password"] == password
            and s["success"]
            for s in self.submissions()
        )

    # ─────────────────────────── mutation ─────────────────────────────

    def add_credentials(self, username: str, password: str) -> None:
        with self._lock:
            self.credentials[username] = password

    def reset(self) -> None:
        with self._lock:
            self._submissions.clear()
            self._visits.clear()
            self._request_log.clear()
            self._sessions.clear()

    # ─────────────────────────── handler factory ──────────────────────

    def _make_handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            # Silence the default per-request stderr logging — we
            # already capture submissions structurally.
            def log_message(self, *a, **kw):  # noqa: D401
                return

            def log_request(self, code="-", size="-"):
                """Called by BaseHTTPRequestHandler after every
                completed request. We hijack this to append to the
                full request log — captures method/path/code uniformly
                for GETs and POSTs without touching each handler.
                """
                path_and_query = getattr(self, "path", "")
                path = path_and_query.split("?", 1)[0]
                query = (path_and_query.split("?", 1)[1]
                          if "?" in path_and_query else "")
                try:
                    code_int = int(code)
                except (TypeError, ValueError):
                    code_int = 0
                # `self.headers` is None until the request line + header
                # block parse cleanly. `send_error` on a malformed
                # request can call `log_request` before that — guard
                # with a getattr.
                headers = getattr(self, "headers", None)

                def _hdr(name: str) -> str:
                    if headers is None:
                        return ""
                    try:
                        return headers.get(name) or ""
                    except Exception:
                        return ""
                with site._lock:
                    site._request_log.append({
                        "method": getattr(self, "command", ""),
                        "path": path,
                        "query": query,
                        "content_length": _hdr("Content-Length"),
                        "content_type": _hdr("Content-Type"),
                        "response_code": code_int,
                        "user_agent": _hdr("User-Agent"),
                        "epoch": time.time(),
                    })

            def do_GET(self):
                path_and_query = self.path
                path = path_and_query.split("?", 1)[0]
                query = (path_and_query.split("?", 1)[1]
                          if "?" in path_and_query else "")
                # Record the visit BEFORE dispatching so 404s also count
                # — agent intent to navigate matters, not response code.
                # Skip the status endpoint to avoid self-poll noise.
                if path != site.status_path:
                    with site._lock:
                        site._visits.append({
                            "path": path,
                            "query": query,
                            "epoch": time.time(),
                            "user_agent": self.headers.get(
                                "User-Agent", ""),
                        })
                # Static-page templates take precedence over the
                # built-in routes (so a generator can override `/` with
                # a custom landing page if needed).
                if path in site.static_pages:
                    self._serve_static_page(path, query)
                    return
                # Step 5M (2026-06-08) — prefix-routed static_pages.
                # If a registered key ends in "/", treat it as a prefix
                # that matches any path starting with it (e.g. key
                # "/product/" matches "/product/wm-1546"). The TEMPLATE
                # gets the full request path via the `path` kwarg so it
                # can dispatch on the tail. Used by shop generators to
                # serve N product detail pages from one template.
                #
                # Exclude bare "/" — every absolute path starts with it,
                # so using it as a prefix would catch all unmatched
                # requests. The root is reserved for exact matching.
                # Longest-prefix-wins (so `/account/cards/` beats
                # `/account/`) to keep nested routes deterministic.
                prefix_match = None
                for key in site.static_pages:
                    if key == "/" or not key.endswith("/"):
                        continue
                    if path.startswith(key) and (
                            prefix_match is None
                            or len(key) > len(prefix_match)):
                        prefix_match = key
                if prefix_match is not None:
                    self._serve_static_page(
                        prefix_match, query, full_path=path)
                    return
                if path == site.sign_in_path or path == "/":
                    self._serve_form(mode="signin")
                elif path == site.sign_up_path:
                    self._serve_form(mode="signup")
                elif path == site.dashboard_path:
                    self._serve_dashboard()
                elif path == site.status_path:
                    self._serve_status()
                else:
                    self.send_error(404)

            def do_POST(self):
                path_and_query = self.path
                path = path_and_query.split("?", 1)[0]
                if path == site.sign_in_path:
                    self._handle_credential_post(create=False)
                elif path == site.sign_up_path:
                    self._handle_credential_post(create=True)
                else:
                    # Capture POSTs to any other path as a generic
                    # submission. Generators can submit forms to
                    # arbitrary endpoints (`/rsvp`, `/buy`, etc.)
                    # without declaring them up front; the verifier
                    # reads them via `mock_site.submissions` with
                    # `path` in the selector.
                    self._handle_generic_post(path)

            # ─────────────── responses ───────────────

            def _serve_form(self, mode: str) -> None:
                # `autocomplete` attrs are how iOS Password AutoFill
                # decides which UX to surface in the keyboard accessory:
                #   "username" + "current-password" → autofill from saved
                #   "username" + "new-password"     → "Suggest Strong Password"
                pw_autocomplete = (
                    "new-password" if mode == "signup" else "current-password"
                )
                title = "Create Account" if mode == "signup" else "Sign In"
                action = (
                    site.sign_up_path if mode == "signup" else site.sign_in_path
                )
                html = (
                    "<!DOCTYPE html>\n"
                    "<html lang=\"en\">\n"
                    "<head>\n"
                    "<meta charset=\"utf-8\">\n"
                    "<meta name=\"viewport\" "
                    "content=\"width=device-width, initial-scale=1\">\n"
                    f"<title>{title} — SIBB Test Site</title>\n"
                    "</head>\n"
                    "<body>\n"
                    f"<h1>{title}</h1>\n"
                    f"<form method=\"POST\" action=\"{action}\">\n"
                    "<p><input type=\"text\" name=\"username\" "
                    "autocomplete=\"username\" "
                    "placeholder=\"Username\" required></p>\n"
                    "<p><input type=\"password\" name=\"password\" "
                    f"autocomplete=\"{pw_autocomplete}\" "
                    "placeholder=\"Password\" required></p>\n"
                    f"<p><button type=\"submit\">{title}</button></p>\n"
                    "</form>\n"
                    "</body>\n"
                    "</html>\n"
                )
                self._send_html(200, html)

            def _handle_credential_post(self, create: bool) -> None:
                MAX_BODY = 1 << 20
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                length = max(0, min(length, MAX_BODY))
                raw = self.rfile.read(length) if length > 0 else b""
                fields = urllib.parse.parse_qs(
                    raw.decode("utf-8", "replace"),
                    keep_blank_values=True)
                username = (fields.get("username") or [""])[0]
                password = (fields.get("password") or [""])[0]

                session_token: Optional[str] = None
                with site._lock:
                    if create:
                        # Signup auto-accepts any non-empty pair and
                        # registers it for subsequent sign-ins.
                        success = bool(username) and bool(password)
                        if success:
                            site.credentials[username] = password
                    else:
                        success = (
                            bool(password)
                            and site.credentials.get(username) == password
                        )

                    if success:
                        session_token = uuid.uuid4().hex
                        site._sessions.add(session_token)

                    site._submissions.append({
                        "mode": "signup" if create else "signin",
                        "path": (site.sign_up_path if create
                                  else site.sign_in_path),
                        "username": username,
                        "password": password,
                        "success": success,
                        "is_decoy": False,
                        "timestamp": time.time(),
                    })

                if success:
                    self.send_response(302)
                    self.send_header("Location", site.dashboard_path)
                    self.send_header(
                        "Set-Cookie",
                        f"session={session_token}; Path=/; HttpOnly")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send_html(
                        401,
                        "<!DOCTYPE html>\n<html><body>\n"
                        "<h1>Invalid credentials</h1>\n"
                        f"<p><a href=\"{site.sign_in_path}\">Try again</a></p>\n"
                        "</body></html>\n")

            def _serve_dashboard(self) -> None:
                # The cookie check is the "session/effect" verification
                # surface: a successful login deposits a token in
                # `_sessions`; presenting it back proves the user
                # actually got past the sign-in form (not just typed
                # the right values).
                cookie_header = self.headers.get("Cookie") or ""
                token: Optional[str] = None
                for pair in cookie_header.split(";"):
                    name, _, value = pair.strip().partition("=")
                    if name == "session":
                        token = value
                        break
                with site._lock:
                    authenticated = (
                        token is not None and token in site._sessions
                    )
                if authenticated:
                    self._send_html(
                        200,
                        "<!DOCTYPE html>\n<html><body>\n"
                        "<h1>Welcome!</h1>\n"
                        "<p>You are signed in.</p>\n"
                        "</body></html>\n")
                else:
                    self._send_html(
                        401,
                        "<!DOCTYPE html>\n<html><body>\n"
                        "<h1>Not signed in</h1>\n"
                        f"<p><a href=\"{site.sign_in_path}\">Sign in</a></p>\n"
                        "</body></html>\n")

            def _serve_status(self) -> None:
                with site._lock:
                    payload = {
                        "site_id": site.site_id,
                        "base_url": site.base_url,
                        "credentials_configured": sorted(site.credentials),
                        "submission_count": len(site._submissions),
                        "submissions": [dict(s) for s in site._submissions],
                        "active_session_count": len(site._sessions),
                    }
                body = json.dumps(payload, sort_keys=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_static_page(self, registered_path: str,
                                    query: str = "",
                                    *,
                                    full_path: Optional[str] = None) -> None:
                """Render a static-page template and serve it.

                Templates are either strings (verbatim HTML) or
                callables that take a per-path `random.Random` and
                return HTML. The per-path RNG is seeded deterministically
                so the same path always renders the same layout within
                an episode (replayable), but different paths (or a
                different MockSite.page_seed across episodes) produce
                different layouts.

                Step 5M (2026-06-08) — templates may OPT IN to extra
                request context by declaring `path` and/or `query`
                keyword parameters. The MockSite uses `inspect` to
                pass them only if the signature accepts them, so
                pre-existing single-arg templates (`rsvp_event` etc.)
                are unaffected. New shop templates use this to serve
                N product detail pages from one prefix-routed template
                (key `/product/` matches `/product/<sku_id>`; the
                template reads the tail from the `path` kwarg).
                """
                tpl = site.static_pages.get(registered_path)
                if tpl is None:
                    self.send_error(404)
                    return
                # Effective path for RNG seeding + the kwarg passed to
                # the template — the full request path (so each
                # `/product/<sku_id>` gets its own RNG and the template
                # can extract <sku_id>) rather than the registered key.
                effective_path = full_path or registered_path
                import html as _html
                if callable(tpl):
                    import random as _random
                    import inspect as _inspect
                    # Stable per-path seed: episode seed XOR a stable
                    # 32-bit digest of the path. Each path gets its
                    # own RNG. The same helper is exposed publicly so
                    # generators can re-derive the SAME RNG to know
                    # what choices the template will make.
                    from harness_layout import compute_path_seed
                    path_seed = compute_path_seed(
                        site.page_seed, effective_path)
                    rng = _random.Random(path_seed)
                    # Opt-in: pass `path` / `query` kwargs only when
                    # the template's signature names them. Keeps the
                    # existing `(rng)`-only convention working.
                    try:
                        sig = _inspect.signature(tpl)
                        extra = {}
                        if "path" in sig.parameters:
                            extra["path"] = effective_path
                        if "query" in sig.parameters:
                            extra["query"] = query
                        if "page_seed" in sig.parameters:
                            # Step 5O (2026-06-09) — templates needing
                            # cross-path task-level state (e.g. V4 shop
                            # /product/, /checkout, /account/cards all
                            # branching on the same `use_saved_cards`
                            # bool) opt in to the episode's page_seed
                            # and derive the helper's RNG from a fixed
                            # reference path.
                            extra["page_seed"] = site.page_seed
                    except (TypeError, ValueError):
                        extra = {}
                    try:
                        html = tpl(rng, **extra)
                    except Exception as e:
                        self._send_html(
                            500,
                            f"<!DOCTYPE html><html><body>\n"
                            f"<h1>Template error</h1>\n"
                            f"<pre>{_html.escape(repr(e))}</pre>"
                            f"</body></html>\n")
                        return
                    # Templates MUST return a str. Coroutines, None,
                    # bytes, and other types are author bugs — surface
                    # them as a 500 with a clear message so generators
                    # get an actionable signal instead of an opaque
                    # `bytes has no attribute encode` later.
                    if not isinstance(html, str):
                        type_name = type(html).__name__
                        self._send_html(
                            500,
                            f"<!DOCTYPE html><html><body>\n"
                            f"<h1>Template type error</h1>\n"
                            f"<p>Template for "
                            f"<code>{_html.escape(effective_path)}</code> "
                            f"returned a "
                            f"<code>{_html.escape(type_name)}</code>; "
                            f"expected <code>str</code>.</p>\n"
                            f"</body></html>\n")
                        return
                else:
                    html = str(tpl)
                self._send_html(200, html)

            def _handle_generic_post(self, path: str) -> None:
                """Capture a POST to a non-credential path. Form fields
                land in `_submissions` under a path-discriminated entry
                so verifiers can scope by both `mode` (=path) AND
                fields. Returns a small acknowledgement page so the
                agent's UI flow has somewhere to land.

                Generators can use this for `/rsvp`, `/buy`, `/contact`
                etc. without declaring those paths up front. The
                verifier reads via `mock_site.submissions` with
                `mode=<path>` in the selector.
                """
                # Cap body size at 1 MiB. A malformed or adversarial
                # Content-Length larger than this is silently truncated
                # — prevents `read()` from hanging on the wire forever
                # waiting for bytes that never come, and bounds memory
                # for any one request.
                MAX_BODY = 1 << 20
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                length = max(0, min(length, MAX_BODY))
                raw = self.rfile.read(length) if length > 0 else b""
                content_type = (
                    self.headers.get("Content-Type") or "").lower()
                parse_error: Optional[str] = None
                if content_type.startswith("multipart/"):
                    # We don't parse multipart bodies. Record the marker
                    # so verifier authors don't false-pass on "no fields
                    # submitted" when the form was actually a multipart
                    # upload.
                    fields_qs = {}
                    parse_error = "multipart_unsupported"
                else:
                    # keep_blank_values=True so a deliberately-empty
                    # field (`email=`) gets recorded as `email=""` —
                    # otherwise verifiers can't distinguish "agent
                    # didn't fill the field" from "field absent from
                    # form".
                    fields_qs = urllib.parse.parse_qs(
                        raw.decode("utf-8", "replace"),
                        keep_blank_values=True)
                # parse_qs returns each value as a list; flatten to the
                # first value (the typical case for HTML forms) but
                # keep the list-form in `fields_raw` for radio/checkbox
                # groups where multiple values are legitimate.
                fields_flat = {
                    k: (v[0] if len(v) == 1 else v)
                    for k, v in fields_qs.items()
                }
                # Lazy-import so test fixtures don't pay the cost.
                try:
                    from harness_layout import DECOY_PATH as _DECOY
                except Exception:
                    _DECOY = "/__sibb_decoy__"
                is_decoy = (path == _DECOY)
                entry: Dict[str, Any] = {
                    # `mode` is the legacy key carrying signin/signup
                    # for credential submissions; for static-page POSTs
                    # we put the path here too (same value as `path`)
                    # for backwards-compat. New code should select on
                    # `path` directly.
                    "mode": path,
                    "path": path,
                    "fields": fields_flat,
                    "fields_raw": fields_qs,
                    "is_decoy": is_decoy,
                    "timestamp": time.time(),
                }
                if parse_error:
                    entry["parse_error"] = parse_error
                    entry["content_type"] = content_type
                with site._lock:
                    site._submissions.append(entry)
                import html as _html
                # Echo back what the server received so the agent can
                # see concrete evidence of which field values landed —
                # turns a vague "did it work?" into "yes, here's what
                # you submitted". `role="status"` marks the confirmation
                # as a live region so it surfaces clearly in the iOS
                # AX tree (Safari maps `role="status"` to an
                # `XCUIElementTypeStaticText` with the live-region
                # role hint).
                rows = []
                for k, v in fields_flat.items():
                    if isinstance(v, list):
                        v_str = ", ".join(str(x) for x in v)
                    else:
                        v_str = str(v)
                    rows.append(
                        f"  <dt>{_html.escape(k)}</dt>"
                        f"<dd>{_html.escape(v_str) or '(empty)'}</dd>")
                rows_html = "\n".join(rows) if rows else (
                    "  <dt>(no fields)</dt><dd>—</dd>")
                self._send_html(
                    200,
                    "<!DOCTYPE html>\n<html><head>"
                    "<title>Submission received</title>"
                    "</head>\n<body>\n"
                    "<main aria-label=\"Submission confirmation\">\n"
                    "<h1 role=\"status\">Submission received</h1>\n"
                    "<p>Your form was submitted successfully. "
                    "The server recorded these values:</p>\n"
                    f"<dl>\n{rows_html}\n</dl>\n"
                    "</main>\n</body></html>\n")

            # ─────────────── helpers ───────────────

            def _send_html(self, status: int, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


# ─────────────────────── simulator integration ─────────────────────────


def open_in_safari(udid: str, url: str, *, timeout: float = 10.0) -> None:
    """Open `url` in mobile Safari on the given simulator UDID.

    Backed by `simctl openurl`, which launches Safari (or brings it
    foreground) and navigates to the URL. Call after `MockSite.start()`
    to seed Safari with the login form ready for the agent.

    On simctl failure (e.g. device not booted, openurl rejected by
    iOS) we re-raise with stderr included in the message — opaque
    exit codes like 149 are otherwise undebuggable.
    """
    result = subprocess.run(
        ["xcrun", "simctl", "openurl", udid, url],
        timeout=timeout, capture_output=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"simctl openurl {udid} {url} failed "
            f"(exit {result.returncode}): {stderr or '<no stderr>'}")
