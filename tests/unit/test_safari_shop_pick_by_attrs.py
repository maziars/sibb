"""L1 round-trip tests for `gen_safari_shop_pick_by_attrs` (Step 5M).

Mirrors `test_safari_rsvp_form_generator.py`: spec validates, MockSite
applies, verifier fails before any action, passes after a synthetic
POST with the right fields, fails on wrong sku / persona / count.
"""
from __future__ import annotations

import asyncio
import random
import urllib.error
import urllib.parse
import urllib.request

import pytest

from fakes.fake_reader import FakeXCUITestReader
import sibb_state
from sibb_state import apply_initial_state
from sibb_spec import validate_spec
from sibb_task_generator_v3 import gen_safari_shop_pick_by_attrs
from sibb_verify import blocking_pass, run_checks


pytestmark = pytest.mark.fast


# ─── helpers (copied from rsvp test shape) ────────────────────────────


def _post(url: str, data: dict):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def _verify(reader, task):
    results = asyncio.run(run_checks(reader, task.verify_checks))
    return blocking_pass(results), results


def _apply(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), (
        f"state setup failed: {report['errors']}")
    return report


@pytest.fixture
def patched_no_safari_open(monkeypatch):
    """Skip the simctl-needing helpers; the MockSite HTTP server still
    spawns on 127.0.0.1 and the test directly POSTs to it."""
    import sibb_mock_site
    monkeypatch.setattr(sibb_mock_site, "open_in_safari",
                        lambda udid, url, **kw: None)
    async def _noop_terminate(udid): pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", _noop_terminate)
    yield


def _full_post_payload(task):
    p = task.params
    payload = {
        "sku":         p["target_sku"],
        "ship_name":   p["persona_name"],
        "ship_street": p["persona_street"],
        "ship_city":   p["persona_city"],
        "ship_state":  p["persona_state"],
        "ship_zip":    p["persona_zip"],
    }
    # V4: append payment fields from the PERSONAL card so
    # round-trip POSTs satisfy the V4 verifier blocks.
    if p.get("use_saved_cards"):
        personal = p["personal_card"]
        payload["pay_card_last4"] = personal["last4"]
        payload["pay_exp_mm"]     = personal["exp_mm"]
        payload["pay_exp_yy"]     = personal["exp_yy"]
    return payload


# ─── spec + params ────────────────────────────────────────────────────


def test_shop_spec_validates():
    random.seed(1)
    t = gen_safari_shop_pick_by_attrs()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Safari"]
    sites = [e for e in t.initial_state.spec
             if e.get("type") == "mock_site"]
    assert len(sites) == 1
    site = sites[0]
    # Step 5O (2026-06-09): V0.5 + V4 — 5 templates, /account/cards
    # added unconditionally so non-V4 episodes also serve a friendly
    # empty-state on that path (doesn't leak which seeds have V4 on).
    assert site["static_pages"] == {
        "/":              "shop_landing",
        "/search":        "shop_search_results",
        "/product/":      "shop_pdp",
        "/checkout":      "shop_checkout",
        "/account/cards": "shop_account_cards",
    }
    # Agent now lands on `/` and types into the search bar, not the
    # raw `/search` URL.
    assert site["start_path"] == "/"
    assert site["open_at_start"] is False
    assert isinstance(site["page_seed"], int) and site["page_seed"] > 0


def test_shop_params_are_consistent():
    """Per-seed generator output must internally agree — the winner
    SKU is unique cheapest under its constraints, the persona fields
    are all populated, and the landing_url carries the port
    placeholder."""
    random.seed(2)
    t = gen_safari_shop_pick_by_attrs()
    p = t.params
    assert p["target_sku"].startswith("wm-")
    assert p["target_price_cents"] <= p["target_max_price_cents"]
    for k in ("persona_name", "persona_street", "persona_city",
               "persona_state", "persona_zip"):
        assert p[k], f"persona field {k} empty"
    # Landing URL carries the port placeholder.
    assert "{port:" in p["landing_url"]
    # Archetype + search hint always populated.
    assert p["archetype"] in ("Q1", "Q2")
    assert p["search_hint"]


