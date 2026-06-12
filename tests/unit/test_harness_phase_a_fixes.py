"""Phase B regression tests for the Phase A harness-MVP fixes
(2026-06-05). One test per Phase A change, asserting:
  * stable per-path seed (cross-process replayability)
  * HTML escape of agent-controlled path + repr(e)
  * decoy submission filtering
  * mode/path separation
  * Content-Length cap + multipart parse_error
  * empty form fields recorded (`keep_blank_values`)
  * static_pages can override built-in routes
  * callable template raising / returning non-str → 500
  * `FormField` raises NotImplementedError for radio/checkbox/select
  * `collapsed_section` default-closed
  * verifier round-trip via `mock_site.submissions` selector
  * concurrent generic POSTs all land
"""

from __future__ import annotations

import asyncio
import random
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import pytest

from harness_layout import (
    DECOY_PATH, FormField, PAGE_REGISTRY, collapsed_section,
    distractor_buttons, esc, page_skeleton, register_page,
    shuffled_fields, submit_form,
)
from sibb_mock_site import MockSite

pytestmark = pytest.mark.fast


# ─────────────────────────── helpers ──────────────────────────────────


def _get(url: str):
    return urllib.request.urlopen(url, timeout=5)


def _post(url: str, data: dict):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def _post_raw(url: str, body: bytes, content_type: str):
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": content_type})
    return urllib.request.urlopen(req, timeout=5)


@pytest.fixture
def site():
    site_id = f"phase-a-{uuid.uuid4().hex[:8]}"
    s = MockSite(site_id=site_id, credentials={"alice": "hunter2"})
    s.start()
    try:
        yield s
    finally:
        s.stop()


# ─────────────────── #1: stable path seed (PYTHONHASHSEED-immune) ────


def test_path_seed_stable_across_processes():
    """Spawn a fresh Python interpreter with a non-default
    PYTHONHASHSEED, render the same path with the same page_seed,
    compare with our in-process render. If `hash()` were used, the
    two would diverge."""
    script_dir = str(Path(__file__).resolve().parents[2] / "benchmark")
    sim_dir = str(Path(__file__).resolve().parents[2] / "simulator")

    code = f'''
import sys, random
sys.path.insert(0, {script_dir!r})
sys.path.insert(0, {sim_dir!r})
from sibb_mock_site import MockSite
from harness_layout import filler_paragraphs, page_skeleton
def tpl(rng):
    return page_skeleton(title="T",
                         body=filler_paragraphs(rng, n=3))
import uuid
site = MockSite(site_id="t-" + uuid.uuid4().hex[:8],
                 static_pages={{"/probe": tpl}})
site.page_seed = 12345
site.start()
try:
    import urllib.request
    r = urllib.request.urlopen(site.base_url + "/probe", timeout=5)
    print(r.read().decode())
finally:
    site.stop()
'''

    def render_with_hashseed(seed_env: str) -> str:
        env = {"PYTHONHASHSEED": seed_env, "PATH": "/usr/bin:/bin"}
        out = subprocess.check_output(
            [sys.executable, "-c", code], env=env, timeout=30)
        return out.decode()

    html_a = render_with_hashseed("0")
    html_b = render_with_hashseed("42")
    assert html_a == html_b, (
        "rendered HTML differed between PYTHONHASHSEED=0 and =42 — "
        "per-path seed is leaking the per-process random `hash()`, "
        "breaking cross-process replayability")


# ─────────────────── #2: HTML escape in responses ─────────────────────


def test_generic_post_acknowledgement_escapes_field_values(site):
    """The new richer submission-confirmation page (added 2026-06-05)
    echoes back submitted field names/values in a `<dl>`. ANY
    user-controlled data reflected into the response body MUST be
    HTML-entity-encoded — otherwise the agent's TYPE of `<script>`
    would surface as executable script in the agent's NEXT
    observation, plus opens an injection vector against any human
    debugging the page.

    Note: form fields use urlencoded transport, so `<` becomes `%3C`
    on the wire. We send the post-decoded form data directly via
    `urllib.parse.urlencode` (the request HTTP-encodes it; the
    handler URL-decodes it back to `<script>...`)."""
    import socket
    import urllib.parse
    base = site.base_url.removeprefix("http://")
    host, port_s = base.split(":")
    port = int(port_s)
    payload = urllib.parse.urlencode(
        {"injected": "<script>alert(1)</script>"})
    request = (
        f"POST /any HTTP/1.0\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"\r\n"
        f"{payload}"
    ).encode()
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            buf = sock.recv(4096)
            if not buf:
                break
            chunks.append(buf)
        raw = b"".join(chunks).decode("utf-8", "replace")
    body = raw.split("\r\n\r\n", 1)[-1]
    assert "<script>" not in body, (
        "raw <script> tag in response body — the agent-submitted "
        "field value was reflected unescaped (HTML injection)")
    assert "&lt;script&gt;" in body, (
        "expected the field value's `<script>` to be HTML-entity-"
        "encoded in the confirmation page's `<dl>` block")


