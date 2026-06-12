"""SafariHandler — L1 + L1.5 tests.

Safari bookmarks live in `data/Library/Safari/Bookmarks.db` (host-
side SQLite, no UDF triggers). Unlike Messages (multi-store
filtered), bookmark inserts surface in the Safari UI directly —
empirically verified 2026-05-16.

Tests use real in-memory SQLite databases with the Bookmarks.db
schema rather than a fake-reader pattern, because the handler is
SQL-based and stdlib sqlite3 is the closest-to-real test surface.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import sibb_state
from sibb_spec import Bookmark, MockSite, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    SafariHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_safari_handler_registered_by_bundle_id():
    assert SafariHandler.bundle_id == "com.apple.mobilesafari"
    assert HANDLERS[SafariHandler.bundle_id] is SafariHandler


def test_safari_handler_no_tcc_services():
    """Safari operates on the public web — no SIBB-side TCC grant
    needed. Camera/Microphone for WebRTC would be Safari's concern,
    not the runner's."""
    assert SafariHandler.tcc_services == []


def test_safari_handler_is_not_a_pre_runner():
    assert SafariHandler.pre_runner is False
    assert SafariHandler.pre_runner_kinds == []


def test_safari_not_in_collect_tcc_services():
    services = collect_tcc_services()
    for s in services:
        assert "safari" not in s.lower()


def test_canonicalize_safari_friendly_name():
    assert canonicalize_app("Safari") == "com.apple.mobilesafari"
    assert canonicalize_app("safari") == "com.apple.mobilesafari"


# ─────────────────────────── Bookmark spec ───────────────────────────

def test_bookmark_spec_registered():
    assert ("Safari", "bookmark") in SPEC_TYPES
    assert SPEC_TYPES[("Safari", "bookmark")] is Bookmark


def test_bookmark_required_fields():
    b = Bookmark(title="Apple", url="https://www.apple.com/")
    assert b.title == "Apple"
    assert b.url == "https://www.apple.com/"


def test_bookmark_to_dict_canonical_shape():
    b = Bookmark(title="Apple", url="https://www.apple.com/")
    assert b.to_dict() == {
        "app": "Safari", "type": "bookmark",
        "title": "Apple", "url": "https://www.apple.com/",
        "folder": None,
    }


def test_bookmark_to_dict_includes_folder_when_set():
    b = Bookmark(title="Apple", url="https://www.apple.com/",
                  folder="Tech")
    assert b.to_dict() == {
        "app": "Safari", "type": "bookmark",
        "title": "Apple", "url": "https://www.apple.com/",
        "folder": "Tech",
    }


def test_bookmark_round_trip():
    original = Bookmark(title="Example", url="https://example.com")
    back = Bookmark.from_dict(original.to_dict())
    assert back == original


def test_validate_entry_accepts_bookmark():
    typed, err = validate_entry({
        "app": "Safari", "type": "bookmark",
        "title": "Apple", "url": "https://apple.com",
    })
    assert err is None
    assert isinstance(typed, Bookmark)


# ─────────────────────────── apply validates inputs ──────────────────

class _UdidStub:
    """Stand-in for XCUITestReader — exposes .udid only."""
    def __init__(self, udid: str = "FAKE-UDID"):
        self.udid = udid


async def test_apply_rejects_unknown_entry_kind():
    h = SafariHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "history", "url": "..."})


async def test_apply_requires_reader_with_udid():
    h = SafariHandler(reader=None)
    with pytest.raises(RuntimeError, match="requires a reader"):
        await h.apply({"type": "bookmark", "title": "x", "url": "y"})


async def test_apply_requires_title():
    h = SafariHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="title required"):
        await h.apply({"type": "bookmark", "url": "https://apple.com"})


async def test_apply_requires_url():
    h = SafariHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="url required"):
        await h.apply({"type": "bookmark", "title": "Apple"})


# ─────────────────── SQL helpers against in-memory DB ────────────────

def _create_test_bookmarks_db(path: Path) -> None:
    """Create a Bookmarks.db with the minimum schema SafariHandler
    needs: bookmarks table + a BookmarksBar row (special_id=1).
    Mirrors what Safari creates on first launch."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            special_id INTEGER DEFAULT 0,
            parent INTEGER,
            type INTEGER,
            title TEXT,
            url TEXT,
            num_children INTEGER DEFAULT 0,
            editable INTEGER DEFAULT 1,
            deletable INTEGER DEFAULT 1,
            hidden INTEGER DEFAULT 0,
            order_index INTEGER NOT NULL,
            external_uuid TEXT UNIQUE,
            added INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0
        );
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (0, NULL, 1, 'Root', 0);
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (1, 1, 1, 'BookmarksBar', 0);
    """)
    conn.commit()
    conn.close()


async def test_insert_helper_under_bookmarks_bar(tmp_path, monkeypatch):
    """The SQL helper writes to BookmarksBar (special_id=1) and
    returns the new row id."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    new_id = await sibb_state._safari_insert_bookmark(
        "FAKE-UDID", "Test Title", "https://test.example.com")
    assert new_id > 0

    # Verify the row landed under BookmarksBar.
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT title, url, parent, type FROM bookmarks WHERE id=?;",
        (new_id,)).fetchone()
    conn.close()
    assert row[0] == "Test Title"
    assert row[1] == "https://test.example.com"
    # parent should be the BookmarksBar id.
    assert row[3] == 0  # type=0 == leaf bookmark


async def test_insert_helper_order_index_monotonic(tmp_path, monkeypatch):
    """Successive inserts get monotonically-increasing order_index
    so Safari preserves insertion order on the start page."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    for i in range(3):
        await sibb_state._safari_insert_bookmark(
            "FAKE", f"Bookmark {i}", f"https://x.com/{i}")

    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT title, order_index FROM bookmarks "
        "WHERE type=0 ORDER BY order_index;").fetchall()
    conn.close()
    titles = [r[0] for r in rows]
    indices = [r[1] for r in rows]
    assert titles == ["Bookmark 0", "Bookmark 1", "Bookmark 2"]
    # Indices strictly increasing.
    assert indices == sorted(indices)
    assert len(set(indices)) == 3