def test_shop_instruction_contains_targeting_and_persona():
    """The instruction must explicitly state the targeting constraints
    (brand or search hint, category, max price) and the persona fields.
    Must include the 'CHEAPEST' superlative — that's what makes the
    canonical answer unique."""
    random.seed(3)
    t = gen_safari_shop_pick_by_attrs()
    instr = t.instruction
    if t.params["archetype"] == "Q1":
        assert t.params["target_brand"] in instr
        assert t.params["target_category"] in instr
    else:
        # Q2 puts the search hint verbatim into the instruction.
        assert t.params["search_hint"] in instr
    assert f"${t.params['target_max_price_cents'] / 100:.2f}" in instr
    assert "CHEAPEST" in instr  # the superlative pin
    assert t.params["persona_name"] in instr
    assert t.params["persona_street"] in instr
    assert t.params["persona_zip"] in instr
    # V0.5 dropped the sponsored badge entirely.
    assert "Sponsored" not in instr


def test_shop_archetype_split_across_seeds():
    """Across 40 seeds we should see BOTH Q1 and Q2 with at least
    ~30% each (sampling noise on a 50/50 toss)."""
    counts = {"Q1": 0, "Q2": 0}
    for seed in range(40):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        counts[t.params["archetype"]] += 1
    assert counts["Q1"] >= 12 and counts["Q2"] >= 12, (
        f"archetype split too lopsided: {counts}")


def test_shop_seed_determinism():
    """Same seed → same generator output (modulo Python's randomness
    state on uuid). The targeting params must be stable per seed."""
    random.seed(42)
    a = gen_safari_shop_pick_by_attrs()
    random.seed(42)
    b = gen_safari_shop_pick_by_attrs()
    assert a.params["target_sku"]      == b.params["target_sku"]
    assert a.params["target_brand"]    == b.params["target_brand"]
    assert a.params["target_category"] == b.params["target_category"]
    assert a.params["persona_name"]    == b.params["persona_name"]
    assert a.params["archetype"]       == b.params["archetype"]
    assert a.params["search_hint"]     == b.params["search_hint"]


def test_shop_winner_is_unique_cheapest_in_its_family():
    """V0.5 canonical-answer invariant: the winner SKU is the UNIQUE
    cheapest in-stock item among its (brand, global_category) family
    under the chosen cap. Pin this across 25 seeds because it's the
    contract that makes `attribute_eq fields.sku == winner` correct."""
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    for seed in range(25):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        winner = next(s for s in cat.skus
                       if s.sku_id == t.params["target_sku"])
        siblings = cat.filter(
            brand=winner.brand,
            category=winner.global_category,
            max_price_cents=t.params["target_max_price_cents"])
        assert len(siblings) >= 2, (
            f"seed={seed}: family has only {len(siblings)} eligibles, "
            f"'cheapest' is not meaningful")
        min_price = min(s.price_cents for s in siblings)
        assert winner.price_cents == min_price, (
            f"seed={seed}: winner ${winner.price_cents/100:.2f} is NOT "
            f"the cheapest (min ${min_price/100:.2f}) in its family")
        cheap_count = sum(
            1 for s in siblings if s.price_cents == min_price)
        assert cheap_count == 1, (
            f"seed={seed}: {cheap_count} SKUs tied at the minimum "
            f"price — verifier would be ambiguous")


# ─── verifier round-trip ──────────────────────────────────────────────


def test_shop_verifier_fails_before_action(patched_no_safari_open):
    random.seed(4)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    passed, results = _verify(reader, t)
    assert passed is False, (
        "verifier should FAIL before any /checkout POST")
    failed_kinds = [r.kind for r in results if r.status != "pass"]
    assert "count" in failed_kinds or "attribute_eq" in failed_kinds


def test_shop_verifier_passes_after_correct_post(patched_no_safari_open):
    random.seed(5)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    assert site is not None
    _post(f"{site.base_url}/checkout", _full_post_payload(t))
    passed, results = _verify(reader, t)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"all blocking checks should PASS; failed: "
        f"{[(r.kind, r.label, r.evidence) for r in failed]}")


def test_shop_verifier_fails_on_wrong_sku(patched_no_safari_open):
    """Agent bought from the wrong PDP → fields.sku doesn't match
    target_sku → blocking attribute_eq fails."""
    random.seed(6)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    payload = _full_post_payload(t)
    payload["sku"] = "wm-9999"  # not the winner
    _post(f"{site.base_url}/checkout", payload)
    passed, results = _verify(reader, t)
    assert passed is False
    sku_fails = [r for r in results
                  if r.kind == "attribute_eq"
                  and "sku" in r.label
                  and r.status != "pass"]
    assert sku_fails


