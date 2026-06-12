"""L1 tests for `sibb_mock_site_catalog` — the WebMall-backed catalog
underlying shop-task generators.

The bundled CSV at `sibb/benchmark/data/webmall_1.csv` is a fixed
snapshot (research-permissive, see NOTICE.txt). These tests pin both
the loader's behavior on that data AND the smaller properties
generators rely on (deterministic sampling, no winner-in-distractors,
hard-failure on impossible constraints).
"""
from __future__ import annotations
import os
import random
import sys

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_mock_site_catalog import (  # noqa: E402
    Catalog, SKU, _extract_brand, _parse_price_cents, _strip_html,
    load_catalog,
)

pytestmark = pytest.mark.fast


# ─── price parser ─────────────────────────────────────────────────────


def test_parse_price_european_comma_decimal():
    assert _parse_price_cents("139,99") == 13999


def test_parse_price_one_digit_fraction_pads_to_two():
    """`"5,5"` is 5 euros 50 cents, not 5 euros 5 cents."""
    assert _parse_price_cents("5,5") == 550


def test_parse_price_whole_only():
    assert _parse_price_cents("42") == 4200


def test_parse_price_dot_decimal_also_works():
    """Some rare rows may use dot instead of comma."""
    assert _parse_price_cents("9.99") == 999


def test_parse_price_empty_or_none_returns_none():
    assert _parse_price_cents("") is None
    assert _parse_price_cents("   ") is None
    assert _parse_price_cents("abc") is None


def test_parse_price_with_surrounding_whitespace():
    assert _parse_price_cents("  100,00  ") == 10000


# ─── HTML strip ───────────────────────────────────────────────────────


def test_strip_html_removes_tags_and_collapses_whitespace():
    raw = "<p>Hello   <strong>world</strong>!</p>"
    assert _strip_html(raw) == "Hello world !"


def test_strip_html_truncates_to_max_chars():
    raw = "<p>" + ("x" * 500) + "</p>"
    out = _strip_html(raw, max_chars=100)
    assert len(out) == 100


def test_strip_html_empty_input():
    assert _strip_html("") == ""
    assert _strip_html(None) == ""


# ─── brand extraction ────────────────────────────────────────────────


def test_extract_brand_one_deep_path_is_the_brand():
    assert _extract_brand("Kingston", "Kingston SSD") == "Kingston"


def test_extract_brand_two_deep_path_last_segment():
    assert _extract_brand("Motherboard > Asus", "Asus PRIME ...") == "Asus"


def test_extract_brand_three_deep_path_last_segment():
    assert (_extract_brand("Peripherals > Keyboard > Trust", "Trust TK ...")
            == "Trust")


def test_extract_brand_empty_path_falls_back_to_first_token():
    assert _extract_brand("", "Acme Widget 9000") == "Acme"


def test_extract_brand_strips_trailing_comma():
    assert _extract_brand("", "Asus, PRIME ...") == "Asus"


def test_extract_brand_completely_empty_yields_unknown():
    assert _extract_brand("", "") == "Unknown"


# ─── catalog loader ──────────────────────────────────────────────────


def test_catalog_loads_with_reasonable_size():
    """1152 raw rows; loader drops a handful of malformed-price / empty-name
    rows. Pin the post-load count to a SMALL range so a future
    regression-of-omission (loader silently drops half the corpus) fails."""
    cat = load_catalog()
    assert 1100 <= len(cat) <= 1152, (
        f"catalog size {len(cat)} out of expected 1100-1152 range; "
        f"loader may have dropped too many rows or stopped reading")


def test_catalog_categories_exactly_three_known():
    cat = load_catalog()
    assert set(cat.categories()) == {
        "PC Components", "PC Peripherals", "Other Electronics"}


def test_catalog_each_category_has_substantial_skus():
    cat = load_catalog()
    for c in cat.categories():
        assert len(cat.by_global_category[c]) >= 100, (
            f"category {c!r} has only {len(cat.by_global_category[c])} "
            f"SKUs; too small to build distractor sets from")


def test_catalog_singleton_returns_same_instance():
    a = load_catalog()
    b = load_catalog()
    assert a is b


def test_catalog_force_reload_returns_fresh_instance():
    a = load_catalog()
    b = load_catalog(force_reload=True)
    # Same content but different object identity.
    assert a is not b
    assert len(a) == len(b)


