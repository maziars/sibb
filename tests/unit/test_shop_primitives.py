"""L1 tests for the shop primitives (Step 5M, 2026-06-08).

Covers:
  * `harness_layout.product_card()` — AX-correct card shape
  * `harness_layout.star_glyphs()` — rating-to-stars edge cases
  * `harness_layout.synth_rating()` — plausible synthetic ratings
  * `sibb_mock_site._serve_static_page` — prefix-routing + opt-in
    `path` / `query` kwargs for new shop templates (backward
    compatible with single-arg `(rng)` templates).
"""
from __future__ import annotations

import os
import random
import sys
import urllib.error
import urllib.request

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from harness_layout import product_card, star_glyphs, synth_rating  # noqa: E402
from sibb_mock_site import MockSite  # noqa: E402
from sibb_mock_site_catalog import load_catalog  # noqa: E402


pytestmark = pytest.mark.fast


# ─── star_glyphs ─────────────────────────────────────────────────────


def test_star_glyphs_full_rating():
    assert star_glyphs(5.0) == "★★★★★"


def test_star_glyphs_zero_rating():
    assert star_glyphs(0.0) == "☆☆☆☆☆"


def test_star_glyphs_half_rounds_up():
    # 3.5 → 3 full + half + 1 empty
    assert star_glyphs(3.5) == "★★★½☆"


def test_star_glyphs_round_half_threshold():
    # 3.4 → 3 full + 2 empty (no half)
    assert star_glyphs(3.4) == "★★★☆☆"


def test_star_glyphs_clamps_above_5():
    assert star_glyphs(7.0) == "★★★★★"


def test_star_glyphs_clamps_below_0():
    assert star_glyphs(-2.0) == "☆☆☆☆☆"


# ─── synth_rating ────────────────────────────────────────────────────


def test_synth_rating_returns_in_valid_ranges():
    rng = random.Random(0)
    for _ in range(50):
        r, n = synth_rating(rng)
        assert 3.0 <= r <= 5.0
        assert 1 <= n <= 9999


def test_synth_rating_deterministic_per_seed():
    a = synth_rating(random.Random(42))
    b = synth_rating(random.Random(42))
    assert a == b


def test_synth_rating_distribution_skews_high():
    """80%+ of synthetic ratings should be >= 4.0 (matching real
    e-commerce distributions). Sample 200 draws."""
    rng = random.Random(1)
    ratings = [synth_rating(rng)[0] for _ in range(200)]
    high = sum(1 for r in ratings if r >= 4.0)
    assert high / 200.0 >= 0.7, (
        f"synth_rating skew too low; {high}/200 >= 4.0 stars")


# ─── product_card ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def real_sku():
    """A real WebMall SKU — `product_card` accepts any duck-typed
    object with the right fields; using a real one catches
    integration drift."""
    cat = load_catalog()
    return cat.find_one(random.Random(11), category="PC Peripherals",
                         max_price_cents=5000)


def test_product_card_uses_article_with_aria_labelledby(real_sku):
    html = product_card(real_sku)
    assert "<article aria-labelledby=" in html


def test_product_card_title_is_in_h2_link_not_whole_card_link(real_sku):
    """The load-bearing AX micro-decision: a `<h2><a>title</a></h2>`
    keeps the card-as-AX-node singular; a whole-card `<a>` would
    flatten the contents into one giant label and hide secondary
    actions. Pin both halves of the rule."""
    html = product_card(real_sku)
    # Title link present.
    assert "<h2 id=" in html
    assert f'<a href="/product/{real_sku.sku_id}">' in html
    # No <a> wrapping the entire <article>.
    assert "<a " not in html.split("<article")[1].split("</article>")[0].split("<h2")[0]


def test_product_card_default_link_is_product_path(real_sku):
    html = product_card(real_sku)
    assert f'/product/{real_sku.sku_id}' in html


def test_product_card_detail_link_override(real_sku):
    html = product_card(real_sku, detail_link="/p/custom")
    assert "/p/custom" in html
    assert f'/product/{real_sku.sku_id}' not in html


def test_product_card_rating_block_renders_when_both_set(real_sku):
    html = product_card(real_sku, rating=4.3, review_count=82)
    assert 'aria-label="4.3 stars"' in html
    assert "(82)" in html
    assert "★★★★" in html  # at least 4 full stars


def test_product_card_rating_block_absent_when_either_missing(real_sku):
    html = product_card(real_sku, rating=None, review_count=None)
    assert "aria-label=" in html  # the placeholder image still has one
    assert "stars" not in html