def test_shop_verifier_fails_on_wrong_persona_field(
        patched_no_safari_open):
    """Agent typed the wrong street → blocking ship_street check
    fails."""
    random.seed(7)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    payload = _full_post_payload(t)
    payload["ship_street"] = "999 Wrong Way"
    _post(f"{site.base_url}/checkout", payload)
    passed, results = _verify(reader, t)
    assert passed is False
    street_fails = [r for r in results
                     if r.kind == "attribute_eq"
                     and "ship_street" in r.label
                     and r.status != "pass"]
    assert street_fails


def test_shop_verifier_fails_on_duplicate_submission(
        patched_no_safari_open):
    """Agent placed the order twice → count check fails."""
    random.seed(8)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    payload = _full_post_payload(t)
    _post(f"{site.base_url}/checkout", payload)
    _post(f"{site.base_url}/checkout", payload)
    passed, results = _verify(reader, t)
    assert passed is False
    count_fail = next(
        r for r in results
        if r.kind == "count" and r.status != "pass")
    assert count_fail.evidence.get("actual") == 2


# ─── page templates render via MockSite ───────────────────────────────


def test_shop_landing_page_has_search_form(patched_no_safari_open):
    """The agent's entry point is `/` — must include a search input
    + button form pointing at /search."""
    random.seed(9)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/", timeout=5).read().decode()
    assert 'name="q"' in body
    assert 'action="/search"' in body
    assert '<button type="submit"' in body


def test_shop_search_page_returns_results_for_winner_brand(
        patched_no_safari_open):
    """For Q1-archetype seeds the agent searches by brand; we
    pre-filter eligibility to guarantee that brand → top 8 BM25
    contains the winner. For Q2-archetype seeds the agent uses the
    title hint, which (being title-derived) trivially surfaces the
    winner. Pin both halves of the property across 10 seeds."""
    for seed in range(10):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        reader = FakeXCUITestReader()
        _apply(reader, t)
        from sibb_mock_site import get_site
        site = get_site(t.params["site_id"])
        q = (t.params["target_brand"]
              if t.params["archetype"] == "Q1"
              else t.params["search_hint"])
        body = urllib.request.urlopen(
            f"{site.base_url}/search?q={urllib.parse.quote(q)}",
            timeout=5).read().decode()
        assert t.params["target_sku"] in body, (
            f"seed={seed} archetype={t.params['archetype']} "
            f"query={q!r}: winner {t.params['target_sku']} not in "
            f"top 8 BM25 results")


def test_shop_search_page_empty_query_renders_empty_state(
        patched_no_safari_open):
    """`/search` with no `?q=` must NOT 500 — it should render a
    friendly empty-state with the search bar repeated."""
    random.seed(91)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/search", timeout=5).read().decode()
    # No product cards (nothing to show).
    assert "<article aria-labelledby=" not in body
    # Search form repeated for the agent to retry.
    assert 'name="q"' in body


def test_shop_search_page_zero_results_renders_no_results_state(
        patched_no_safari_open):
    """`/search?q=<gibberish>` returns a "No results" page, not a
    500 or an empty cards block."""
    random.seed(92)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/search?q=qzqxqjqkqv",
        timeout=5).read().decode()
    assert "<article aria-labelledby=" not in body
    assert "No products matched" in body


def test_shop_pdp_page_serves_winner(patched_no_safari_open):
    """The PDP at /product/<winner_sku> renders the winner's title
    and includes the Buy Now link to /checkout?sku=<winner_sku>."""
    random.seed(10)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/product/{t.params['target_sku']}",
        timeout=5).read().decode()
    assert t.params["target_name"][:30] in body
    assert f"/checkout?sku={t.params['target_sku']}" in body
    assert "Buy Now" in body


def test_shop_checkout_page_shows_winner_summary_and_form_fields(
        patched_no_safari_open):
    """The checkout page reads ?sku= and renders an order summary
    block plus the 5 shipping form fields."""
    random.seed(11)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/checkout?sku={t.params['target_sku']}",
        timeout=5).read().decode()
    # Order summary with winner name + price.
    assert t.params["target_name"][:30] in body
    expected_price = f"${t.params['target_price_cents'] / 100:.2f}"
    assert expected_price in body
    # All 5 shipping fields present by name attribute.
    for field in ("ship_name", "ship_street", "ship_city",
                   "ship_state", "ship_zip"):
        assert f'name="{field}"' in body, f"missing field {field}"
    # Submit label.
    assert "Place order" in body
    # Hidden sku field carries the right value.
    assert f'name="sku" value="{t.params["target_sku"]}"' in body


