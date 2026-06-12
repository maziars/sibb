"""gen_safari_bookmark_specific_url — L1 round-trip test.

Mirrors the test_tier1_reminders pattern but lives on the host-side
SQLite surface (Safari bookmarks live in `Bookmarks.db`, not on the
XCUITest socket).

For each seed:
  1. Spec validates.
  2. Apply the spec → distractor bookmarks land under BookmarksBar.
  3. Run the verifier BEFORE the agent acts → must FAIL blocking
     (target URL isn't bookmarked, count is off by one).
  4. Simulate the agent's action (insert the target bookmark via
     the same `_safari_insert_bookmark` SafariHandler.apply uses).
  5. Run the verifier AFTER → must PASS blocking.

The intent is to catch generator/verifier drift early: a typo in
the selector or a forgotten `url_canonicalize=True` would fire here.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from pathlib import Path

import pytest

from fakes.fake_reader import FakeXCUITestReader
import sibb_state
from sibb_state import apply_initial_state
from sibb_spec import validate_spec
from sibb_task_generator_v3 import gen_safari_bookmark_specific_url
from sibb_verify import BaselineSnapshot, blocking_pass, run_checks

pytestmark = pytest.mark.fast


def _create_test_bookmarks_db(path: Path) -> None:
    """Same minimal schema as test_safari_handler. Repeated locally to
    avoid cross-test fixture coupling."""
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


@pytest.fixture
def patched_safari(tmp_path, monkeypatch):
    db = tmp_path / "Bookmarks.db"
    _create_test_bookmarks_db(db)
    monkeypatch.setattr(sibb_state, "_safari_bookmarks_db_path",
                         lambda udid: str(db))

    async def _noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", _noop_terminate)
    return db


# ─────────────────────────── round-trip ───────────────────────────────


def _verify(reader, task, baseline=None):
    results = asyncio.run(
        run_checks(reader, task.verify_checks, baseline=baseline))
    return blocking_pass(results), results


def _capture(reader):
    """Capture a baseline of `safari.bookmarks` right after the spec
    is applied — required by the `identity` check."""
    return asyncio.run(BaselineSnapshot.capture(
        reader, ["safari.bookmarks"]))


def _apply(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


def test_bookmark_specific_url_spec_validates():
    random.seed(1)
    t = gen_safari_bookmark_specific_url()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Safari"]
    # Bookmark spec entries shape: app/type/title/url/(folder?).
    bm_entries = [e for e in t.initial_state.spec
                  if e.get("app") == "Safari"]
    assert 5 <= len(bm_entries) <= 7
    assert all(e["type"] == "bookmark" for e in bm_entries)
    for e in bm_entries:
        assert isinstance(e.get("title"), str) and e["title"]
        assert isinstance(e.get("url"), str) and e["url"]


def test_bookmark_specific_url_verifier_fails_before_action(
        patched_safari):
    random.seed(2)
    t = gen_safari_bookmark_specific_url()
    reader = FakeXCUITestReader()
    _apply(reader, t)

    passed, results = _verify(reader, t)
    assert passed is False, (
        "verifier should FAIL before the agent has bookmarked the "
        "target URL — the target isn't there yet AND count is one "
        "short. If this passes the verifier is broken (false-positive "
        "during state setup).")

    # Inspect why it failed — at LEAST one of (exists target, count)
    # must have failed. Identity should still pass (no distractor has
    # been touched).
    fail_kinds = [r.kind for r in results if r.status != "pass"]
    assert "exists" in fail_kinds or "count" in fail_kinds


def test_bookmark_specific_url_verifier_passes_after_action(
        patched_safari):
    """End-to-end happy path: distractor seed → agent saves target
    bookmark → all 3 verifier checks pass."""
    random.seed(3)
    t = gen_safari_bookmark_specific_url()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    baseline = _capture(reader)

    # Agent's "action": insert the target bookmark via the same
    # SQLite path SafariHandler.apply would use (mimicking iOS
    # Safari's Add Bookmark → BookmarksBar root).
    target_url = t.params["target_url"]
    target_title = t.params.get("target_title_hint") or "Bookmark"
    asyncio.run(sibb_state._safari_insert_bookmark(
        reader.udid, target_title, target_url))

    passed, results = _verify(reader, t, baseline=baseline)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"verifier should PASS after target is bookmarked, but "
        f"these checks failed: "
        f"{[(r.kind, r.evidence) for r in failed]}")


def test_bookmark_specific_url_verifier_pass_with_url_in_subfolder(
        patched_safari):
    """Robustness: the agent files the target into a SUBFOLDER
    (like 'Reference' or 'Tech') rather than the BookmarksBar root.
    The folder-aware fetcher must still find it; verifier must pass."""
    random.seed(4)
    t = gen_safari_bookmark_specific_url()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    baseline = _capture(reader)
    target_url = t.params["target_url"]
    target_title = t.params.get("target_title_hint") or "Bookmark"
    asyncio.run(sibb_state._safari_insert_bookmark(
        reader.udid, target_title, target_url, folder="Inbox"))

    passed, results = _verify(reader, t, baseline=baseline)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"target bookmarked inside a NEW 'Inbox' subfolder — the "
        f"folder-aware fetcher should still find it. Failed checks: "
        f"{[(r.kind, r.evidence) for r in failed]}")


def test_bookmark_specific_url_canonicalization_absorbs_trailing_slash(
        patched_safari):
    """The selector uses `url_canonicalize=True`. If Safari (or the
    agent) saves `https://example.com/` instead of `https://example.com`,
    or `HTTPS://Example.com`, the verifier should still match."""
    random.seed(5)
    t = gen_safari_bookmark_specific_url()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    baseline = _capture(reader)
    target_url = t.params["target_url"]
    # Mutate the URL the way Safari might (lowercase host, add `/`).
    saved = target_url
    if "://" in saved and saved.count("/") == 2 and not saved.endswith("/"):
        saved = saved + "/"
    asyncio.run(sibb_state._safari_insert_bookmark(
        reader.udid, "Bookmark", saved))
    passed, results = _verify(reader, t, baseline=baseline)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"canonicalization should absorb trailing-slash differences "
        f"between selector URL and saved URL; failed: "
        f"{[(r.kind, r.evidence) for r in failed]}")


def test_bookmark_specific_url_distractor_identity_catches_relabel(
        patched_safari):
    """Cheat-resistance: agent renames a distractor bookmark to the
    target title instead of actually navigating + bookmarking the
    target URL. Identity check should catch the relabel."""
    random.seed(6)
    t = gen_safari_bookmark_specific_url()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    baseline = _capture(reader)
    target_url = t.params["target_url"]
    target_title = t.params.get("target_title_hint") or "Bookmark"

    # Agent insertion (legit).
    asyncio.run(sibb_state._safari_insert_bookmark(
        reader.udid, target_title, target_url))
    # Plus a "cheat" mutation — rename one distractor's title.
    import sqlite3 as _sql
    db_path = sibb_state._safari_bookmarks_db_path(reader.udid)
    with _sql.connect(db_path) as conn:
        # Pick the first distractor leaf (type=0, not the target).
        row = conn.execute(
            "SELECT id FROM bookmarks WHERE type=0 AND deleted=0 "
            "AND url != ? LIMIT 1;", (target_url,)).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE bookmarks SET title=? WHERE id=?;",
            ("MOVED-OR-RELABELED", row[0]))
    passed, results = _verify(reader, t, baseline=baseline)
    # Identity must fail; the rename cheats the title preservation.
    identity_fail = any(
        r.kind == "identity" and r.status != "pass"
        for r in results
    )
    assert identity_fail, (
        "renaming a distractor must trip the identity check — that's "
        "what makes the generator cheat-resistant")
    assert passed is False