async def test_insert_helper_raises_when_db_missing(tmp_path, monkeypatch):
    """If Safari has never been launched, Bookmarks.db doesn't
    exist. The helper must raise a clear message pointing at the
    fix (launch Safari during baseline prewarm)."""
    db = tmp_path / "NonexistentBookmarks.db"
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    with pytest.raises(RuntimeError, match="Bookmarks.db doesn't exist"):
        await sibb_state._safari_insert_bookmark(
            "FAKE", "x", "https://x.com")


async def test_list_helper_returns_user_bookmarks(tmp_path, monkeypatch):
    """list_bookmarks returns the leaf bookmarks under BookmarksBar
    (not the BookmarksBar folder itself, not Root)."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    for title, url in [("Apple", "https://apple.com"),
                         ("Bing", "https://bing.com")]:
        await sibb_state._safari_insert_bookmark("FAKE", title, url)

    rows = await sibb_state._safari_list_bookmarks("FAKE")
    titles = sorted(r["title"] for r in rows)
    assert titles == ["Apple", "Bing"]
    # The Root/BookmarksBar folder rows should NOT be in the result.
    assert "BookmarksBar" not in titles
    assert "Root" not in titles


async def test_list_helper_returns_empty_when_no_db(tmp_path, monkeypatch):
    """list_bookmarks is tolerant of the DB being absent — used by
    verifier fetchers that need "absent" to be a legitimate state
    rather than a fetch error."""
    db = tmp_path / "Nonexistent.db"
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))
    rows = await sibb_state._safari_list_bookmarks("FAKE")
    assert rows == []


async def test_handler_apply_inserts_via_helper(tmp_path, monkeypatch):
    """Full handler.apply path: spec entry → SQL insert."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    h = SafariHandler(reader=_UdidStub("FAKE"))
    await h.apply({"type": "bookmark",
                    "title": "via apply",
                    "url": "https://example.com"})

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT title, url FROM bookmarks WHERE title='via apply';"
    ).fetchone()
    conn.close()
    assert row == ("via apply", "https://example.com")


# ─────────────────── reset is documented no-op ───────────────────────

async def test_reset_is_a_documented_noop():
    """v1 doesn't track inserted bookmark ids. The no-op IS the
    contract — pin it so future refactors don't silently wipe the
    default bookmarks (Apple / Bing / Google / Yahoo) Safari ships."""
    h = SafariHandler(reader=_UdidStub())
    # Calling reset on a None reader must not crash — it shouldn't
    # touch the reader at all in v1.
    h.reader = None
    await h.reset()


# ─────────────────── resource fetcher wiring ─────────────────────────

def test_safari_bookmarks_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "safari.bookmarks" in RESOURCE_FETCHERS


async def test_fetcher_returns_inserted_rows(tmp_path, monkeypatch):
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    h = SafariHandler(reader=_UdidStub("FAKE"))
    await h.apply({"type": "bookmark",
                    "title": "Apple",
                    "url": "https://apple.com"})

    fetcher = RESOURCE_FETCHERS["safari.bookmarks"]
    rows = await fetcher(_UdidStub("FAKE"), {})
    assert len(rows) == 1
    assert rows[0]["title"] == "Apple"
    assert rows[0]["url"] == "https://apple.com"


async def test_fetcher_filters_by_title(tmp_path, monkeypatch):
    """Selector-side filtering — the fetcher passes selectors through
    _filter_records, so `{"title": "Apple"}` narrows the result."""
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    h = SafariHandler(reader=_UdidStub("FAKE"))
    for title, url in [("Apple", "https://apple.com"),
                         ("Bing", "https://bing.com")]:
        await h.apply({"type": "bookmark", "title": title, "url": url})

    fetcher = RESOURCE_FETCHERS["safari.bookmarks"]
    rows = await fetcher(_UdidStub("FAKE"), {"title": "Apple"})
    assert len(rows) == 1
    assert rows[0]["title"] == "Apple"


async def test_fetcher_requires_udid():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError

    class _NoUdid:
        pass
    fetcher = RESOURCE_FETCHERS["safari.bookmarks"]
    with pytest.raises(ResourceFetchError, match=".udid"):
        await fetcher(_NoUdid(), {})


# ─────────────────────────── MockSite spec ───────────────────────────
#
# A SafariHandler `mock_site` spec entry spins up the host-side HTTP
# fixture from `sibb_mock_site.py` and (by default) navigates Safari
# to its login URL. The handler owns the fixture lifecycle so reset()
# stops it; tests below cover both pieces in isolation.

def test_mock_site_spec_registered():
    assert ("Safari", "mock_site") in SPEC_TYPES
    assert SPEC_TYPES[("Safari", "mock_site")] is MockSite


def test_mock_site_defaults_credentials_to_empty_dict():
    """Frozen-dataclass default-factory must yield a fresh dict per
    instance — otherwise two specs would share the same dict and
    mutating one would corrupt the other."""
    s1 = MockSite(site_id="a")
    s2 = MockSite(site_id="b")
    assert s1.credentials == {}
    assert s2.credentials == {}
    assert s1.credentials is not s2.credentials


