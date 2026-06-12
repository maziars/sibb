"""L1 round-trip tests for `gen_safari_shop_filter_and_sort` (Q4
archetype, Step 5P, 2026-06-09).

Q4 differs from Q1/Q2 (`gen_safari_shop_pick_by_attrs`) in that the
agent reaches the winner via filter+sort cascade on `/browse`,
NOT by typing a query into a BM25 search bar. Same persona, same
verifier shape, same V4 saved-cards axis.

Mirrors the shape of `test_safari_shop_pick_by_attrs.py` plus
filter-cascade-specific checks: filter UI renders, winner is FIRST
under the canonical cascade, instruction names cat+brand+sort.
"""
from __future__ import annotations

import asyncio
import random
import urllib.parse
import urllib.request

import pytest

from fakes.fake_reader import FakeXCUITestReader
import sibb_state
from sibb_state import apply_initial_state
from sibb_spec import validate_spec
from sibb_task_generator_v3 import gen_safari_shop_filter_and_sort
from sibb_verify import blocking_pass, run_checks


pytestmark = pytest.mark.fast


# ─── helpers ──────────────────────────────────────────────────────────


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
    if p.get("use_saved_cards"):
        personal = p["personal_card"]
        payload["pay_card_last4"] = personal["last4"]
        payload["pay_exp_mm"]     = personal["exp_mm"]
        payload["pay_exp_yy"]     = personal["exp_yy"]
    return payload


def _find_v4_seed(start: int = 0, limit: int = 200):
    for seed in range(start, start + limit):
        random.seed(seed)
        t = gen_safari_shop_filter_and_sort()
        if t.params["use_saved_cards"]:
            return seed, t
    raise AssertionError("no V4 seed found")


def _find_non_v4_seed(start: int = 0, limit: int = 200):
    for seed in range(start, start + limit):
        random.seed(seed)
        t = gen_safari_shop_filter_and_sort()
        if not t.params["use_saved_cards"]:
            return seed, t
    raise AssertionError("no non-V4 seed found")


def _filter_url(t):
    """Build the canonical filter-cascade URL the agent is supposed
    to assemble (cat=X, brand=Y, sort=price_asc, max_price=Z)."""
    p = t.params
    qs = (
        f"cat={urllib.parse.quote(p['target_category'])}"
        f"&brand={urllib.parse.quote(p['target_brand'])}"
        f"&sort={p['canonical_sort']}"
        f"&max_price={p['target_max_price_cents']}"
    )
    return qs


# ─── spec + params ────────────────────────────────────────────────────


def test_q4_spec_validates():
    random.seed(1)
    t = gen_safari_shop_filter_and_sort()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Safari"]
    sites = [e for e in t.initial_state.spec
             if e.get("type") == "mock_site"]
    assert len(sites) == 1
    site = sites[0]
    # Q4-specific landing + browse; shared pdp/checkout/account_cards.
    assert site["static_pages"] == {
        "/":              "shop_q4_landing",
        "/browse":        "shop_q4_browse",
        "/product/":      "shop_pdp",
        "/checkout":      "shop_checkout",
        "/account/cards": "shop_account_cards",
    }
    assert site["start_path"] == "/"
    assert site["open_at_start"] is False
    assert isinstance(site["page_seed"], int) and site["page_seed"] > 0


def test_q4_params_are_consistent():
    random.seed(2)
    t = gen_safari_shop_filter_and_sort()
    p = t.params
    assert p["target_sku"].startswith("wm-")
    assert p["target_price_cents"] <= p["target_max_price_cents"]
    for k in ("persona_name", "persona_street", "persona_city",
              "persona_state", "persona_zip"):
        assert p[k], f"persona field {k} empty"
    assert "{port:" in p["landing_url"]
    assert p["canonical_sort"] == "price_asc"


def test_q4_seed_determinism():
    random.seed(42)
    a = gen_safari_shop_filter_and_sort()
    random.seed(42)
    b = gen_safari_shop_filter_and_sort()
    assert a.params["target_sku"]      == b.params["target_sku"]
    assert a.params["target_brand"]    == b.params["target_brand"]
    assert a.params["target_category"] == b.params["target_category"]
    assert a.params["persona_name"]    == b.params["persona_name"]
    assert a.params["use_saved_cards"] == b.params["use_saved_cards"]