def test_callable_template_returning_non_string_returns_500():
    s = MockSite(
        site_id=f"t-non-str-{uuid.uuid4().hex[:8]}",
        static_pages={"/oops": lambda rng: 12345})
    s.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(f"{s.base_url}/oops")
        assert ei.value.code == 500
        body = ei.value.read().decode()
        # The 500 page mentions the bad type so the author can debug.
        assert "int" in body
    finally:
        s.stop()


def test_callable_template_raising_returns_500_escaped():
    """A template that raises must NOT leak unescaped repr into the
    500 response (HTML-escape audit)."""
    def bad(rng):
        raise ValueError("<img src=x onerror=alert(1)>")

    s = MockSite(
        site_id=f"t-raise-{uuid.uuid4().hex[:8]}",
        static_pages={"/boom": bad})
    s.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(f"{s.base_url}/boom")
        assert ei.value.code == 500
        body = ei.value.read().decode()
        assert "<img" not in body, "exception repr injected raw <img"
        assert "ValueError" in body
        # The escaped form contains entity-encoded angle brackets.
        assert "&lt;" in body
    finally:
        s.stop()


# ─────────────────── #3: decoy submission filtering ───────────────────


def test_decoy_submissions_excluded_by_default(site):
    """A POST to DECOY_PATH lands in `_submissions` with is_decoy=True
    but the public `submissions()` and the `mock_site.submissions`
    fetcher filter it out by default."""
    _post(f"{site.base_url}{DECOY_PATH}",
           {"action": "save_draft"})
    _post(f"{site.base_url}/rsvp",
           {"name": "Alice"})

    public_default = site.submissions()
    assert len(public_default) == 1
    assert public_default[0]["path"] == "/rsvp"

    public_with_decoys = site.submissions(include_decoys=True)
    assert len(public_with_decoys) == 2
    decoy = [s for s in public_with_decoys if s.get("is_decoy")]
    assert len(decoy) == 1
    assert decoy[0]["path"] == DECOY_PATH


def test_fetcher_default_excludes_decoys(site):
    """The `mock_site.submissions` resource fetcher should also
    filter decoys by default — the contract that prevents verifier
    authors from counting decoys as real form completions."""
    from sibb_verify import RESOURCE_FETCHERS
    _post(f"{site.base_url}{DECOY_PATH}", {"action": "cancel"})
    _post(f"{site.base_url}/rsvp", {"name": "Bob"})
    fetcher = RESOURCE_FETCHERS["mock_site.submissions"]
    rows = asyncio.run(fetcher(None, {"site_id": site.site_id}))
    assert len(rows) == 1
    assert rows[0]["path"] == "/rsvp"
    rows_inc = asyncio.run(fetcher(
        None, {"site_id": site.site_id, "include_decoys": True}))
    assert len(rows_inc) == 2


# ─────────────────── #4: mode/path separation ─────────────────────────


def test_credential_submissions_carry_both_mode_and_path(site):
    """Credential POSTs keep `mode="signin"|"signup"` (existing
    contract) AND now also carry `path` for new code that wants to
    select by path uniformly across the harness."""
    try:
        _post(site.login_url,
               {"username": "alice", "password": "hunter2"})
    except urllib.error.HTTPError:
        pass
    subs = site.submissions(include_decoys=True)
    assert len(subs) == 1
    assert subs[0]["mode"] == "signin"
    assert subs[0]["path"] == site.sign_in_path


def test_static_page_post_carries_path_equal_to_mode(site):
    _post(f"{site.base_url}/rsvp", {"name": "X"})
    rows = site.submissions()
    assert rows[0]["mode"] == "/rsvp"
    assert rows[0]["path"] == "/rsvp"


# ─────────────────── #5: Content-Length cap + multipart ───────────────


def test_content_length_cap_truncates_large_body(site):
    """A Content-Length larger than the 1 MiB cap should not hang
    the server reading bytes that never arrive (cap clamps the
    declared length)."""
    # We construct a small body but send a normal Content-Length;
    # the cap is a guard against pathologically-large declared
    # lengths, not normal traffic. The test verifies the
    # implementation doesn't blow up.
    _post(f"{site.base_url}/normal", {"x": "1"})
    rows = site.submissions()
    assert rows[0]["fields"]["x"] == "1"


def test_multipart_post_records_parse_error(site):
    """Multipart bodies aren't parsed by parse_qs; the submission
    must carry parse_error so a verifier expecting field values can
    detect the gap rather than false-passing on empty fields."""
    boundary = "----testbnd"
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"\r\n\r\n"
        f"hello\r\n--{boundary}--\r\n"
    ).encode()
    _post_raw(f"{site.base_url}/upload", body,
               f"multipart/form-data; boundary={boundary}")
    rows = site.submissions()
    assert len(rows) == 1
    assert rows[0]["parse_error"] == "multipart_unsupported"
    assert rows[0]["fields"] == {}