def test_mock_site_to_dict_canonical_shape():
    spec = MockSite(
        site_id="ep-42",
        credentials={"alice": "hunter2"},
        open_at_start=True,
    )
    d = spec.to_dict()
    assert d["app"] == "Safari"
    assert d["type"] == "mock_site"
    assert d["site_id"] == "ep-42"
    assert d["credentials"] == {"alice": "hunter2"}
    assert d["open_at_start"] is True
    assert d["sign_in_path"] == "/login"
    assert d["sign_up_path"] == "/signup"


def test_mock_site_round_trip():
    original = MockSite(
        site_id="ep-7",
        credentials={"u": "p"},
        open_at_start=False,
        sign_in_path="/auth",
        sign_up_path="/register",
    )
    back = MockSite.from_dict(original.to_dict())
    assert back == original


def test_validate_entry_accepts_mock_site():
    typed, err = validate_entry({
        "app": "Safari", "type": "mock_site",
        "site_id": "ep-1",
        "credentials": {"alice": "hunter2"},
    })
    assert err is None
    assert isinstance(typed, MockSite)
    assert typed.site_id == "ep-1"
    assert typed.credentials == {"alice": "hunter2"}


# ─────────────────────── handler.apply(mock_site) ─────────────────────


@pytest.fixture
def patched_safari(monkeypatch):
    """Replace simctl-touching helpers with recorders so handler
    tests can run without a simulator. Returns the recorder dict."""
    calls = {"terminate": [], "openurl": []}

    async def fake_terminate(udid):
        calls["terminate"].append(udid)

    def fake_open_in_safari(udid, url, *, timeout=10.0):
        calls["openurl"].append((udid, url))

    monkeypatch.setattr(sibb_state, "_safari_terminate", fake_terminate)

    import sibb_mock_site
    monkeypatch.setattr(sibb_mock_site, "open_in_safari", fake_open_in_safari)

    return calls


async def test_apply_mock_site_starts_and_registers(patched_safari):
    """apply(mock_site) creates a running MockSite and registers it
    in the process-global registry, where the verifier looks it up."""
    from sibb_mock_site import get_site

    h = SafariHandler(reader=_UdidStub("FAKE"))
    site_id = f"test-{id(h)}"
    try:
        await h.apply({
            "type": "mock_site",
            "site_id": site_id,
            "credentials": {"alice": "hunter2"},
        })
        assert get_site(site_id) is not None
        assert get_site(site_id).credentials == {"alice": "hunter2"}
        assert len(h._mock_sites) == 1
        assert h._mock_sites[0].site_id == site_id
    finally:
        await h.reset()


async def test_apply_mock_site_open_at_start_navigates_safari(
        patched_safari):
    """Default open_at_start=True: handler terminates Safari and
    opens the login URL via simctl."""
    h = SafariHandler(reader=_UdidStub("FAKE-UDID"))
    site_id = f"test-{id(h)}"
    try:
        await h.apply({
            "type": "mock_site",
            "site_id": site_id,
            "credentials": {"alice": "hunter2"},
        })
        # Terminate happened once before openurl.
        assert "FAKE-UDID" in patched_safari["terminate"]
        assert len(patched_safari["openurl"]) == 1
        udid, url = patched_safari["openurl"][0]
        assert udid == "FAKE-UDID"
        assert url.endswith("/login")
        assert url.startswith("http://127.0.0.1:")
    finally:
        await h.reset()


async def test_apply_mock_site_open_at_start_false_skips_simctl(
        patched_safari):
    """When open_at_start=False the agent is expected to navigate
    manually; the fixture is up but Safari isn't touched."""
    h = SafariHandler(reader=_UdidStub("FAKE"))
    site_id = f"test-{id(h)}"
    try:
        await h.apply({
            "type": "mock_site",
            "site_id": site_id,
            "open_at_start": False,
        })
        assert patched_safari["openurl"] == []
        # And terminate is also skipped — no Safari to clear.
        assert patched_safari["terminate"] == []
    finally:
        await h.reset()


async def test_apply_mock_site_open_at_start_requires_udid(patched_safari):
    h = SafariHandler(reader=None)
    site_id = f"test-{id(h)}"
    try:
        with pytest.raises(RuntimeError, match=r".udid"):
            await h.apply({
                "type": "mock_site",
                "site_id": site_id,
                "open_at_start": True,
            })
        # Even though the open failed, the site DID start before the
        # raise — verify it's tracked so reset() will clean it up.
        assert len(h._mock_sites) == 1
    finally:
        await h.reset()


async def test_apply_mock_site_requires_site_id(patched_safari):
    h = SafariHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="site_id required"):
        await h.apply({"type": "mock_site"})
    with pytest.raises(ValueError, match="site_id required"):
        await h.apply({"type": "mock_site", "site_id": ""})
    with pytest.raises(ValueError, match="site_id required"):
        await h.apply({"type": "mock_site", "site_id": 42})


async def test_apply_mock_site_rejects_non_dict_credentials(patched_safari):
    h = SafariHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="credentials must be a dict"):
        await h.apply({
            "type": "mock_site",
            "site_id": "x",
            "credentials": ["alice", "hunter2"],
        })


async def test_apply_mock_site_rejects_non_string_credential_pairs(
        patched_safari):
    h = SafariHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="must be str"):
        await h.apply({
            "type": "mock_site",
            "site_id": "x",
            "credentials": {"alice": 12345},
        })


