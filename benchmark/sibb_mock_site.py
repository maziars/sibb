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

        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._submissions: List[Dict[str, Any]] = []
        self._visits: List[Dict[str, Any]] = []
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

    def submissions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(s) for s in self._submissions]

    def last_submission(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._submissions[-1]) if self._submissions else None

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
            self._sessions.clear()

    # ─────────────────────────── handler factory ──────────────────────

    def _make_handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            # Silence the default per-request stderr logging — we
            # already capture submissions structurally.
            def log_message(self, *a, **kw):  # noqa: D401
                return

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
                path = self.path.split("?", 1)[0]
                if path == site.sign_in_path:
                    self._handle_credential_post(create=False)
                elif path == site.sign_up_path:
                    self._handle_credential_post(create=True)
                else:
                    self.send_error(404)

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
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                fields = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
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
                        "username": username,
                        "password": password,
                        "success": success,
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