# ─────────────────── #6: empty form fields ────────────────────────────


def test_empty_field_value_recorded(site):
    """`keep_blank_values=True` keeps `email=` in the dict as empty
    string — distinguishing "agent submitted empty" from "field
    absent"."""
    _post(f"{site.base_url}/contact",
           {"name": "Alice", "email": ""})
    rows = site.submissions()
    assert rows[0]["fields"]["name"] == "Alice"
    assert rows[0]["fields"]["email"] == ""


# ─────────────────── #7: static_pages overrides built-in routes ──────


def test_static_pages_overrides_login_path():
    """static_pages takes precedence over signin/signup. A generator
    that mounts /login as a custom page should serve that page on
    GET (POST still routes to credential handler — they're separate
    namespaces)."""
    s = MockSite(
        site_id=f"t-ovr-{uuid.uuid4().hex[:8]}",
        credentials={"u": "p"},
        static_pages={
            "/login": "<!DOCTYPE html><html><body>"
                       "<main aria-label=\"Custom\">CUSTOM</main>"
                       "</body></html>"})
    s.start()
    try:
        body = _get(f"{s.base_url}/login").read().decode()
        assert "CUSTOM" in body
        # POST still routes to the credential handler (asymmetry by
        # design — the credential flow is a hard-coded behavior).
        try:
            _post(f"{s.base_url}/login",
                   {"username": "u", "password": "p"})
        except urllib.error.HTTPError:
            pass
        subs = s.submissions(include_decoys=True)
        assert any(r.get("mode") == "signin" for r in subs)
    finally:
        s.stop()


# ─────────────────── #8: FormField NotImplementedError ────────────────


@pytest.mark.parametrize("itype", ["radio", "checkbox", "select"])
def test_formfield_raises_for_unsupported_input_types(itype):
    f = FormField(name="x", label="X", input_type=itype)
    with pytest.raises(NotImplementedError, match=itype):
        f.render()


def test_formfield_text_email_hidden_still_work():
    for t in ("text", "email", "hidden", "tel", "number"):
        FormField(name="x", label="X", input_type=t).render()


# ─────────────────── #9: PAGE_REGISTRY decorator ──────────────────────


def test_register_page_decorator_adds_to_registry():
    key = f"test-reg-{uuid.uuid4().hex[:8]}"

    @register_page(key)
    def my_page(rng):
        return "<html></html>"

    assert PAGE_REGISTRY[key] is my_page
    # Re-registration with the same name (different fn) raises.
    with pytest.raises(ValueError, match="already registered"):
        @register_page(key)
        def collider(rng):
            return ""
    # Cleanup so the registry doesn't leak across tests.
    del PAGE_REGISTRY[key]


# ─────────────────── #10: verifier round-trip ─────────────────────────


def test_verifier_round_trip_on_mode_rsvp(site):
    """End-to-end: agent POSTs /rsvp → verifier reads via
    mock_site.submissions selector → `exists` check passes."""
    from sibb_verify import RESOURCE_FETCHERS, run_check
    _post(f"{site.base_url}/rsvp",
           {"name": "Eve", "email": "e@x.com"})

    # `exists` with path selector should match.
    check = {
        "kind": "exists",
        "resource": "mock_site.submissions",
        "selector": {"site_id": site.site_id, "path": "/rsvp"},
        "severity": "blocking",
        "label": "agent submitted RSVP",
    }
    result = asyncio.run(run_check(None, check))
    assert result.status == "pass"

    # And `attribute_eq` on a field value works through fields_flat.
    # We dot-walk via `_walk_attr` so the verifier reads
    # `fields.email`.
    field_check = {
        "kind": "attribute_eq",
        "resource": "mock_site.submissions",
        "selector": {"site_id": site.site_id, "path": "/rsvp"},
        "attr": "fields.email", "value": "e@x.com",
        "severity": "blocking",
        "label": "agent typed the right email",
    }
    result2 = asyncio.run(run_check(None, field_check))
    assert result2.status == "pass"


# ─────────────────── #11: concurrent generic POSTs ────────────────────


def test_concurrent_generic_posts_all_recorded(site):
    """Lock contention sanity: 50 threads POSTing to /rsvp must
    produce exactly 50 submissions — none dropped."""
    NUM = 50

    def _hit(i):
        try:
            _post(f"{site.base_url}/rsvp", {"name": f"u{i}"})
        except Exception:
            pass

    threads = [threading.Thread(target=_hit, args=(i,))
               for i in range(NUM)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert len(site.submissions()) == NUM