async def test_apply_mock_site_custom_paths_propagate(patched_safari):
    h = SafariHandler(reader=_UdidStub("FAKE"))
    site_id = f"test-{id(h)}"
    try:
        await h.apply({
            "type": "mock_site",
            "site_id": site_id,
            "open_at_start": False,
            "sign_in_path": "/auth/in",
            "sign_up_path": "/auth/up",
        })
        site = h._mock_sites[0]
        assert site.sign_in_path == "/auth/in"
        assert site.sign_up_path == "/auth/up"
        assert site.login_url.endswith("/auth/in")
        assert site.signup_url.endswith("/auth/up")
    finally:
        await h.reset()


async def test_apply_multiple_mock_sites_in_one_episode(patched_safari):
    """An episode may want two fixtures (e.g. one to save creds for,
    another to use them on). Both must be tracked and torn down."""
    from sibb_mock_site import get_site

    h = SafariHandler(reader=_UdidStub("FAKE"))
    ids = [f"test-{id(h)}-a", f"test-{id(h)}-b"]
    try:
        for sid in ids:
            await h.apply({
                "type": "mock_site",
                "site_id": sid,
                "open_at_start": False,
            })
        assert len(h._mock_sites) == 2
        for sid in ids:
            assert get_site(sid) is not None
    finally:
        await h.reset()
    for sid in ids:
        assert get_site(sid) is None


# ─────────────────────── reset() owns the lifecycle ───────────────────


async def test_reset_stops_and_unregisters_all_mock_sites(patched_safari):
    from sibb_mock_site import get_site

    h = SafariHandler(reader=_UdidStub("FAKE"))
    site_id = f"test-{id(h)}"
    await h.apply({
        "type": "mock_site",
        "site_id": site_id,
        "open_at_start": False,
    })
    assert get_site(site_id) is not None

    await h.reset()
    assert get_site(site_id) is None
    assert h._mock_sites == []


async def test_reset_terminates_safari_when_sites_present(patched_safari):
    h = SafariHandler(reader=_UdidStub("FAKE-UDID"))
    site_id = f"test-{id(h)}"
    await h.apply({
        "type": "mock_site",
        "site_id": site_id,
        "open_at_start": False,
    })
    # Sanity: open_at_start=False didn't terminate.
    assert patched_safari["terminate"] == []

    await h.reset()
    # reset() terminates once on its way to shutting fixtures down.
    assert patched_safari["terminate"] == ["FAKE-UDID"]


async def test_reset_without_mock_sites_is_still_noop():
    """v1's "don't wipe Safari's default bookmarks" contract: with
    no mock sites in flight, reset() must touch nothing — not even
    a Safari terminate. Future-us: don't quietly add a terminate
    here without thinking about it."""
    h = SafariHandler(reader=_UdidStub())
    h.reader = None
    await h.reset()  # must not raise


async def test_reset_swallows_stop_failures(patched_safari, monkeypatch):
    """One fixture failing to stop() must not prevent the other
    fixtures from being torn down. Otherwise a stale registry
    entry leaks across episodes and the next start() collides."""
    from sibb_mock_site import get_site

    h = SafariHandler(reader=_UdidStub("FAKE"))
    sid_a = f"test-{id(h)}-a"
    sid_b = f"test-{id(h)}-b"
    await h.apply({"type": "mock_site",
                    "site_id": sid_a, "open_at_start": False})
    await h.apply({"type": "mock_site",
                    "site_id": sid_b, "open_at_start": False})

    # Make the LAST-spawned site's stop() raise (reset pops in
    # reverse order, so site_b is torn down first).
    def boom():
        raise RuntimeError("simulated socket close failure")
    h._mock_sites[-1].stop = boom

    await h.reset()
    # The healthy site must still be unregistered.
    assert get_site(sid_a) is None
    # The failing site's registry entry IS left behind (it raised
    # before _REGISTRY.pop ran inside stop()); we clean it ourselves
    # in test teardown so subsequent tests aren't poisoned.
    import sibb_mock_site
    sibb_mock_site._REGISTRY.pop(sid_b, None)


# ─────────────────────── bookmark path regression ─────────────────────


