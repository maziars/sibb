"""SafariHandler — L2 sim integration.

Exercises Bookmarks.db SQL inject + mock-site fixture lifecycle
against the real simulator. Unlike L1.5 (which uses in-memory
sqlite + monkey-patched simctl helpers), this verifies:
1. The on-disk path resolution works for a real UDID
2. Safari's real Bookmarks.db schema accepts our INSERT shape
3. The bookmark surfaces in the Safari UI on next launch
   (caught by Messages but proven viable for Safari 2026-05-16)
4. `apply(mock_site)` against a real sim spawns the host-side
   HTTP fixture, terminates Safari, navigates Safari to the
   login URL, and the resulting AX tree exposes the form fields
   (verified empirically via `sibb_probe_safari_ax.py` 2026-05-17;
   see `IOS_SIM_QUIRKS.md` §14 for the full Safari AX taxonomy)
5. Form submission through real Safari (type into AX TextFields,
   submit, server records the password value) — closes the
   verification loop the keychain encryption blocks
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim

_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
for p in (_SIM_DIR, _BENCHMARK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sibb_scaffold import AXReader, ElementRole  # noqa: E402
from sibb_state import (  # noqa: E402
    SafariHandler,
    _safari_clear_tab_state,
    _safari_insert_bookmark,
    _safari_list_bookmarks,
    _safari_terminate,
)


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[AXReader]:
    # Launch Safari once to ensure Bookmarks.db exists, then
    # terminate so the test's first insert doesn't race the app.
    import subprocess
    subprocess.run(["xcrun", "simctl", "launch", sibb_udid,
                     "com.apple.mobilesafari"],
                    capture_output=True, timeout=10)
    import asyncio
    await asyncio.sleep(3.0)
    subprocess.run(["xcrun", "simctl", "terminate", sibb_udid,
                     "com.apple.mobilesafari"],
                    capture_output=True, timeout=10)
    await asyncio.sleep(1.0)
    r = AXReader(sibb_udid)
    await r.start(bundle_id="com.apple.springboard")
    try:
        yield r
    finally:
        await r.stop()


# ────────────────────── SQL helpers against real DB ──────────────

async def test_insert_returns_row_id(sibb_udid: str, reader):
    new_id = await _safari_insert_bookmark(
        sibb_udid, "L2 Test 1", "https://l2-test-1.example.com")
    assert new_id > 0


async def test_list_returns_inserted_bookmarks(sibb_udid: str, reader):
    """Insert two distinctive bookmarks, confirm the list helper
    returns them. Avoids hardcoded defaults — looks for our markers."""
    await _safari_insert_bookmark(
        sibb_udid, "L2 Marker A", "https://marker-a.example")
    await _safari_insert_bookmark(
        sibb_udid, "L2 Marker B", "https://marker-b.example")
    rows = await _safari_list_bookmarks(sibb_udid)
    titles = {r["title"] for r in rows}
    assert "L2 Marker A" in titles
    assert "L2 Marker B" in titles


# ────────────────────── handler + fetcher integration ────────────

async def test_handler_apply_then_fetcher_round_trip(sibb_udid: str, reader):
    """End-to-end: handler.apply writes bookmark, resource fetcher
    reads it back. Mirrors the verifier-AFTER loop. Uses a
    UUID-suffixed title so re-runs against the same clone don't
    collide on prior inserts."""
    import uuid as _uuid
    from sibb_verify import RESOURCE_FETCHERS

    marker = f"Round Trip {_uuid.uuid4().hex[:8]}"
    url = f"https://handler-rt.example/{marker}"

    class _Reader:
        def __init__(self, udid):
            self.udid = udid
    h = SafariHandler(reader=_Reader(sibb_udid))
    await h.apply({"type": "bookmark", "title": marker, "url": url})

    fetcher = RESOURCE_FETCHERS["safari.bookmarks"]
    rows = await fetcher(_Reader(sibb_udid), {"title": marker})
    assert len(rows) == 1
    assert rows[0]["url"] == url


# ────────────────────── UI surfacing (the viability check) ────────

async def test_inserted_bookmark_visible_in_safari_ui(sibb_udid: str,
                                                       reader):
    """Inject a bookmark via SQL, launch Safari, dump AX tree,
    confirm the bookmark surfaces. This is what made Safari viable
    where Messages wasn't.
    """
    sentinel_title = "SIBB L2 UI Visible Test"
    sentinel_url = "https://sibb-l2-ui-test.example.com"
    await _safari_insert_bookmark(
        sibb_udid, sentinel_title, sentinel_url)

    # Force Safari to its Start Page on next launch. Without this,
    # iOS restores whatever tab was last open (could be a prior
    # mock-site URL, a probe artifact, etc.), and the bookmarks
    # never surface in the AX tree because we're not on the Start
    # Page. `_safari_terminate` is required before the file delete
    # so we don't corrupt Safari's open WAL state.
    await _safari_terminate(sibb_udid)
    _safari_clear_tab_state(sibb_udid)

    # Re-attach reader to Safari (the fixture's reader was attached
    # to SpringBoard for the SQL-only tests).
    safari_reader = AXReader(sibb_udid)
    await safari_reader.start(bundle_id="com.apple.mobilesafari")
    try:
        import asyncio
        await asyncio.sleep(2.0)
        # Dismiss any "Continue"-style onboardings.
        for _ in range(4):
            tree = await safari_reader.read()
            cont = next(
                (e for e in tree.elements
                 if e.effective_role == ElementRole.BUTTON
                 and (e.effective_label or "").strip()
                     in ("Continue", "Not Now", "Skip", "Done",
                         "Start Browsing", "Get Started")),
                None,
            )
            if not cont or not cont.frame:
                break
            await safari_reader._xcuitest.tap(
                cont.frame.center_x, cont.frame.center_y)
            await asyncio.sleep(1.0)

        # Safari's Start Page shows only the top-N favorites by default;
        # rest are hidden behind "Show All". Tap it so all bookmarks
        # are reachable in the visible AX tree.
        tree = await safari_reader.read()
        show_all = next(
            (e for e in tree.elements
             if e.effective_role == ElementRole.BUTTON
             and (e.effective_label or "").strip() == "Show All"),
            None,
        )
        if show_all and show_all.frame:
            await safari_reader._xcuitest.tap(
                show_all.frame.center_x, show_all.frame.center_y)
            await asyncio.sleep(0.8)
            tree = await safari_reader.read()

        labels = [e.effective_label or "" for e in tree.elements]
        assert any(sentinel_title in lbl for lbl in labels), (
            f"sentinel bookmark not in Safari AX tree after tapping "
            f"'Show All'. Cells/StaticText labels found: "
            f"{[l for l in labels if l][:25]}"
        )
    finally:
        await safari_reader.stop()


# ───────────────────── mock-site fixture lifecycle ───────────────────
#
# These tests prove the SafariHandler `mock_site` entry shipped in
# `sibb_state.py` actually works against a real sim:
#   - the host-side HTTP fixture is reachable from sim Safari
#   - Safari loads the page and the form is in the AX tree
#   - submitting through real Safari lands plaintext at the server
#   - `handler.reset()` stops the fixture cleanly
#
# Sim-side state we touch: just Safari (terminate + openurl). No
# permissions, no defaults, no clone required — the mock site lives
# entirely on the host and the sim sees it as any external URL.

import asyncio  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402


class _Reader:
    """Bare-bones reader stand-in for the handler — the bookmark
    path only uses `.udid`, and `_apply_mock_site` does too. We
    don't pass the module-scoped AXReader because the handler
    terminates Safari, which would close its socket."""
    def __init__(self, udid: str):
        self.udid = udid


async def _safari_reader(udid: str) -> AXReader:
    """Open a fresh Safari-attached AXReader. Caller is responsible
    for calling `.stop()`."""
    r = AXReader(udid)
    await r.start(bundle_id="com.apple.mobilesafari")
    await asyncio.sleep(1.5)  # let WebKit settle
    return r


async def _dismiss_safari_onboarding(reader: AXReader) -> None:
    """Tap through any leftover onboarding/welcome buttons. Idempotent
    no-op on a primed sim, important on a fresh clone."""
    for _ in range(4):
        tree = await reader.read()
        cont = next(
            (e for e in tree.elements
             if e.effective_role == ElementRole.BUTTON
             and (e.effective_label or "").strip()
                 in ("Continue", "Not Now", "Skip", "Done",
                     "Start Browsing", "Get Started")),
            None,
        )
        if not cont or not cont.frame:
            break
        await reader._xcuitest.tap(
            cont.frame.center_x, cont.frame.center_y)
        await asyncio.sleep(1.0)


def _post_to_fixture(url: str, data: dict) -> int:
    """Sanity-poke the fixture from the host side. Used in the
    reset test to confirm the port stops responding after stop()."""
    import urllib.parse
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


async def test_apply_mock_site_navigates_safari_to_visible_form(
        sibb_udid: str, reader):
    """End-to-end: `apply(mock_site, open_at_start=True)` against a
    real sim must (1) start a reachable HTTP fixture, (2) terminate
    any prior Safari, (3) navigate Safari to the login URL, and
    (4) leave the AX tree exposing the username/password TextFields
    and the Sign In Button. Anchored on `placeholder` reading as
    `value` (per `IOS_SIM_QUIRKS.md` §14, "universal behaviors")."""
    from sibb_mock_site import get_site

    h = SafariHandler(reader=_Reader(sibb_udid))
    sid = f"l2-form-{uuid.uuid4().hex[:8]}"
    try:
        await h.apply({
            "type": "mock_site",
            "site_id": sid,
            "credentials": {"alice": "hunter2"},
        })
        # Fixture is up and registered.
        site = get_site(sid)
        assert site is not None, "mock site not registered after apply"
        # Reachable from the host as well as (presumably) from sim
        # Safari — confirm GET returns 200.
        with urllib.request.urlopen(site.login_url, timeout=3) as resp:
            assert resp.status == 200

        # Give Safari time to load the page.
        await asyncio.sleep(5.0)
        sr = await _safari_reader(sibb_udid)
        try:
            await _dismiss_safari_onboarding(sr)
            tree = await sr.read()
            labels = [(e.effective_label or "") for e in tree.elements]
            values = [(getattr(e, "value", None) or "")
                      for e in tree.elements]

            # Sign In button by label (the <h1> renders as StaticText
            # AND the <button> renders as Button — either suffices,
            # but the Button role is the one we care about).
            has_sign_in_button = any(
                e.effective_role == ElementRole.BUTTON
                and (e.effective_label or "").strip() == "Sign In"
                for e in tree.elements
            )
            assert has_sign_in_button, (
                f"no 'Sign In' Button in Safari AX tree. labels="
                f"{[l for l in labels if l][:25]}")

            # Username/Password TextFields surface placeholder as value.
            assert any("Username" in v for v in values), (
                f"no 'Username' placeholder field. values={values[:25]}")
            assert any("Password" in v for v in values), (
                f"no 'Password' placeholder field. values={values[:25]}")
        finally:
            await sr.stop()
    finally:
        await h.reset()
        # reset() must unregister the fixture and stop its server.
        assert get_site(sid) is None


async def test_mock_site_reset_stops_fixture_and_drops_registry(
        sibb_udid: str, reader):
    """`handler.reset()` must (a) unregister the fixture, (b) stop
    the HTTP server so the port no longer responds. Otherwise a
    long-running test suite leaks ports and stale registry entries
    collide on the next start()."""
    from sibb_mock_site import get_site

    h = SafariHandler(reader=_Reader(sibb_udid))
    sid = f"l2-reset-{uuid.uuid4().hex[:8]}"
    await h.apply({
        "type": "mock_site",
        "site_id": sid,
        "open_at_start": False,  # don't churn Safari for this test
    })
    site = get_site(sid)
    assert site is not None
    login_url = site.login_url
    # Sanity: fixture is reachable while running.
    with urllib.request.urlopen(login_url, timeout=3) as resp:
        assert resp.status == 200

    await h.reset()

    # Registry cleared.
    assert get_site(sid) is None
    # And the port is no longer listening — connection refused, not
    # a stale response. URLError wraps ConnectionRefusedError.
    with pytest.raises((urllib.error.URLError, ConnectionRefusedError,
                          OSError)):
        urllib.request.urlopen(login_url, timeout=2)


async def test_mock_site_form_submission_via_safari_records_password(
        sibb_udid: str, reader):
    """The verification surface this whole foundation exists for:
    type a sentinel password into Safari's rendered form, submit
    it, and confirm the mock site recorded the *plaintext* value.
    This is what the keychain encryption blocks at the keychain
    layer; the mock site closes the loop.

    Submission strategy: type the values, re-read AX (WebKit
    scrolls the focused form up so the submit button stays above
    the keyboard), then tap the Sign In Button by label. We
    tried `typeText(...+"\\n")` first — iOS's typeText crashes the
    XCUITest server on embedded newlines, so the keyboard-Return
    trick that works in pure Swift XCUI is off the table here."""
    from sibb_mock_site import get_site
    from sibb_verify import RESOURCE_FETCHERS

    h = SafariHandler(reader=_Reader(sibb_udid))
    sid = f"l2-submit-{uuid.uuid4().hex[:8]}"
    sentinel = f"SIBB-PW-{uuid.uuid4().hex[:10]}"
    try:
        await h.apply({
            "type": "mock_site",
            "site_id": sid,
            "credentials": {"alice": sentinel},
        })
        await asyncio.sleep(5.0)
        sr = await _safari_reader(sibb_udid)
        try:
            await _dismiss_safari_onboarding(sr)
            tree = await sr.read()

            username_field = next(
                (e for e in tree.elements
                 if e.effective_role == ElementRole.TEXT_FIELD
                 and (getattr(e, "value", None) or "") == "Username"),
                None)
            password_field = next(
                (e for e in tree.elements
                 if e.effective_role == ElementRole.TEXT_FIELD
                 and (getattr(e, "value", None) or "") == "Password"),
                None)
            assert username_field is not None, (
                "Username TextField not found in Safari AX tree")
            assert password_field is not None, (
                "Password TextField not found in Safari AX tree")

            # Type the username.
            await sr._xcuitest.tap(
                username_field.frame.center_x,
                username_field.frame.center_y)
            await asyncio.sleep(0.6)
            await sr._xcuitest.type_text("alice")
            await asyncio.sleep(0.3)

            # Re-locate the password field — focusing the username
            # shifted the layout when the keyboard rose.
            tree = await sr.read()
            password_field = next(
                (e for e in tree.elements
                 if e.effective_role == ElementRole.TEXT_FIELD
                 and (getattr(e, "value", None) or "") == "Password"),
                None)
            assert password_field is not None, (
                "Password field disappeared after typing username")
            await sr._xcuitest.tap(
                password_field.frame.center_x,
                password_field.frame.center_y)
            await asyncio.sleep(0.6)
            await sr._xcuitest.type_text(sentinel)
            await asyncio.sleep(0.5)

            # Re-read AX: WebKit has scrolled the focused form so
            # the submit button stays above the keyboard, so the
            # Sign In Button's frame is now in the visible area
            # and tappable.
            tree = await sr.read()
            sign_in = next(
                (e for e in tree.elements
                 if e.effective_role == ElementRole.BUTTON
                 and (e.effective_label or "").strip() == "Sign In"
                 and e.frame),
                None)
            assert sign_in is not None, (
                "Sign In Button not visible after focusing password "
                "field; AX tree had no tappable 'Sign In' Button")
            await sr._xcuitest.tap(
                sign_in.frame.center_x, sign_in.frame.center_y)
            # Give the POST + 302 redirect time to complete.
            await asyncio.sleep(3.0)
        finally:
            await sr.stop()

        # The mock site recorded the exact sentinel — proves the
        # password value made it through Safari's form to the
        # server in plaintext, with no keychain decryption needed.
        rows = await RESOURCE_FETCHERS["mock_site.submissions"](
            _Reader(sibb_udid),
            {"site_id": sid,
             "username": "alice",
             "password": sentinel,
             "success": True})
        assert len(rows) == 1, (
            f"expected exactly one successful submission for alice "
            f"with sentinel password={sentinel!r}; got rows={rows}")
    finally:
        await h.reset()