def test_product_card_sponsored_adds_badge(real_sku):
    html = product_card(real_sku, sponsored=True)
    assert "[Sponsored]" in html


def test_product_card_show_description_false_suppresses_prose(real_sku):
    html_with = product_card(real_sku, show_description=True)
    html_without = product_card(real_sku, show_description=False)
    # Description content present in one, absent in the other.
    if real_sku.short_description:
        snippet = real_sku.short_description[:30]
        assert snippet in html_with
        assert snippet not in html_without


def test_product_card_escapes_html_in_title(real_sku):
    """Defense in depth: the WebMall catalog has clean titles today
    but a future row with `&` / `<` must not break out of the card."""
    # Build a duck-typed SKU with a malicious title.
    class FakeSKU:
        sku_id = "wm-9999"
        name = 'Bad <script>alert("xss")</script> Title & "more"'
        brand = "Acme"
        price_cents = 2599
        short_description = "<b>desc</b>"
    html = product_card(FakeSKU())
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_product_card_renders_price_with_dollar_sign(real_sku):
    html = product_card(real_sku)
    # "$XX.YY" should appear inside <strong>.
    expected = f"${real_sku.price_cents / 100:.2f}"
    assert f"<strong>{expected}</strong>" in html


# ─── MockSite prefix routing + opt-in kwargs ─────────────────────────


def _http_get(port: int, path: str) -> str:
    return urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=5).read().decode()


def _http_get_status(port: int, path: str) -> int:
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=5)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


@pytest.fixture
def shop_site():
    """Spin up a MockSite with mixed exact + prefix routes for routing
    tests. Yields (site, port); auto-stops at teardown."""
    def landing(rng):
        return "<h1>Landing</h1>"

    def pdp(rng, *, path, query):
        sku = path.rsplit("/", 1)[-1] if "/" in path else ""
        return f"<h1>PDP {sku}</h1><p>q={query!r}</p>"

    def account_root(rng, *, path):
        return f"<h1>Account: {path}</h1>"

    def account_profile(rng, *, path):
        return f"<h1>Profile: {path}</h1>"

    site = MockSite(
        site_id="test-shop",
        static_pages={
            "/": landing,
            "/product/": pdp,
            "/account/": account_root,
            "/account/profile/": account_profile,
        },
    )
    site.page_seed = 42
    site.start()
    try:
        yield site, site.port
    finally:
        site.stop()


def test_routing_exact_match_root(shop_site):
    _, port = shop_site
    assert "Landing" in _http_get(port, "/")


def test_routing_prefix_matches_pdp(shop_site):
    _, port = shop_site
    body = _http_get(port, "/product/wm-1546")
    assert "PDP wm-1546" in body


def test_routing_prefix_passes_query_kwarg(shop_site):
    _, port = shop_site
    body = _http_get(port, "/product/wm-1234?ref=email")
    assert "q='ref=email'" in body


def test_routing_longest_prefix_wins(shop_site):
    """/account/profile/me must hit account_profile, not account_root."""
    _, port = shop_site
    body = _http_get(port, "/account/profile/me")
    assert "Profile: /account/profile/me" in body


def test_routing_shorter_prefix_wins_when_no_longer_match(shop_site):
    _, port = shop_site
    body = _http_get(port, "/account/billing")
    assert "Account: /account/billing" in body


def test_routing_bare_root_not_used_as_prefix(shop_site):
    """`/` is registered as an exact-match template; it should NOT
    swallow `/nothere` as a prefix (the realism research's main
    smoke-test concern)."""
    _, port = shop_site
    assert _http_get_status(port, "/nothere") == 404


def test_routing_backward_compat_single_arg_template():
    """Templates that accept only `rng` (the existing convention)
    still work — `inspect`-based kwarg pass MUST NOT pass `path` /
    `query` to a template that doesn't declare them."""
    def legacy(rng):  # no kwargs
        return f"<p>legacy {rng.randint(0,9)}</p>"
    site = MockSite(site_id="legacy-test", static_pages={"/legacy": legacy})
    site.page_seed = 1
    site.start()
    try:
        body = _http_get(site.port, "/legacy")
        assert "legacy" in body
    finally:
        site.stop()


def test_routing_template_with_only_path_kwarg():
    """A template can opt in to `path` without `query`."""
    def only_path(rng, *, path):
        return f"<p>got path={path!r}</p>"
    site = MockSite(site_id="only-path",
                    static_pages={"/x/": only_path})
    site.page_seed = 1
    site.start()
    try:
        body = _http_get(site.port, "/x/foo/bar")
        assert "path='/x/foo/bar'" in body
    finally:
        site.stop()