async def test_bookmark_path_still_works_after_handler_split(
        tmp_path, monkeypatch):
    """Refactor regression: the bookmark code path moved into
    `_apply_bookmark`. Walk through the public apply() to be sure
    routing still hits it."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    h = SafariHandler(reader=_UdidStub("FAKE"))
    await h.apply({"type": "bookmark",
                    "title": "Apple",
                    "url": "https://apple.com"})
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT title, url FROM bookmarks WHERE title='Apple';"
    ).fetchone()
    conn.close()
    assert row == ("Apple", "https://apple.com")


# ─────────────────── folder-tree + Reading List + canon ───────────────
#
# These tests cover the 2026-06-01 fixes to address the critic findings:
# (a) _safari_list_bookmarks now walks subfolders; (b) Reading List can
# be included via include_reading_list; (c) URLs can be canonicalized.


def _create_test_bookmarks_db_with_folders_and_rl(path: Path) -> None:
    """Bookmarks.db with: BookmarksBar root, a "Recipes" subfolder
    under it (with a leaf bookmark inside), a leaf at the root, and a
    Reading List parent (special_id=3, the iOS 26 value) with one
    Reading List item."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            special_id INTEGER DEFAULT 0,
            parent INTEGER,
            type INTEGER,
            title TEXT,
            url TEXT,
            num_children INTEGER DEFAULT 0,
            editable INTEGER DEFAULT 1,
            deletable INTEGER DEFAULT 1,
            hidden INTEGER DEFAULT 0,
            order_index INTEGER NOT NULL,
            external_uuid TEXT UNIQUE,
            added INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0
        );
        -- Root folder (id=1).
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (0, NULL, 1, 'Root', 0);
        -- BookmarksBar (id=2, special_id=1).
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (1, 1, 1, 'BookmarksBar', 0);
        -- "Recipes" subfolder under BookmarksBar (id=3).
        INSERT INTO bookmarks (special_id, parent, type, title, order_index,
                                external_uuid)
        VALUES (0, 2, 1, 'Recipes', 0, 'FOLDER-RECIPES');
        -- Bookmark directly under BookmarksBar (id=4).
        INSERT INTO bookmarks (special_id, parent, type, title, url,
                                order_index, external_uuid)
        VALUES (0, 2, 0, 'Apple', 'https://apple.com', 1, 'BM-APPLE');
        -- Bookmark inside the Recipes folder (id=5).
        INSERT INTO bookmarks (special_id, parent, type, title, url,
                                order_index, external_uuid)
        VALUES (0, 3, 0, 'Carbonara', 'https://example.com/carbonara',
                0, 'BM-CARBO');
        -- Reading List parent (id=6, special_id=3, the iOS 26 value).
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (3, 1, 1, 'com.apple.ReadingList', 0);
        -- Reading List item (id=7).
        INSERT INTO bookmarks (special_id, parent, type, title, url,
                                order_index, external_uuid)
        VALUES (0, 6, 0, 'Saved Article', 'https://news.example/saved',
                0, 'RL-1');
    """)
    conn.commit()
    conn.close()


async def test_list_bookmarks_walks_subfolders(tmp_path, monkeypatch):
    """The fixed fetcher recurses through subfolders under
    BookmarksBar so bookmarks the agent files inside e.g. "Recipes"
    don't silently disappear."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db_with_folders_and_rl(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    rows = await sibb_state._safari_list_bookmarks("FAKE")
    titles = sorted(r["title"] for r in rows)
    # Apple (root) + Carbonara (under Recipes). Reading List is OFF
    # by default.
    assert titles == ["Apple", "Carbonara"]
    # Folder path is populated.
    carbonara = next(r for r in rows if r["title"] == "Carbonara")
    assert carbonara["folder_path"] == ["Favorites", "Recipes"]
    assert carbonara["parent_title"] == "Recipes"
    assert carbonara["kind"] == "bookmark"


async def test_list_bookmarks_without_subfolders(tmp_path, monkeypatch):
    """When include_subfolders=False, only direct BookmarksBar
    leaves come back."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db_with_folders_and_rl(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    rows = await sibb_state._safari_list_bookmarks(
        "FAKE", include_subfolders=False)
    titles = sorted(r["title"] for r in rows)
    assert titles == ["Apple"]


async def test_list_bookmarks_includes_reading_list_on_demand(
        tmp_path, monkeypatch):
    """include_reading_list=True merges Reading List entries with
    bookmarks, distinguished by `kind`."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db_with_folders_and_rl(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    rows = await sibb_state._safari_list_bookmarks(
        "FAKE", include_reading_list=True)
    kinds = {(r["title"], r["kind"]) for r in rows}
    assert kinds == {
        ("Apple", "bookmark"),
        ("Carbonara", "bookmark"),
        ("Saved Article", "reading_list"),
    }


async def test_list_bookmarks_folder_filter(tmp_path, monkeypatch):
    """`parent_filter="Recipes"` restricts to bookmarks whose
    ancestry includes that folder."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db_with_folders_and_rl(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    rows = await sibb_state._safari_list_bookmarks(
        "FAKE", parent_filter="Recipes")
    assert [r["title"] for r in rows] == ["Carbonara"]


async def test_fetch_safari_bookmarks_selector_threads_folder(
        tmp_path, monkeypatch):
    """Selector-level `folder` propagates into the fetcher's
    parent_filter — was a dead parameter in the original code."""
    import asyncio
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db_with_folders_and_rl(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))
    from sibb_verify import RESOURCE_FETCHERS
    fetcher = RESOURCE_FETCHERS["safari.bookmarks"]
    rows = await fetcher(_UdidStub("FAKE"), {"folder": "Recipes"})
    assert [r["title"] for r in rows] == ["Carbonara"]


async def test_fetch_safari_bookmarks_canonicalize_url(
        tmp_path, monkeypatch):
    """When url_canonicalize=True, both selector and row URLs are
    normalized before compare — bare-host trailing-slash + uppercase
    host should still match."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db_with_folders_and_rl(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))
    from sibb_verify import RESOURCE_FETCHERS
    fetcher = RESOURCE_FETCHERS["safari.bookmarks"]
    rows = await fetcher(_UdidStub("FAKE"),
                          {"url_canonicalize": True,
                           "url": "HTTPS://Apple.com/"})
    assert len(rows) == 1
    assert rows[0]["title"] == "Apple"


def test_canonicalize_url_idempotent():
    """Running canonicalization twice gives the same result."""
    from sibb_verify import _canonicalize_url
    inputs = ["https://Apple.com/", "http://example.com:80/",
              "https://example.com:443/path",
              "example.com", "https://EXAMPLE.com/PATH?q=1"]
    for u in inputs:
        once = _canonicalize_url(u)
        twice = _canonicalize_url(once)
        assert once == twice, f"non-idempotent on {u!r}: {once!r} → {twice!r}"


def test_canonicalize_url_lowercases_host_only():
    """Host gets lowercased; path is preserved as-is."""
    from sibb_verify import _canonicalize_url
    assert (_canonicalize_url("HTTPS://Apple.COM/About")
            == "https://apple.com/About")


def test_canonicalize_url_drops_default_ports():
    from sibb_verify import _canonicalize_url
    assert _canonicalize_url("http://example.com:80/x") == "http://example.com/x"
    assert _canonicalize_url("https://example.com:443/") == "https://example.com"


def test_canonicalize_url_preserves_nonstandard_ports():
    from sibb_verify import _canonicalize_url
    assert (_canonicalize_url("http://127.0.0.1:8765/login")
            == "http://127.0.0.1:8765/login")