def test_q4_instruction_names_cat_brand_and_sort():
    random.seed(3)
    t = gen_safari_shop_filter_and_sort()
    instr = t.instruction
    assert t.params["target_brand"] in instr
    assert t.params["target_category"] in instr
    assert "Price (low to high)" in instr
    assert "/browse" in instr
    assert "CHEAPEST" not in instr  # Q4 uses "cheapest" lowercase
    assert "cheapest" in instr.lower()
    assert t.params["persona_name"] in instr
    assert t.params["persona_zip"] in instr


def test_q4_winner_is_unique_cheapest_in_its_family():
    """Same uniqueness invariant as V0.5 — the agent's cascade
    surfaces a SPECIFIC SKU as first result, so the verifier's
    attribute_eq is unambiguous."""
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    for seed in range(25):
        random.seed(seed)
        t = gen_safari_shop_filter_and_sort()
        winner = next(s for s in cat.skus
                      if s.sku_id == t.params["target_sku"])
        siblings = cat.filter(
            brand=winner.brand,
            category=winner.global_category,
            max_price_cents=t.params["target_max_price_cents"])
        assert len(siblings) >= 2
        min_price = min(s.price_cents for s in siblings)
        assert winner.price_cents == min_price
        cheap_count = sum(
            1 for s in siblings if s.price_cents == min_price)
        assert cheap_count == 1


# ─── /browse template renders + filter cascade surfaces winner ──────


def test_q4_landing_has_no_search_bar(patched_no_safari_open):
    """Q4 landing must NOT have a `<input name="q">` — that would
    let the agent fall back to BM25 search, defeating the Q4
    filter-cascade exercise."""
    random.seed(9)
    t = gen_safari_shop_filter_and_sort()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/", timeout=5).read().decode()
    assert 'name="q"' not in body
    # Must have the /browse link as the entry point.
    assert 'href="/browse"' in body


def test_q4_browse_renders_filter_rows(patched_no_safari_open):
    random.seed(10)
    t = gen_safari_shop_filter_and_sort()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/browse", timeout=5).read().decode()
    for needle in ("Category:", "Brand:", "Sort by:",
                    "Price (low to high)", "Price (high to low)",
                    "Rating (high to low)"):
        assert needle in body, f"missing {needle!r}"


def test_q4_canonical_cascade_makes_winner_first(
        patched_no_safari_open):
    """The whole task contract: applying (cat=X, brand=Y,
    sort=price_asc, max_price=cap) puts the winner SKU FIRST in
    the rendered results. Pin across 10 seeds."""
    for seed in range(10):
        random.seed(seed)
        t = gen_safari_shop_filter_and_sort()
        reader = FakeXCUITestReader()
        _apply(reader, t)
        from sibb_mock_site import get_site
        site = get_site(t.params["site_id"])
        url = f"{site.base_url}/browse?{_filter_url(t)}"
        body = urllib.request.urlopen(url, timeout=5).read().decode()
        # Find the FIRST product card link.
        import re
        skus_in_order = re.findall(r'/product/(wm-\d+)', body)
        assert skus_in_order, (
            f"seed={seed}: no product cards rendered for "
            f"cascade {_filter_url(t)!r}")
        assert skus_in_order[0] == t.params["target_sku"], (
            f"seed={seed}: winner {t.params['target_sku']} is NOT "
            f"first in cascade results (first={skus_in_order[0]})")


def test_q4_browse_no_filters_shows_results_but_winner_not_first(
        patched_no_safari_open):
    """With no filters applied, /browse shows the price-asc sort of
    the cap'd pool. The winner is NOT first there (otherwise the
    agent could win without filtering at all). Pin this to make
    sure the cascade is doing real work."""
    random.seed(11)
    t = gen_safari_shop_filter_and_sort()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/browse?max_price={t.params['target_max_price_cents']}",
        timeout=5).read().decode()
    import re
    skus_in_order = re.findall(r'/product/(wm-\d+)', body)
    assert skus_in_order
    # We expect the winner to NOT be the first result of the unfiltered
    # cap'd pool — there are many cheaper SKUs across all categories
    # under the same cap. If this ever flakes, the catalog has shifted.
    # Cap is met but cat+brand aren't, so first is just the absolute
    # cheapest under cap, which is almost certainly not the winner.
    assert skus_in_order[0] != t.params["target_sku"], (
        "winner is first WITHOUT filters — the filter cascade isn't "
        "the only path to the winner, so Q4 doesn't differ from a "
        "single-click homepage selection")


