"""NewsHandler — L1 tests.

News is a content app with capability investigation finalized
2026-05-16. v1 handler is minimal: registers the app, declares
no TCC needs, stubs apply, provides a news.headlines fetcher
that scrapes the Today-feed AX tree.

These tests pin the v1 contract. When v2 figures out how to
write to `reading-list` (the 6-byte header wrapper), the apply
test will need to be updated.
"""

from __future__ import annotations

import pytest

from sibb_state import (
    HANDLERS,
    NewsHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_news_handler_registered_by_bundle_id():
    assert NewsHandler.bundle_id == "com.apple.news"
    assert HANDLERS[NewsHandler.bundle_id] is NewsHandler


def test_news_handler_no_tcc_services():
    """News doesn't need any TCC grant for our v1 surface (feed
    reads via AX, no contacts/photos/etc. integration). If v2 adds
    a Following-channels-write that uses Contacts integration,
    this test would need updating."""
    assert NewsHandler.tcc_services == []


def test_news_handler_is_not_a_pre_runner():
    assert NewsHandler.pre_runner is False
    assert NewsHandler.pre_runner_kinds == []


def test_canonicalize_news_friendly_name():
    assert canonicalize_app("News") == "com.apple.news"
    assert canonicalize_app("news") == "com.apple.news"


def test_news_does_not_contribute_to_collect_tcc_services():
    services = collect_tcc_services()
    for s in services:
        assert "news" not in s.lower()


# ─────────────────────────── apply/reset stubs ───────────────────────

async def test_reset_is_noop():
    h = NewsHandler(reader=None)
    await h.reset()


async def test_apply_raises_clear_error_in_v1():
    """v1 has no apply primitive. The error message should point
    at WHY (server-curated content + unwrapped plist format) so
    future engineers understand the gap rather than think it's
    incomplete."""
    h = NewsHandler(reader=None)
    with pytest.raises(ValueError, match="no apply primitive"):
        await h.apply({"type": "save", "article_id": "rl-12345"})


# ─────────────────────────── news.headlines fetcher ──────────────────

def test_news_headlines_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "news.headlines" in RESOURCE_FETCHERS


class _RecordingReader:
    """Stand-in for XCUITestReader that returns a canned observe
    response. Lets us test the parsing logic without a real sim."""
    def __init__(self, observe_response):
        self._response = observe_response

    async def _send(self, cmd):
        if cmd.get("type") == "observe":
            return self._response
        return {"ok": False, "error": "unsupported in fake"}


async def test_fetcher_parses_today_feed_headlines():
    """Real iOS News.app encodes article cells as `Other` elements
    with label `"<source>, <title>"`. Fetcher must extract source
    and title from each."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "ScrollArea", "label": "Today Feed"},
            {"role": "Other",
             "label": "The Wall Street Journal, Trump nemesis "
                      "Sen. Bill Cassidy defeated in GOP primary"},
            {"role": "Other",
             "label": "The Associated Press, WHO declares global "
                      "health emergency over Ebola outbreak"},
            {"role": "Other", "label": "Vertical scroll bar, 4 pages"},
            {"role": "Button", "label": "Today"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.headlines"](reader, {})
    assert len(rows) == 2
    assert rows[0] == {
        "source": "The Wall Street Journal",
        "title":  "Trump nemesis Sen. Bill Cassidy defeated in "
                  "GOP primary",
    }
    assert rows[1] == {
        "source": "The Associated Press",
        "title":  "WHO declares global health emergency over "
                  "Ebola outbreak",
    }


async def test_fetcher_filters_out_scroll_bars():
    """Today feed has multiple `Other` elements that aren't articles
    (scroll bars, layout helpers). Fetcher must filter them out by
    checking for "scroll bar" in the title segment."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "Other", "label": "Vertical scroll bar, 4 pages"},
            {"role": "Other", "label": "Horizontal scroll bar, 1 page"},
            {"role": "Other",
             "label": "Bloomberg, Markets close on volatile Friday"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.headlines"](reader, {})
    assert len(rows) == 1
    assert rows[0]["source"] == "Bloomberg"


async def test_fetcher_propagates_observe_failure():
    """When the observe call to the runner fails, the fetcher must
    raise ResourceFetchError rather than silently returning an
    empty list (which would mask broken plumbing as "no headlines")."""
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    reader = _RecordingReader({
        "ok": False, "error": "no_app"})
    with pytest.raises(ResourceFetchError, match="no_app"):
        await RESOURCE_FETCHERS["news.headlines"](reader, {})


async def test_fetcher_filters_by_source():
    """Selector pushdown: filter by source name. The Wall Street
    Journal vs Associated Press are the two we'd most often
    differentiate in tests."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "Other",
             "label": "The Wall Street Journal, Article A"},
            {"role": "Other",
             "label": "The Associated Press, Article B"},
            {"role": "Other",
             "label": "Bloomberg, Article C"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.headlines"](
        reader, {"source": "Bloomberg"})
    assert len(rows) == 1
    assert rows[0]["title"] == "Article C"


# ─────────────────────────── news.recipes fetcher ────────────────────

def test_news_recipes_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "news.recipes" in RESOURCE_FETCHERS


async def test_recipes_fetcher_parses_with_duration():
    """News' Recipe Catalog labels recipes as
    `"<source>, RECIPE, <duration>, <title>"`. Fetcher extracts
    all three fields. Verified labels from live capture
    2026-05-16."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "Other",
             "label": "Real Simple, RECIPE, 35m, "
                      "Springy Orzo Salad With Lemon Vinaigrette"},
            {"role": "Other",
             "label": "Simply Recipes, RECIPE, 3h 5m, "
                      "Easy Make-Ahead French Breakfast Casserole"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.recipes"](reader, {})
    assert len(rows) == 2
    assert rows[0] == {
        "source": "Real Simple",
        "duration": "35m",
        "title": "Springy Orzo Salad With Lemon Vinaigrette",
    }
    assert rows[1] == {
        "source": "Simply Recipes",
        "duration": "3h 5m",
        "title": "Easy Make-Ahead French Breakfast Casserole",
    }


async def test_recipes_fetcher_parses_without_duration():
    """Some recipes have no duration field —
    `"<source>, RECIPE, <title>"`. Fetcher must handle both shapes
    so a missing-duration label doesn't drop the row."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "Other",
             "label": "Epicurious, RECIPE, Blistered-Asparagus Frittata"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.recipes"](reader, {})
    assert len(rows) == 1
    assert rows[0]["source"] == "Epicurious"
    assert rows[0]["duration"] == ""
    assert rows[0]["title"] == "Blistered-Asparagus Frittata"


async def test_recipes_fetcher_ignores_non_recipe_others():
    """Non-recipe Other elements (regular articles, scroll bars,
    section headers) must not appear in recipe rows. Distinguishing
    marker is `, RECIPE, ` in the label."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "Other",
             "label": "The Washington Post, "
                      "Is Red Lobster's Endless Shrimp deal worth it?"},
            {"role": "Other",
             "label": "Vertical scroll bar, 4 pages"},
            {"role": "Other",
             "label": "Real Simple, RECIPE, 10m, Test Recipe"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.recipes"](reader, {})
    assert len(rows) == 1
    assert rows[0]["title"] == "Test Recipe"


async def test_recipes_fetcher_filters_by_source():
    """Selector pushdown: narrow to one publisher."""
    from sibb_verify import RESOURCE_FETCHERS
    reader = _RecordingReader({
        "ok": True,
        "elements": [
            {"role": "Other", "label": "Real Simple, RECIPE, 10m, A"},
            {"role": "Other",
             "label": "Epicurious, RECIPE, B"},
            {"role": "Other",
             "label": "Simply Recipes, RECIPE, 30m, C"},
        ],
    })
    rows = await RESOURCE_FETCHERS["news.recipes"](
        reader, {"source": "Epicurious"})
    assert len(rows) == 1
    assert rows[0]["title"] == "B"