def test_canonicalize_url_handles_missing_scheme():
    from sibb_verify import _canonicalize_url
    assert _canonicalize_url("apple.com") == "http://apple.com"
    assert _canonicalize_url("apple.com/about") == "http://apple.com/about"


def test_canonicalize_url_drops_trailing_slash_on_bare_host():
    from sibb_verify import _canonicalize_url
    assert _canonicalize_url("https://apple.com/") == "https://apple.com"
    # But keeps it on paths.
    assert (_canonicalize_url("https://apple.com/about/")
            == "https://apple.com/about/")


def test_canonicalize_url_passes_through_non_url_strings():
    from sibb_verify import _canonicalize_url
    assert _canonicalize_url("") == ""
    assert _canonicalize_url("not a url") == "not a url"
    assert _canonicalize_url(None) is None


# ─────────────────── insert_bookmark with folder ──────────────────────


async def test_insert_bookmark_with_folder_creates_subfolder(
        tmp_path, monkeypatch):
    """`folder="Tech"` creates BookmarksBar/Tech on demand and
    puts the bookmark inside it."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    await sibb_state._safari_insert_bookmark(
        "FAKE", "GitHub", "https://github.com", folder="Tech")
    conn = sqlite3.connect(str(db))
    folder_id = conn.execute(
        "SELECT id FROM bookmarks "
        "WHERE title='Tech' AND type=1 AND deleted=0;").fetchone()[0]
    bookmark_row = conn.execute(
        "SELECT title, url, type, parent FROM bookmarks "
        "WHERE title='GitHub' AND deleted=0;").fetchone()
    conn.close()
    assert bookmark_row[0] == "GitHub"
    assert bookmark_row[1] == "https://github.com"
    assert bookmark_row[2] == 0  # leaf
    assert bookmark_row[3] == folder_id  # parent is the Tech folder


async def test_insert_bookmark_with_existing_folder_reuses_it(
        tmp_path, monkeypatch):
    """A second bookmark with `folder="Tech"` lands in the same
    folder as the first (no duplicate folder created)."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    await sibb_state._safari_insert_bookmark(
        "FAKE", "GitHub", "https://github.com", folder="Tech")
    await sibb_state._safari_insert_bookmark(
        "FAKE", "GitLab", "https://gitlab.com", folder="Tech")
    conn = sqlite3.connect(str(db))
    folder_count = conn.execute(
        "SELECT COUNT(*) FROM bookmarks "
        "WHERE title='Tech' AND type=1 AND deleted=0;").fetchone()[0]
    bookmark_count = conn.execute(
        "SELECT COUNT(*) FROM bookmarks "
        "WHERE type=0 AND deleted=0;").fetchone()[0]
    conn.close()
    assert folder_count == 1
    assert bookmark_count == 2


async def test_apply_threads_folder_into_insert(tmp_path, monkeypatch):
    """The Bookmark spec's `folder` flows from apply() → insert."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    h = SafariHandler(reader=_UdidStub("FAKE"))
    await h.apply({"type": "bookmark",
                    "title": "GitHub",
                    "url": "https://github.com",
                    "folder": "Tech"})
    conn = sqlite3.connect(str(db))
    folder_present = conn.execute(
        "SELECT COUNT(*) FROM bookmarks "
        "WHERE title='Tech' AND type=1 AND deleted=0;").fetchone()[0]
    conn.close()
    assert folder_present == 1


# ─────────────────── P0/P1 follow-up tests (2026-06-03) ───────────────
#
# Added per the post-fixes critic round: lock in semantics that would
# otherwise drift silently on future refactors.


def _create_db_with_two_recipes_folders(path: Path) -> None:
    """DB with TWO "Recipes" folders at different depths.
    Tree:
      Root (1)
      ├── BookmarksBar / "Favorites" (2, special_id=1)
      │   ├── "Recipes" (3) — direct subfolder
      │   │   └── leaf "Carbonara" (4)
      │   └── "Italian" (5)
      │       └── "Recipes" (6) — nested deeper
      │           └── leaf "Lasagna" (7)
    """
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            special_id INTEGER DEFAULT 0,
            parent INTEGER,
            type INTEGER,
            title TEXT,
            url TEXT,
            num_children INTEGER DEFAULT 0,
            editable INTEGER DEFAULT 1,
            deletable INTEGER DEFAULT 1,
            hidden INTEGER DEFAULT 0,
            order_index INTEGER NOT NULL,
            external_uuid TEXT UNIQUE,
            added INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0
        );
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (0, NULL, 1, 'Root', 0);
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (1, 1, 1, 'BookmarksBar', 0);
        INSERT INTO bookmarks (special_id, parent, type, title, order_index,
                                external_uuid)
        VALUES (0, 2, 1, 'Recipes', 0, 'FOLDER-RECIPES-TOP');
        INSERT INTO bookmarks (special_id, parent, type, title, url,
                                order_index, external_uuid)
        VALUES (0, 3, 0, 'Carbonara', 'https://example.com/carbonara',
                0, 'BM-CARBO');
        INSERT INTO bookmarks (special_id, parent, type, title, order_index,
                                external_uuid)
        VALUES (0, 2, 1, 'Italian', 1, 'FOLDER-ITALIAN');
        INSERT INTO bookmarks (special_id, parent, type, title, order_index,
                                external_uuid)
        VALUES (0, 5, 1, 'Recipes', 0, 'FOLDER-RECIPES-NESTED');
        INSERT INTO bookmarks (special_id, parent, type, title, url,
                                order_index, external_uuid)
        VALUES (0, 6, 0, 'Lasagna', 'https://example.com/lasagna',
                0, 'BM-LASAGNA');
    """)
    conn.commit()
    conn.close()