def test_shop_pdp_unknown_sku_gets_not_found_page(patched_no_safari_open):
    """Navigating to /product/<nonexistent_sku> renders a friendly
    'not found' page rather than 500-ing."""
    random.seed(12)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/product/wm-9999999", timeout=5).read().decode()
    assert "not found" in body.lower()


def test_shop_checkout_without_sku_query_renders_empty_state(
        patched_no_safari_open):
    """Going straight to /checkout with no ?sku= must render an
    empty-cart message rather than blowing up."""
    random.seed(13)
    t = gen_safari_shop_pick_by_attrs()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/checkout", timeout=5).read().decode()
    assert "Checkout" in body
    # No form fields rendered (since there's no order to checkout for).
    assert 'name="ship_name"' not in body


# ─── V4 (Step 5O) — saved-cards axis ───────────────────────────────────


def _find_v4_seed(start: int = 0, limit: int = 200):
    """Helper — scan seeds for one with use_saved_cards=True. Returns
    (seed, task). V4 fires at ~40% so this finds one within a few
    tries; the wider window guards against unlucky streaks."""
    for seed in range(start, start + limit):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        if t.params["use_saved_cards"]:
            return seed, t
    raise AssertionError(
        f"no V4 seed found in [{start}, {start+limit}) — V4 axis "
        f"either disabled or improbably skewed")


def _find_non_v4_seed(start: int = 0, limit: int = 200):
    for seed in range(start, start + limit):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        if not t.params["use_saved_cards"]:
            return seed, t
    raise AssertionError("no non-V4 seed found")


def test_v4_axis_fires_for_some_seeds_and_not_others():
    """V4 is ~40% per the constant. Across 60 seeds we should see
    both V4 and non-V4 episodes, neither dominating."""
    on, off = 0, 0
    for seed in range(60):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        if t.params["use_saved_cards"]:
            on += 1
        else:
            off += 1
    assert on >= 10 and off >= 10, (
        f"V4 split too lopsided: on={on}, off={off} of 60")


def test_v4_is_orthogonal_to_archetype():
    """V4 must combine with BOTH Q1 and Q2 — the orthogonality is
    the whole point of the design ("V4 always paired with one of
    Q1/Q2")."""
    seen = {"Q1_v4": False, "Q2_v4": False,
            "Q1_novm": False, "Q2_novm": False}
    for seed in range(80):
        random.seed(seed)
        t = gen_safari_shop_pick_by_attrs()
        v4 = "v4" if t.params["use_saved_cards"] else "novm"
        seen[f"{t.params['archetype']}_{v4}"] = True
        if all(seen.values()):
            return
    missing = [k for k, v in seen.items() if not v]
    raise AssertionError(
        f"didn't observe all 4 (archetype × V4) combos: missing "
        f"{missing}")


def test_v4_personal_and_work_cards_are_distinct_brands():
    """Personal vs Work must use DIFFERENT brand strings — agent
    has to disambiguate by label, not coincidence."""
    seed, t = _find_v4_seed()
    p = t.params
    pc, wc = p["personal_card"], p["work_card"]
    assert pc["brand"] != wc["brand"], (
        f"seed={seed}: personal and work cards share brand "
        f"{pc['brand']!r}")
    # Card last4 must be 4 digits.
    assert pc["last4"].isdigit() and len(pc["last4"]) == 4
    assert wc["last4"].isdigit() and len(wc["last4"]) == 4
    # Expiry month 01-12, year 26/27/28.
    assert 1 <= int(pc["exp_mm"]) <= 12
    assert pc["exp_yy"] in ("26", "27", "28")


def test_v4_instruction_mentions_account_cards_and_personal(
        patched_no_safari_open):
    """V4 instruction must direct the agent to /account/cards AND
    explicitly say PERSONAL (not the Work card)."""
    seed, t = _find_v4_seed()
    instr = t.instruction
    assert "/account/cards" in instr
    assert "PERSONAL" in instr
    assert "Work" in instr  # paired contrast so agent knows there ARE two


def test_v4_verifier_includes_3_payment_blocks():
    """V4 verify_checks must have exactly 3 extra blocking checks for
    pay_card_last4, pay_exp_mm, pay_exp_yy on top of the base 8."""
    seed, t = _find_v4_seed()
    pay_labels = [c["label"] for c in t.verify_checks
                   if c.get("attr", "").startswith("fields.pay_")]
    assert len(pay_labels) == 3, (
        f"seed={seed}: expected 3 payment checks, got {pay_labels}")