def test_every_sku_has_required_fields():
    """No empty names, valid positive prices, recognized category."""
    cat = load_catalog()
    for s in cat.skus:
        assert s.sku_id.startswith("wm-")
        assert s.name.strip()
        assert s.price_cents > 0
        assert s.global_category in {
            "PC Components", "PC Peripherals", "Other Electronics"}
        assert s.brand
        assert s.in_stock is True  # the CSV happens to be all-in-stock


def test_sku_ids_are_unique():
    cat = load_catalog()
    ids = [s.sku_id for s in cat.skus]
    assert len(ids) == len(set(ids))


# ─── filter / sample APIs ─────────────────────────────────────────────


def test_filter_by_category_returns_only_matching():
    cat = load_catalog()
    out = cat.filter(category="PC Peripherals")
    assert out  # non-empty
    assert all(s.global_category == "PC Peripherals" for s in out)


def test_filter_max_price_excludes_overpriced():
    cat = load_catalog()
    out = cat.filter(max_price_cents=5000)
    assert all(s.price_cents <= 5000 for s in out)


def test_filter_combined_constraints_intersect():
    cat = load_catalog()
    out = cat.filter(category="PC Components",
                      max_price_cents=10000)
    assert all(s.global_category == "PC Components"
                and s.price_cents <= 10000 for s in out)


def test_find_one_deterministic_per_seed():
    cat = load_catalog()
    rng_a = random.Random(42)
    rng_b = random.Random(42)
    a = cat.find_one(rng_a, category="PC Peripherals",
                      max_price_cents=5000)
    b = cat.find_one(rng_b, category="PC Peripherals",
                      max_price_cents=5000)
    assert a.sku_id == b.sku_id


def test_find_one_different_seed_different_sku_likely():
    """Across 20 seeds we should see at least 5 distinct winners — pinning
    that determinism doesn't accidentally collapse the sample space."""
    cat = load_catalog()
    winners = set()
    for seed in range(20):
        w = cat.find_one(random.Random(seed),
                          category="PC Components",
                          max_price_cents=20000)
        winners.add(w.sku_id)
    assert len(winners) >= 5, (
        f"only {len(winners)} distinct winners in 20 seeds; sampling "
        f"may have collapsed")


def test_find_one_raises_on_impossible_constraint():
    cat = load_catalog()
    with pytest.raises(ValueError):
        # Negative cap can't match anything.
        cat.find_one(random.Random(0), max_price_cents=-1)


def test_sample_distractors_excludes_winner():
    cat = load_catalog()
    rng = random.Random(7)
    w = cat.find_one(rng, category="PC Peripherals",
                      max_price_cents=10000)
    ds = cat.sample_distractors(rng, w, n=8,
                                  category="PC Peripherals",
                                  max_price_cents=10000)
    assert len(ds) == 8
    assert all(d.sku_id != w.sku_id for d in ds)


def test_sample_distractors_are_distinct():
    cat = load_catalog()
    rng = random.Random(11)
    w = cat.find_one(rng, category="PC Peripherals",
                      max_price_cents=10000)
    ds = cat.sample_distractors(rng, w, n=10,
                                  category="PC Peripherals",
                                  max_price_cents=10000)
    assert len({d.sku_id for d in ds}) == 10


def test_sample_distractors_raises_if_pool_too_small():
    cat = load_catalog()
    rng = random.Random(0)
    # Pick a constraint so tight only ~1-2 SKUs match.
    pool = cat.filter(max_price_cents=200)
    assert len(pool) >= 2  # sanity for the test premise
    w = pool[0]
    # Ask for more distractors than exist in the pool.
    with pytest.raises(ValueError):
        cat.sample_distractors(rng, w, n=10_000, max_price_cents=200)


def test_sample_mixed_returns_requested_count():
    cat = load_catalog()
    rng = random.Random(3)
    out = cat.sample_mixed(rng, n=12)
    assert len(out) == 12
    # And distinct.
    assert len({s.sku_id for s in out}) == 12


def test_sample_mixed_in_category_only():
    cat = load_catalog()
    rng = random.Random(13)
    out = cat.sample_mixed(rng, n=8, category="Other Electronics")
    assert all(s.global_category == "Other Electronics" for s in out)