async def test_parent_filter_recipes_matches_ancestor_anywhere(
        tmp_path, monkeypatch):
    """Pin: `parent_filter="Recipes"` matches any leaf whose path
    contains a folder named "Recipes" — either as immediate parent
    OR as a higher ancestor. With two distinct "Recipes" folders in
    the tree, BOTH leaves under them are returned. This is the
    "Recipes anywhere in ancestry" semantics, deliberately chosen
    over "immediate parent only". A future narrow-the-match refactor
    must update this test and the docstring of
    `_safari_list_bookmarks.parent_filter`.
    """
    db = tmp_path / "Bookmarks.db"
    _create_db_with_two_recipes_folders(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    rows = await sibb_state._safari_list_bookmarks(
        "FAKE", parent_filter="Recipes")
    titles = sorted(r["title"] for r in rows)
    # Both Carbonara (direct Recipes parent) AND Lasagna (Recipes
    # ancestor through Italian) come back.
    assert titles == ["Carbonara", "Lasagna"]
    # parent_filter matching is case-insensitive.
    rows_lower = await sibb_state._safari_list_bookmarks(
        "FAKE", parent_filter="recipes")
    assert sorted(r["title"] for r in rows_lower) == titles


# ─────────────────── nested folders >1 level (P1 #6) ──────────────────


def _create_db_with_3deep_folders(path: Path) -> None:
    """Favorites > Tech > AI > leaf "GPT-4". Tests the BFS frontier
    walk in `_safari_list_bookmarks` past one level."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            special_id INTEGER DEFAULT 0,
            parent INTEGER,
            type INTEGER,
            title TEXT,
            url TEXT,
            num_children INTEGER DEFAULT 0,
            editable INTEGER DEFAULT 1,
            deletable INTEGER DEFAULT 1,
            hidden INTEGER DEFAULT 0,
            order_index INTEGER NOT NULL,
            external_uuid TEXT UNIQUE,
            added INTEGER DEFAULT 1,
            deleted INTEGER DEFAULT 0
        );
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (0, NULL, 1, 'Root', 0);
        INSERT INTO bookmarks (special_id, parent, type, title, order_index)
        VALUES (1, 1, 1, 'BookmarksBar', 0);
        INSERT INTO bookmarks (special_id, parent, type, title, order_index, external_uuid)
        VALUES (0, 2, 1, 'Tech', 0, 'FOLDER-TECH');
        INSERT INTO bookmarks (special_id, parent, type, title, order_index, external_uuid)
        VALUES (0, 3, 1, 'AI', 0, 'FOLDER-AI');
        INSERT INTO bookmarks (special_id, parent, type, title, url, order_index, external_uuid)
        VALUES (0, 4, 0, 'GPT-4', 'https://example.com/gpt4', 0, 'BM-GPT4');
    """)
    conn.commit()
    conn.close()


async def test_list_bookmarks_walks_three_levels(tmp_path, monkeypatch):
    """BFS frontier must descend past one level. A leaf nested three
    folders deep surfaces with the full folder_path."""
    db = tmp_path / "Bookmarks.db"
    _create_db_with_3deep_folders(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    rows = await sibb_state._safari_list_bookmarks("FAKE")
    assert [r["title"] for r in rows] == ["GPT-4"]
    assert rows[0]["folder_path"] == ["Favorites", "Tech", "AI"]


# ─────────────────── cycle guard (P1 #7) ──────────────────────────────


async def test_bookmark_path_cycle_guard_terminates(tmp_path, monkeypatch):
    """_safari_bookmark_path has a `seen: Set[int]` guard that should
    fire if a malformed DB has a parent-cycle. Construct a cycle
    artificially and confirm the walker returns within bounded
    steps."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    # Inject two folders A (id=3) and B (id=4) under BookmarksBar
    # (id=2), then mutate B.parent → A and A.parent → B (cycle).
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        INSERT INTO bookmarks (special_id, parent, type, title, order_index, external_uuid)
        VALUES (0, 2, 1, 'A', 0, 'FOLDER-A');
        INSERT INTO bookmarks (special_id, parent, type, title, order_index, external_uuid)
        VALUES (0, 2, 1, 'B', 1, 'FOLDER-B');
        UPDATE bookmarks SET parent=4 WHERE id=3;
        UPDATE bookmarks SET parent=3 WHERE id=4;
        INSERT INTO bookmarks (special_id, parent, type, title, url, order_index, external_uuid)
        VALUES (0, 3, 0, 'CycleLeaf', 'https://example.com/cycle', 0, 'BM-CYCLE');
    """)
    conn.commit()
    leaf_id = conn.execute(
        "SELECT id FROM bookmarks WHERE title='CycleLeaf';").fetchone()[0]

    # Call directly with a real connection — must return in bounded time,
    # not infinite loop.
    import time as _time
    t0 = _time.monotonic()
    path = sibb_state._safari_bookmark_path(conn, leaf_id)
    elapsed = _time.monotonic() - t0
    conn.close()

    # Cycle: A→B→A→... — the seen set caps the walk at 2 unique ids
    # before stopping. Output is bounded; doesn't matter exactly what
    # the path looks like — just that we got out alive.
    assert elapsed < 1.0, f"cycle walker took {elapsed:.3f}s — guard didn't fire"
    assert isinstance(path, list)
    # Path length is bounded by the seen-set logic.
    assert len(path) <= 3


# ─────────────────── _canonicalize_url edge cases (P1 #8) ─────────────


def test_canonicalize_url_preserves_fragment():
    from sibb_verify import _canonicalize_url
    assert (_canonicalize_url("https://example.com/page#section-3")
            == "https://example.com/page#section-3")