# ─── verifier round-trip ──────────────────────────────────────────────


def test_q4_verifier_fails_before_action(patched_no_safari_open):
    random.seed(4)
    t = gen_safari_shop_filter_and_sort()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    passed, _ = _verify(reader, t)
    assert passed is False


def test_q4_verifier_passes_after_correct_post(patched_no_safari_open):
    random.seed(5)
    t = gen_safari_shop_filter_and_sort()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    _post(f"{site.base_url}/checkout", _full_post_payload(t))
    passed, results = _verify(reader, t)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"Q4 verifier should PASS; failed: "
        f"{[(r.kind, r.label) for r in failed]}")


def test_q4_verifier_fails_on_wrong_sku(patched_no_safari_open):
    random.seed(6)
    t = gen_safari_shop_filter_and_sort()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    payload = _full_post_payload(t)
    payload["sku"] = "wm-9999"
    _post(f"{site.base_url}/checkout", payload)
    passed, results = _verify(reader, t)
    assert passed is False


# ─── V4 axis composes with Q4 ────────────────────────────────────────


def test_q4_v4_axis_fires_for_some_seeds():
    on, off = 0, 0
    for seed in range(60):
        random.seed(seed)
        t = gen_safari_shop_filter_and_sort()
        if t.params["use_saved_cards"]:
            on += 1
        else:
            off += 1
    assert on >= 10 and off >= 10, (
        f"V4 split too lopsided in Q4: on={on}, off={off}/60")


def test_q4_v4_card_axis_matches_q1q2_for_same_page_seed():
    """KEY refactor invariant (Step 5P): for the same page_seed, Q4
    and Q1/Q2 cfgs agree on use_saved_cards/personal_card/work_card.
    Otherwise shop_pdp + shop_account_cards (which only know
    page_seed, not which generator's task is active) would render
    inconsistent V4 state."""
    from harness_pages import _shop_q4_task_cfg, _shop_task_cfg
    for ps in (12345, 67890, 1, 99999):
        q4 = _shop_q4_task_cfg(ps)
        q12 = _shop_task_cfg(ps)
        assert q4["use_saved_cards"] == q12["use_saved_cards"], ps
        assert q4["personal_card"]   == q12["personal_card"], ps
        assert q4["work_card"]       == q12["work_card"], ps


def test_q4_v4_verifier_includes_3_payment_blocks():
    seed, t = _find_v4_seed()
    pay_labels = [c["label"] for c in t.verify_checks
                  if c.get("attr", "").startswith("fields.pay_")]
    assert len(pay_labels) == 3, f"seed={seed}: {pay_labels}"


def test_q4_non_v4_verifier_has_no_payment_blocks():
    seed, t = _find_non_v4_seed()
    pay_checks = [c for c in t.verify_checks
                  if c.get("attr", "").startswith("fields.pay_")]
    assert pay_checks == []


def test_q4_v4_instruction_mentions_account_cards_and_personal():
    seed, t = _find_v4_seed()
    instr = t.instruction
    assert "/account/cards" in instr
    assert "PERSONAL" in instr


def test_q4_v4_account_cards_page_renders_two_distinct_cards(
        patched_no_safari_open):
    """Q4 reuses the shop_account_cards template — confirm it
    still renders correctly when the Q4 generator is the owner of
    the page_seed."""
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


def test_q4_v4_pdp_buy_link_appends_pay_flag(patched_no_safari_open):
    seed, t = _find_v4_seed()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = urllib.request.urlopen(
        f"{site.base_url}/product/{t.params['target_sku']}",
        timeout=5).read().decode()
    expected = (
        f"/checkout?sku={t.params['target_sku']}&amp;pay=1")
    assert expected in body


def test_q4_v4_verifier_fails_with_work_card(patched_no_safari_open):
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
    passed, _ = _verify(reader, t)
    assert passed is False
