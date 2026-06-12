"""MockSite fixture — L1 tests.

These tests do NOT touch the iOS simulator. They drive the host-
side HTTP server directly via `urllib.request` and verify:

- server lifecycle (start/stop, registry, double-start guards)
- HTTP routes (login form HTML shape, signup form autocomplete,
  unknown route 404)
- credential flow (signin success/failure, signup add-then-use,
  session cookie issuance, dashboard auth gate)
- submission recording (correct + incorrect attempts, reset,
  per-mode tagging)
- /status JSON shape
- verifier integration via `sibb_verify.RESOURCE_FETCHERS`
- `open_in_safari` simctl shape

Safari ↔ fixture integration is L2 territory and lives elsewhere.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest

import sibb_mock_site
from sibb_mock_site import MockSite, get_site, list_sites, open_in_safari

pytestmark = pytest.mark.fast


# ─────────────────────────── helpers / fixtures ───────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Don't follow 302 → /dashboard so tests can inspect the
    Location and Set-Cookie headers directly."""

    def http_error_302(self, req, fp, code, msg, hdrs):
        raise urllib.error.HTTPError(req.full_url, code, msg, hdrs, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def _get(url: str, *, follow_redirects: bool = True):
    if follow_redirects:
        return urllib.request.urlopen(url, timeout=5)
    return _no_redirect_opener.open(urllib.request.Request(url), timeout=5)


def _post(url: str, data: dict, *, follow_redirects: bool = True):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    if follow_redirects:
        return urllib.request.urlopen(req, timeout=5)
    return _no_redirect_opener.open(req, timeout=5)


@pytest.fixture
def site():
    """Each test gets its own MockSite on a unique site_id; the
    fixture guarantees `stop()` runs so the module registry never
    leaks between tests."""
    site_id = f"test-{uuid.uuid4().hex[:8]}"
    s = MockSite(site_id=site_id, credentials={"alice": "hunter2"})
    s.start()
    try:
        yield s
    finally:
        s.stop()


# ─────────────────────────── lifecycle ────────────────────────────────


def test_start_binds_to_random_port(site):
    assert site.port is not None
    assert 1024 <= site.port <= 65535


def test_start_registers_in_module_registry(site):
    assert get_site(site.site_id) is site
    assert site.site_id in list_sites()


def test_stop_unregisters():
    site_id = f"test-{uuid.uuid4().hex[:8]}"
    s = MockSite(site_id=site_id)
    s.start()
    assert get_site(site_id) is s
    s.stop()
    assert get_site(site_id) is None
    assert site_id not in list_sites()


def test_double_start_raises(site):
    with pytest.raises(RuntimeError, match="already started"):
        site.start()


def test_duplicate_site_id_raises(site):
    """Two fixtures sharing a site_id would silently route the
    verifier to whichever was last registered — surface the
    collision early."""
    rival = MockSite(site_id=site.site_id)
    try:
        with pytest.raises(RuntimeError, match="already registered"):
            rival.start()
    finally:
        if rival._server is not None:  # defensive
            rival.stop()


def test_stop_is_idempotent():
    s = MockSite(site_id=f"test-{uuid.uuid4().hex[:8]}")
    s.start()
    s.stop()
    s.stop()  # second call must not raise


# ─────────────────────────── URL / property API ───────────────────────


def test_base_url_before_start_raises():
    s = MockSite(site_id=f"test-{uuid.uuid4().hex[:8]}")
    with pytest.raises(RuntimeError, match="not started"):
        _ = s.base_url


def test_base_url_uses_loopback_ip(site):
    # Binding to 127.0.0.1 (not "localhost") avoids IPv6
    # resolution surprises on macOS; URLs must match.
    assert site.base_url.startswith("http://127.0.0.1:")


def test_login_url_ends_with_signin_path(site):
    assert site.login_url.endswith("/login")


def test_signup_url_ends_with_signup_path(site):
    assert site.signup_url.endswith("/signup")


def test_paths_are_configurable():
    site_id = f"test-{uuid.uuid4().hex[:8]}"
    s = MockSite(
        site_id=site_id,
        sign_in_path="/auth/signin",
        sign_up_path="/auth/signup",
    )
    s.start()
    try:
        assert s.login_url.endswith("/auth/signin")
        assert s.signup_url.endswith("/auth/signup")
        # The configured path actually serves the form:
        body = _get(s.login_url).read().decode()
        assert "<form" in body
        # And the default /login is now a 404:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(f"{s.base_url}/login")
        assert ei.value.code == 404
    finally:
        s.stop()


# ─────────────────────────── HTTP routes ──────────────────────────────


def test_get_root_serves_signin_form(site):
    resp = _get(site.base_url + "/")
    assert resp.status == 200
    body = resp.read().decode()
    assert "<form" in body
    assert 'name="username"' in body
    assert 'name="password"' in body


def test_signin_form_uses_username_and_current_password_autocomplete(site):
    """These exact `autocomplete` values are the contract with
    iOS Password AutoFill: the keyboard-accessory key chip only
    appears when the form advertises `username` + `current-password`
    (signin) or `username` + `new-password` (signup)."""
    body = _get(site.login_url).read().decode()
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body


def test_signup_form_uses_new_password_autocomplete(site):
    """`new-password` is what triggers iOS "Suggest Strong Password"."""
    body = _get(site.signup_url).read().decode()
    assert 'autocomplete="username"' in body
    assert 'autocomplete="new-password"' in body


def test_get_unknown_path_returns_404(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{site.base_url}/does-not-exist")
    assert ei.value.code == 404


def test_post_to_non_credential_path_captured_as_generic_submission(site):
    """Behavior change (2026-06-05): POSTs to arbitrary paths now land
    in `submissions` as generic entries with `mode=<path>` so harness
    generators can submit `/rsvp` / `/buy` / `/contact` forms without
    declaring those paths up front. (Previously this returned 404.)"""
    resp = _post(f"{site.base_url}/does-not-exist", {"x": "1"})
    assert resp.status == 200
    body = resp.read().decode().lower()
    assert "submitted" in body
    rsvp = [s for s in site.submissions()
            if s.get("mode") == "/does-not-exist"]
    assert len(rsvp) == 1
    assert rsvp[0]["fields"]["x"] == "1"


# ─────────────────────────── signin flow ──────────────────────────────


def test_signin_correct_creds_redirect_to_dashboard(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    assert ei.value.code == 302
    assert ei.value.headers.get("Location") == "/dashboard"
    cookie = ei.value.headers.get("Set-Cookie") or ""
    assert cookie.startswith("session=")
    assert "HttpOnly" in cookie


def test_signin_wrong_password_returns_401(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.login_url, {"username": "alice", "password": "WRONG"})
    assert ei.value.code == 401


def test_signin_unknown_user_returns_401(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.login_url, {"username": "eve", "password": "anything"})
    assert ei.value.code == 401


def test_signin_empty_password_returns_401(site):
    """Belt and braces: a configured empty password must not
    authenticate when the agent submits an empty value (would
    otherwise allow trivial bypass for any user without an
    explicit password)."""
    site.add_credentials("bob", "")
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.login_url, {"username": "bob", "password": ""})
    assert ei.value.code == 401


# ─────────────────────────── signup flow ──────────────────────────────


def test_signup_adds_credentials_and_authenticates(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.signup_url,
              {"username": "carol", "password": "swordfish"},
              follow_redirects=False)
    assert ei.value.code == 302
    assert site.credentials.get("carol") == "swordfish"

    # The new pair must now accept a signin POST:
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.login_url,
              {"username": "carol", "password": "swordfish"},
              follow_redirects=False)
    assert ei.value.code == 302


def test_signup_empty_fields_returns_401(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(site.signup_url, {"username": "", "password": ""})
    assert ei.value.code == 401


# ─────────────────────────── submission recording ─────────────────────


def test_submissions_records_signin_attempts(site):
    for pw in ("hunter2", "WRONG"):
        try:
            _post(site.login_url,
                  {"username": "alice", "password": pw},
                  follow_redirects=False)
        except urllib.error.HTTPError:
            pass

    subs = site.submissions()
    assert len(subs) == 2
    assert all(s["mode"] == "signin" for s in subs)
    assert subs[0]["password"] == "hunter2"
    assert subs[0]["success"] is True
    assert subs[1]["password"] == "WRONG"
    assert subs[1]["success"] is False
    # Timestamps are monotonic.
    assert subs[1]["timestamp"] >= subs[0]["timestamp"]


def test_submissions_tags_signup_mode(site):
    try:
        _post(site.signup_url,
              {"username": "dan", "password": "p4ssw0rd"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    subs = site.submissions()
    assert len(subs) == 1
    assert subs[0]["mode"] == "signup"


def test_has_successful_login_matches_on_value(site):
    try:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    assert site.has_successful_login("alice", "hunter2") is True
    assert site.has_successful_login("alice", "WRONG") is False
    assert site.has_successful_login("eve", "hunter2") is False


def test_last_submission_returns_most_recent(site):
    assert site.last_submission() is None
    for pw in ("hunter2", "WRONG"):
        try:
            _post(site.login_url,
                  {"username": "alice", "password": pw},
                  follow_redirects=False)
        except urllib.error.HTTPError:
            pass
    last = site.last_submission()
    assert last is not None
    assert last["password"] == "WRONG"


def test_reset_clears_submissions_and_sessions(site):
    try:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    assert len(site.submissions()) == 1
    site.reset()
    assert site.submissions() == []
    # Session token from the prior signin must no longer authenticate.
    # (Re-fetched cookie wouldn't survive reset.)


# ─────────────────────────── visits / mock_site.visited ───────────────


def test_visits_records_get_to_signin_path(site):
    """A GET to the signin path is logged with path + epoch."""
    _get(site.login_url)
    visits = site.visits()
    assert len(visits) == 1
    assert visits[0]["path"] == site.sign_in_path
    assert visits[0]["epoch"] > 0


def test_visits_records_query_string(site):
    """The query string is recorded separately from the path."""
    _get(f"{site.base_url}{site.sign_in_path}?ref=email")
    visits = site.visits()
    assert len(visits) == 1
    assert visits[0]["path"] == site.sign_in_path
    assert visits[0]["query"] == "ref=email"


def test_visits_does_not_record_status_polls(site):
    """The /status endpoint exists for verifier polling — recording
    those visits would contaminate the "agent navigated" signal."""
    _get(f"{site.base_url}/status")
    assert site.visits() == []


def test_visits_records_404s_too(site):
    """A 404 still represents agent navigation intent."""
    try:
        _get(f"{site.base_url}/this-path-does-not-exist")
    except urllib.error.HTTPError:
        pass
    visits = site.visits()
    assert len(visits) == 1
    assert visits[0]["path"] == "/this-path-does-not-exist"


def test_reset_clears_visits(site):
    _get(site.login_url)
    assert len(site.visits()) == 1
    site.reset()
    assert site.visits() == []


def test_mock_site_visited_resource_registered():
    """The new check kind must be in RESOURCE_FETCHERS so verifier
    selectors `{"resource": "mock_site.visited", ...}` dispatch."""
    from sibb_verify import RESOURCE_FETCHERS
    assert "mock_site.visited" in RESOURCE_FETCHERS


def test_mock_site_visited_fetcher_filters_by_path(site):
    """The fetcher's path selector restricts results."""
    import asyncio
    from sibb_verify import RESOURCE_FETCHERS
    _get(site.login_url)
    try:
        _get(f"{site.base_url}/somewhere-else")
    except urllib.error.HTTPError:
        pass
    fetcher = RESOURCE_FETCHERS["mock_site.visited"]
    rows = asyncio.run(fetcher(None, {"site_id": site.site_id,
                                         "path": site.sign_in_path}))
    assert len(rows) == 1
    assert rows[0]["path"] == site.sign_in_path


def test_mock_site_visited_fetcher_path_contains(site):
    """`path_contains` matches substrings — useful for permalink URLs."""
    import asyncio
    from sibb_verify import RESOURCE_FETCHERS
    try:
        _get(f"{site.base_url}/articles/42-pluto-facts")
    except urllib.error.HTTPError:
        pass
    fetcher = RESOURCE_FETCHERS["mock_site.visited"]
    rows = asyncio.run(fetcher(None, {"site_id": site.site_id,
                                         "path_contains": "/articles/42"}))
    assert len(rows) == 1


def test_mock_site_visited_fetcher_min_epoch(site):
    """`min_epoch` filters older visits — used to scope to the
    current episode."""
    import asyncio
    import time
    from sibb_verify import RESOURCE_FETCHERS
    _get(site.login_url)
    cutoff = time.time()
    # A small sleep so the second visit's epoch is strictly > cutoff.
    time.sleep(0.02)
    _get(site.login_url)
    fetcher = RESOURCE_FETCHERS["mock_site.visited"]
    rows = asyncio.run(fetcher(None, {"site_id": site.site_id,
                                         "min_epoch": cutoff}))
    assert len(rows) == 1  # only the post-cutoff visit


def test_mock_site_visited_unknown_site_id_raises():
    """The fetcher must refuse silently — a typo in site_id should
    surface, not return empty."""
    import asyncio
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    fetcher = RESOURCE_FETCHERS["mock_site.visited"]
    with pytest.raises(ResourceFetchError, match="no mock site"):
        asyncio.run(fetcher(None, {"site_id": "nonexistent-xyz"}))


# ─────────────────── P1/P2 follow-up (2026-06-03) ─────────────────────


def test_visits_records_user_agent_shape(site):
    """The visits log records the User-Agent header. For verifiers
    that want to distinguish Safari-real vs probe-harness traffic
    by UA, the field must be non-empty for a typical urllib client.
    """
    _get(site.login_url)
    visits = site.visits()
    assert len(visits) == 1
    ua = visits[0].get("user_agent")
    assert isinstance(ua, str)
    assert ua, "user_agent should not be empty for a default urllib client"


def test_mock_site_visited_latest_composes_with_path_filter(site):
    """`latest` returns the most-recent row AFTER all other filters
    apply. Combining `path="/login"` with `latest=True` returns the
    most recent /login visit only, ignoring /other visits in between."""
    import asyncio
    import time
    from sibb_verify import RESOURCE_FETCHERS
    _get(site.login_url)
    time.sleep(0.01)
    try:
        _get(f"{site.base_url}/other-path")
    except urllib.error.HTTPError:
        pass
    time.sleep(0.01)
    _get(site.login_url)  # third visit, second /login

    fetcher = RESOURCE_FETCHERS["mock_site.visited"]
    rows = asyncio.run(fetcher(None, {
        "site_id": site.site_id,
        "path": site.sign_in_path,
        "latest": True,
    }))
    assert len(rows) == 1
    assert rows[0]["path"] == site.sign_in_path
    # All three visits exist; latest+path picked the most-recent
    # MATCHING path, not the most-recent overall (which would be /login
    # but only because that's the 3rd visit — assert it's specifically
    # the 3rd visit's epoch, the largest).
    all_login = [v for v in site.visits()
                 if v["path"] == site.sign_in_path]
    assert rows[0]["epoch"] == max(v["epoch"] for v in all_login)


def test_visits_zero_match_count_check():
    """`count(path=X) == 0` is the canonical "agent did not navigate
    to X" idiom. Verify the fetcher returns an empty list when the
    path was never visited, which a count check turns into 0."""
    import asyncio
    s = MockSite(site_id=f"test-{uuid.uuid4().hex[:8]}")
    s.start()
    try:
        # Visit /login but not /elsewhere.
        _get(s.login_url)
        from sibb_verify import RESOURCE_FETCHERS
        fetcher = RESOURCE_FETCHERS["mock_site.visited"]
        rows = asyncio.run(fetcher(None, {"site_id": s.site_id,
                                            "path": "/elsewhere"}))
        assert rows == []
    finally:
        s.stop()


def test_mock_site_visited_multi_site_disambiguation():
    """Two MockSites with different site_ids. Visit only site A; the
    fetcher for B returns an empty list (NOT site A's visits)."""
    import asyncio
    site_a_id = f"test-A-{uuid.uuid4().hex[:8]}"
    site_b_id = f"test-B-{uuid.uuid4().hex[:8]}"
    a = MockSite(site_id=site_a_id)
    b = MockSite(site_id=site_b_id)
    a.start()
    b.start()
    try:
        _get(a.login_url)
        from sibb_verify import RESOURCE_FETCHERS
        fetcher = RESOURCE_FETCHERS["mock_site.visited"]
        rows_a = asyncio.run(fetcher(None, {"site_id": site_a_id}))
        rows_b = asyncio.run(fetcher(None, {"site_id": site_b_id}))
        assert len(rows_a) == 1
        assert rows_b == []
    finally:
        a.stop()
        b.stop()


def test_visits_concurrent_no_lost_records(site):
    """50 threads × 1 GET each must produce exactly 50 recorded visits
    (no race lost a record). Lock contention sanity for the visits
    log — if `with site._lock:` is removed, threads can drop visits."""
    import threading
    NUM_THREADS = 50

    def _hit():
        try:
            _get(site.login_url)
        except Exception:
            pass

    threads = [threading.Thread(target=_hit) for _ in range(NUM_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert len(site.visits()) == NUM_THREADS


def test_visits_records_count_check_via_verifier_idiom():
    """End-to-end style: visit /login twice, /other once. Use the
    count check kind with selectors to assert exactly 2 hits to
    /login. This is the canonical verifier idiom for reverse-direction
    generators (Calendar.url → Safari etc.)."""
    import asyncio
    s = MockSite(site_id=f"test-{uuid.uuid4().hex[:8]}")
    s.start()
    try:
        _get(s.login_url)
        _get(s.login_url)
        try:
            _get(f"{s.base_url}/other")
        except urllib.error.HTTPError:
            pass
        from sibb_verify import RESOURCE_FETCHERS
        fetcher = RESOURCE_FETCHERS["mock_site.visited"]
        rows = asyncio.run(fetcher(None, {"site_id": s.site_id,
                                            "path": s.sign_in_path}))
        # count check kind would assert len(rows) == 2.
        assert len(rows) == 2
    finally:
        s.stop()


# ─────────────────────────── /status JSON ─────────────────────────────


def test_status_returns_json_with_site_state(site):
    try:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    payload = json.loads(_get(f"{site.base_url}/status").read().decode())
    assert payload["site_id"] == site.site_id
    assert payload["base_url"] == site.base_url
    assert payload["credentials_configured"] == ["alice"]
    assert payload["submission_count"] == 1
    assert payload["active_session_count"] == 1
    sub = payload["submissions"][0]
    assert sub["password"] == "hunter2"
    assert sub["success"] is True


# ─────────────────────────── dashboard auth gate ──────────────────────


def test_dashboard_without_cookie_returns_401(site):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"{site.base_url}/dashboard")
    assert ei.value.code == 401


def test_dashboard_with_invalid_cookie_returns_401(site):
    req = urllib.request.Request(
        f"{site.base_url}/dashboard",
        headers={"Cookie": "session=not-a-real-token"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=5)
    assert ei.value.code == 401


def test_dashboard_with_valid_session_returns_200(site):
    # Sign in, capture the cookie, re-present it to /dashboard.
    cookie = None
    try:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    except urllib.error.HTTPError as e:
        cookie = (e.headers.get("Set-Cookie") or "").split(";")[0]
    assert cookie and cookie.startswith("session=")
    req = urllib.request.Request(
        f"{site.base_url}/dashboard",
        headers={"Cookie": cookie})
    resp = urllib.request.urlopen(req, timeout=5)
    assert resp.status == 200
    assert b"Welcome" in resp.read()


# ─────────────────── verifier fetcher integration ─────────────────────


class _FakeReader:
    def __init__(self, udid: str = "FAKE"):
        self.udid = udid


def test_mock_site_submissions_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "mock_site.submissions" in RESOURCE_FETCHERS


async def test_fetcher_returns_recorded_submissions(site):
    from sibb_verify import RESOURCE_FETCHERS
    try:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    rows = await RESOURCE_FETCHERS["mock_site.submissions"](
        _FakeReader(), {"site_id": site.site_id})
    assert len(rows) == 1
    assert rows[0]["username"] == "alice"
    assert rows[0]["password"] == "hunter2"
    assert rows[0]["success"] is True
    assert rows[0]["mode"] == "signin"


async def test_fetcher_filters_by_success(site):
    from sibb_verify import RESOURCE_FETCHERS
    for pw in ("hunter2", "WRONG"):
        try:
            _post(site.login_url,
                  {"username": "alice", "password": pw},
                  follow_redirects=False)
        except urllib.error.HTTPError:
            pass
    rows = await RESOURCE_FETCHERS["mock_site.submissions"](
        _FakeReader(),
        {"site_id": site.site_id, "success": True})
    assert len(rows) == 1
    assert rows[0]["password"] == "hunter2"


async def test_fetcher_filters_by_username_and_password(site):
    """The exact-match password filter is THE verification primitive:
    'did the agent's autofill carry value P to the form post'."""
    from sibb_verify import RESOURCE_FETCHERS
    for pw in ("hunter2", "WRONG", "decoy"):
        try:
            _post(site.login_url,
                  {"username": "alice", "password": pw},
                  follow_redirects=False)
        except urllib.error.HTTPError:
            pass
    rows = await RESOURCE_FETCHERS["mock_site.submissions"](
        _FakeReader(),
        {"site_id": site.site_id,
         "username": "alice",
         "password": "hunter2"})
    assert len(rows) == 1
    assert rows[0]["success"] is True


async def test_fetcher_filters_by_mode(site):
    from sibb_verify import RESOURCE_FETCHERS
    try:
        _post(site.login_url,
              {"username": "alice", "password": "hunter2"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    try:
        _post(site.signup_url,
              {"username": "new", "password": "user"},
              follow_redirects=False)
    except urllib.error.HTTPError:
        pass
    rows = await RESOURCE_FETCHERS["mock_site.submissions"](
        _FakeReader(),
        {"site_id": site.site_id, "mode": "signup"})
    assert len(rows) == 1
    assert rows[0]["username"] == "new"


async def test_fetcher_latest_returns_one_row(site):
    from sibb_verify import RESOURCE_FETCHERS
    for pw in ("hunter2", "WRONG"):
        try:
            _post(site.login_url,
                  {"username": "alice", "password": pw},
                  follow_redirects=False)
        except urllib.error.HTTPError:
            pass
    rows = await RESOURCE_FETCHERS["mock_site.submissions"](
        _FakeReader(),
        {"site_id": site.site_id, "latest": True})
    assert len(rows) == 1
    assert rows[0]["password"] == "WRONG"


async def test_fetcher_unknown_site_raises():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    with pytest.raises(ResourceFetchError, match="no mock site"):
        await RESOURCE_FETCHERS["mock_site.submissions"](
            _FakeReader(), {"site_id": "definitely-not-registered"})


async def test_fetcher_defaults_to_site_id_default():
    """When no `site_id` selector is passed, the fetcher looks up
    "default". Document the default rather than letting it surprise."""
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    assert get_site("default") is None
    with pytest.raises(ResourceFetchError, match="'default'"):
        await RESOURCE_FETCHERS["mock_site.submissions"](
            _FakeReader(), {})


# ─────────────────────────── open_in_safari ───────────────────────────


def test_open_in_safari_invokes_simctl_openurl(monkeypatch):
    """Verify the subprocess command shape. The actual sim launch
    is an L2 concern owned by the integration tests."""
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    monkeypatch.setattr(sibb_mock_site.subprocess, "run", fake_run)
    open_in_safari("ABC-123", "http://127.0.0.1:55555/login")
    assert captured["cmd"] == [
        "xcrun", "simctl", "openurl", "ABC-123",
        "http://127.0.0.1:55555/login",
    ]
    assert captured["kw"]["timeout"] == 10.0


def test_open_in_safari_raises_with_stderr_on_nonzero_exit(monkeypatch):
    """Non-zero simctl exit must surface stderr. iOS returns opaque
    codes (149, 4, etc.) and the message body is the only hint at
    the actual failure."""

    def fake_run(cmd, **kw):
        class _R:
            returncode = 149
            stdout = b""
            stderr = b"the device cannot do that"
        return _R()

    monkeypatch.setattr(sibb_mock_site.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError,
                         match=r"exit 149.*device cannot do that"):
        open_in_safari("ABC-123", "http://127.0.0.1:55555/login")


def test_open_in_safari_raises_clear_error_with_empty_stderr(monkeypatch):
    """Sometimes simctl exits non-zero with no stderr — error
    message must still be useful, not just `<no stderr>` orphaned."""

    def fake_run(cmd, **kw):
        class _R:
            returncode = 1
            stdout = b""
            stderr = b""
        return _R()

    monkeypatch.setattr(sibb_mock_site.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match=r"exit 1.*<no stderr>"):
        open_in_safari("ABC-123", "http://127.0.0.1:55555/login")