def test_canonicalize_url_preserves_query():
    from sibb_verify import _canonicalize_url
    assert (_canonicalize_url("https://example.com/search?q=hi&n=10")
            == "https://example.com/search?q=hi&n=10")


def test_canonicalize_url_preserves_fragment_and_query():
    from sibb_verify import _canonicalize_url
    assert (_canonicalize_url("https://example.com/?q=1#top")
            == "https://example.com/?q=1#top")


def test_canonicalize_url_passes_through_mailto():
    from sibb_verify import _canonicalize_url
    # mailto: has no netloc — function returns original by design.
    assert (_canonicalize_url("mailto:alice@example.com")
            == "mailto:alice@example.com")


def test_canonicalize_url_passes_through_tel():
    from sibb_verify import _canonicalize_url
    assert (_canonicalize_url("tel:+15551212")
            == "tel:+15551212")


def test_canonicalize_url_passes_through_file_url():
    from sibb_verify import _canonicalize_url
    # file:// usually has empty netloc; canonicalizer returns input.
    out = _canonicalize_url("file:///etc/passwd")
    assert out == "file:///etc/passwd"


# ─────────────────── folder= insert policies (P1 #9) ──────────────────


async def test_insert_bookmark_folder_is_case_sensitive(
        tmp_path, monkeypatch):
    """Pin: folder match for lookup-or-create is CASE-SENSITIVE.
    `folder="Tech"` and `folder="tech"` create TWO distinct folders.
    Matches iOS Safari's UI behavior where folder names are user data
    and "Tech" / "tech" are different to the user. A future case-
    insensitive change must update this test deliberately."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    await sibb_state._safari_insert_bookmark(
        "FAKE", "X", "https://x.com", folder="Tech")
    await sibb_state._safari_insert_bookmark(
        "FAKE", "Y", "https://y.com", folder="tech")
    conn = sqlite3.connect(str(db))
    folders = conn.execute(
        "SELECT title FROM bookmarks "
        "WHERE type=1 AND deleted=0 AND title IN ('Tech', 'tech') "
        "ORDER BY title;").fetchall()
    conn.close()
    assert [t[0] for t in folders] == ["Tech", "tech"]


async def test_insert_bookmark_folder_with_slash_is_literal_name(
        tmp_path, monkeypatch):
    """Pin: folder='Tech/AI' creates ONE folder literally named
    "Tech/AI" (no hierarchy interpretation). For nested folders,
    callers must layer their own logic — the handler does not parse
    `/` as a path separator."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    await sibb_state._safari_insert_bookmark(
        "FAKE", "GPT", "https://gpt", folder="Tech/AI")
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT title FROM bookmarks "
        "WHERE type=1 AND deleted=0 AND title LIKE 'Tech%';").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["Tech/AI"]


async def test_insert_bookmark_empty_folder_string_treated_as_no_folder(
        tmp_path, monkeypatch):
    """`folder=""` is falsy in Python → handler treats it as
    no-folder. Bookmark lands directly under BookmarksBar; no empty-
    named folder is created."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    await sibb_state._safari_insert_bookmark(
        "FAKE", "X", "https://x.com", folder="")
    conn = sqlite3.connect(str(db))
    bookmarksbar_id = conn.execute(
        "SELECT id FROM bookmarks WHERE special_id=1;").fetchone()[0]
    leaf_parent = conn.execute(
        "SELECT parent FROM bookmarks WHERE title='X';").fetchone()[0]
    empty_folders = conn.execute(
        "SELECT COUNT(*) FROM bookmarks "
        "WHERE type=1 AND title='';").fetchone()[0]
    conn.close()
    assert leaf_parent == bookmarksbar_id
    assert empty_folders == 0


async def test_apply_with_folder_twice_plus_no_folder_coexist(
        tmp_path, monkeypatch):
    """Multiple bookmarks with the same `folder` reuse one folder;
    a bookmark without `folder` lives directly under BookmarksBar.
    All three coexist after the apply sequence."""
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", noop_terminate)

    h = SafariHandler(reader=_UdidStub("FAKE"))
    await h.apply({"type": "bookmark", "title": "T1",
                    "url": "https://t1", "folder": "Tech"})
    await h.apply({"type": "bookmark", "title": "T2",
                    "url": "https://t2", "folder": "Tech"})
    await h.apply({"type": "bookmark", "title": "Root1",
                    "url": "https://root1"})

    rows = await sibb_state._safari_list_bookmarks("FAKE")
    titles = sorted(r["title"] for r in rows)
    assert titles == ["Root1", "T1", "T2"]
    # T1, T2 share a single folder; Root1 is directly under Favorites.
    t1 = next(r for r in rows if r["title"] == "T1")
    t2 = next(r for r in rows if r["title"] == "T2")
    root1 = next(r for r in rows if r["title"] == "Root1")
    assert t1["parent_id"] == t2["parent_id"]
    assert t1["folder_path"] == ["Favorites", "Tech"]
    assert root1["folder_path"] == ["Favorites"]


# ─────────────────── probe smoke (P2 #15) ─────────────────────────────


def test_safari_body_probe_importable():
    """The probe is intended to be re-run as a regression check on
    iOS bumps. Confirm it imports cleanly and exposes `main` — a tiny
    smoke that catches accidental syntax breakage."""
    import importlib
    spec = importlib.util.spec_from_file_location(
        "sibb_probe_safari_body_reader",
        str(Path(__file__).resolve().parents[2]
            / "simulator" / "sibb_probe_safari_body_reader.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "main"), "probe must expose `main` entrypoint"
    assert callable(mod.main)