# ─── data quality sanity ─────────────────────────────────────────────


def test_catalog_has_diverse_price_range():
    """Realism: the catalog should span at least 1 to 5000 dollars so
    distractor-by-price seeds can find both cheap and expensive
    options."""
    cat = load_catalog()
    prices = [s.price_cents for s in cat.skus]
    assert min(prices) <= 5_00     # at least one item ≤ $5
    assert max(prices) >= 500_00   # at least one item ≥ $500


def test_catalog_has_at_least_30_distinct_brands():
    """Distractor-by-brand seeds need a brand pool wider than a handful."""
    cat = load_catalog()
    assert len(cat.brands()) >= 30


# ─── BM25 search (Step 5N) ────────────────────────────────────────────


def test_bm25_returns_results_for_brand_queries():
    """A bare brand name as a query should surface ≥1 item of that
    brand in the top results — pinning the BM25 corpus contains the
    brand token."""
    cat = load_catalog()
    for brand in ("Asus", "Kingston", "Hama"):
        results = cat.bm25_search(brand, n=8)
        assert results
        assert any(r.brand == brand for r in results), (
            f"BM25 search {brand!r} returned no {brand}-brand SKUs")


def test_bm25_empty_query_returns_empty():
    cat = load_catalog()
    assert cat.bm25_search("", n=8) == []
    assert cat.bm25_search("   ", n=8) == []


def test_bm25_gibberish_query_returns_empty():
    cat = load_catalog()
    assert cat.bm25_search("foobarbazquux", n=8) == []


def test_bm25_top_n_respected():
    cat = load_catalog()
    results = cat.bm25_search("cable", n=3)
    assert len(results) <= 3
    results = cat.bm25_search("cable", n=20)
    assert len(results) <= 20


def test_bm25_deterministic_across_calls():
    cat = load_catalog()
    a = cat.bm25_search("wireless headphones", n=5)
    b = cat.bm25_search("wireless headphones", n=5)
    assert [s.sku_id for s in a] == [s.sku_id for s in b]


# ─── eligible cheapest winners (Step 5N) ──────────────────────────────


def test_eligible_cheapest_winners_returns_unique_cheapest():
    """Every (sku, cap) in the eligibility list MUST satisfy: sku is
    the strict-unique cheapest in its (brand, global_category, cap)
    family — i.e. the invariant `gen_safari_shop_pick_by_attrs`
    relies on."""
    cat = load_catalog()
    caps = (2500, 5000, 7500, 10000, 15000)
    eligibles = cat.eligible_cheapest_winners(caps)
    assert eligibles  # non-empty
    for sku, cap in eligibles:
        siblings = cat.filter(
            brand=sku.brand,
            category=sku.global_category,
            max_price_cents=cap)
        assert len(siblings) >= 2
        prices = [s.price_cents for s in siblings]
        assert sku.price_cents == min(prices)
        assert prices.count(min(prices)) == 1


def test_eligible_cheapest_winners_cached():
    """Repeated calls with the same caps return the SAME list object
    (cache hit)."""
    cat = load_catalog()
    caps = (2500, 5000, 7500, 10000, 15000)
    a = cat.eligible_cheapest_winners(caps)
    b = cat.eligible_cheapest_winners(caps)
    assert a is b


def test_eligible_cheapest_winners_solvability_gate():
    """Every winner in the eligibility list shows up in the top 8
    BM25 results for its own brand — the Q1 archetype solvability
    contract."""
    cat = load_catalog()
    caps = (2500, 5000, 7500, 10000, 15000)
    for sku, cap in cat.eligible_cheapest_winners(caps):
        hits = cat.bm25_search(sku.brand, n=8)
        ids = {h.sku_id for h in hits}
        assert sku.sku_id in ids, (
            f"winner {sku.sku_id} brand={sku.brand!r} cap=${cap/100} "
            f"not in BM25 top 8 for brand search")


def test_short_description_is_non_html():
    """Descriptions get HTML-stripped at load time — no `<` or `>` in
    the parsed field."""
    cat = load_catalog()
    for s in cat.skus[:100]:  # spot-check is fine
        assert "<" not in s.short_description
        assert ">" not in s.short_description