def test_non_v4_verifier_has_no_payment_blocks():
    """Non-V4 episodes must NOT include payment-field checks (they
    wouldn't be satisfiable — checkout never renders pay_* inputs)."""
    seed, t = _find_non_v4_seed()
    pay_checks = [c for c in t.verify_checks
                   if c.get("attr", "").startswith("fields.pay_")]
    assert pay_checks == []


def test_v4_account_cards_page_renders_two_distinct_cards(
        patched_no_safari_open):
    """The /account/cards page must show BOTH cards with their
    label, brand, and last4 so the agent can pick PERSONAL."""
    seed, t = _find_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/account/cards", timeout=5).read().decode()
    p = t.params
    pc, wc = p["personal_card"], p["work_card"]
    assert "Personal card" in body
    assert "Work card" in body
    assert pc["brand"] in body and wc["brand"] in body
    assert pc["last4"] in body and wc["last4"] in body
    # Empty-state copy must NOT be present.
    assert "no saved cards" not in body.lower()


def test_non_v4_account_cards_page_renders_empty_state(
        patched_no_safari_open):
    """Non-V4 episodes still serve /account/cards (it's in static_pages
    unconditionally so explorers don't 404) but render an empty-state
    that doesn't leak whether V4 was on."""
    seed, t = _find_non_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/account/cards", timeout=5).read().decode()
    assert "no saved cards" in body.lower()


def test_v4_pdp_buy_link_appends_pay_flag(patched_no_safari_open):
    """V4 PDP's Buy Now link must include `&pay=1` so checkout renders
    payment fields. Without this, the agent can't even reach the
    payment form."""
    seed, t = _find_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/product/{t.params['target_sku']}",
        timeout=5).read().decode()
    # The buy_link is rendered inside an HTML href attribute, where
    # `&` is HTML-escaped to `&amp;`. The browser normalizes this
    # back so taps still hit /checkout?sku=...&pay=1.
    expected = (
        f"/checkout?sku={t.params['target_sku']}&amp;pay=1")
    assert expected in body


def test_non_v4_pdp_buy_link_omits_pay_flag(patched_no_safari_open):
    seed, t = _find_non_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/product/{t.params['target_sku']}",
        timeout=5).read().decode()
    assert "&pay=1" not in body
    assert f"/checkout?sku={t.params['target_sku']}" in body


def test_v4_checkout_page_renders_payment_fields(patched_no_safari_open):
    """When the checkout URL has `?pay=1`, the form gains three extra
    inputs: pay_card_last4, pay_exp_mm, pay_exp_yy."""
    seed, t = _find_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/checkout?sku={t.params['target_sku']}&pay=1",
        timeout=5).read().decode()
    for field in ("pay_card_last4", "pay_exp_mm", "pay_exp_yy"):
        assert f'name="{field}"' in body, (
            f"V4 checkout page missing {field}")
    # CVV was intentionally NOT included.
    assert 'name="pay_cvv"' not in body


def test_non_v4_checkout_page_has_no_payment_fields(
        patched_no_safari_open):
    """Without `?pay=1` (V0.5 default), checkout renders only the 5
    shipping fields."""
    seed, t = _find_non_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/checkout?sku={t.params['target_sku']}",
        timeout=5).read().decode()
    for field in ("pay_card_last4", "pay_exp_mm", "pay_exp_yy"):
        assert f'name="{field}"' not in body


def test_v4_verifier_passes_with_personal_card(patched_no_safari_open):
    """End-to-end V4 round-trip: POST with personal card fields →
    verifier PASS."""
    seed, t = _find_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    _post(f"{site.base_url}/checkout", _full_post_payload(t))
    passed, results = _verify(reader, t)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"V4 verifier should PASS with personal card; failed: "
        f"{[(r.kind, r.label) for r in failed]}")


def test_v4_verifier_fails_with_work_card(patched_no_safari_open):
    """Using the Work card instead of PERSONAL must FAIL the V4
    verifier — that's the V4 disambiguation signal."""
    seed, t = _find_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    payload = _full_post_payload(t)
    work = t.params["work_card"]
    payload["pay_card_last4"] = work["last4"]
    payload["pay_exp_mm"]     = work["exp_mm"]
    payload["pay_exp_yy"]     = work["exp_yy"]
    _post(f"{site.base_url}/checkout", payload)
    passed, results = _verify(reader, t)
    assert passed is False
    # At least one of the pay_* checks must be the failing one.
    pay_fails = [r for r in results
                  if r.kind == "attribute_eq"
                  and "pay_" in r.label
                  and r.status != "pass"]
    assert pay_fails
