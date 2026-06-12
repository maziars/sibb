"""gen_safari_rsvp_form — L1 round-trip test.

Mirrors `test_safari_bookmark_generator.py` but on the harness-served
form path.

For each test:
  1. Spec validates.
  2. Apply the spec → MockSite spawns with static_pages={"/event":
     rsvp_event} and page_seed set. Safari is NOT actually opened
     (we skip open_in_safari by not having a real sim — the fake
     reader has no UDID + we override open_at_start). The HTTP
     server IS running so we can hit it from the test.
  3. Run the verifier BEFORE → FAIL (no submission yet).
  4. Simulate the agent's action by POSTing the right form values
     to /rsvp via urllib.
  5. Run the verifier AFTER → PASS.

Coverage:
  - exists/count/attribute_eq pass when the agent submits the
    correct values
  - count check fails when the agent submits TWICE
  - exists check fails when the agent submits to the wrong path
  - attribute_eq fails when the agent submits a wrong field value
  - decoy-only click (POST to /__sibb_decoy__) does NOT satisfy the
    verifier (decoy filter is on by default)
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
from sibb_task_generator_v3 import gen_safari_rsvp_form
from sibb_verify import BaselineSnapshot, blocking_pass, run_checks

pytestmark = pytest.mark.fast


# ─────────────────────────── helpers ──────────────────────────────────


def _post(url: str, data: dict):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    return urllib.request.urlopen(req, timeout=5)


def _get(url: str):
    return urllib.request.urlopen(url, timeout=5)


def _verify(reader, task, baseline=None):
    results = asyncio.run(
        run_checks(reader, task.verify_checks, baseline=baseline))
    return blocking_pass(results), results


def _capture(reader, task):
    # MockSite's `mock_site.submissions` requires no baseline (the
    # identity check kind isn't used here). But run_checks accepts
    # baseline=None — so this helper is for parity with the bookmark
    # test only. Return None.
    return None


def _apply(reader, task):
    report = asyncio.run(apply_initial_state(reader, task))
    assert not report.get("errors"), \
        f"state setup failed: {report['errors']}"
    return report


@pytest.fixture
def patched_no_safari_open(monkeypatch):
    """Skip `open_in_safari` (no real sim). The MockSite HTTP server
    still spawns on 127.0.0.1 — the test directly POSTs to it."""
    # `open_in_safari` is imported lazily from sibb_state's
    # `_apply_mock_site`. Monkeypatch the symbol it imports.
    import sibb_mock_site

    def _noop(udid, url, **kwargs):
        pass
    monkeypatch.setattr(sibb_mock_site, "open_in_safari", _noop)

    async def _noop_terminate(udid):
        pass
    monkeypatch.setattr(sibb_state, "_safari_terminate", _noop_terminate)
    yield


# ─────────────────────────── round-trip ───────────────────────────────


def test_rsvp_spec_validates():
    random.seed(1)
    t = gen_safari_rsvp_form()
    assert validate_spec(t.initial_state.spec) == []
    assert t.apps == ["Safari"]
    # One mock_site entry; the rest are springboard noise.
    sites = [e for e in t.initial_state.spec
              if e.get("type") == "mock_site"]
    assert len(sites) == 1
    s = sites[0]
    assert s["static_pages"] == {"/event": "rsvp_event"}
    assert s["start_path"] == "/event"
    assert isinstance(s["page_seed"], int) and s["page_seed"] > 0


def _sweep_rsvp_generator(gen_fn, page_template_name, seed_range=40):
    """Shared 40-seed sweep used by both the unclipped + clipped tests.
    For each seed: assert the per-seed font_size_px is in range,
    `form_triggers_auto_zoom` is synced, and the rendered page HTML
    contains the matching `font-size:Npx` rule. Returns
    `(zooms_seen, no_zooms_seen)`."""
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    fn = PAGE_REGISTRY[page_template_name]
    zooms_seen, no_zooms_seen = 0, 0
    for seed in range(seed_range):
        random.seed(seed)
        t = gen_fn()
        px = t.params["form_font_size_px"]
        assert px in (13, 14, 15, 16, 17, 18), \
            f"unexpected font_size_px={px} (seed={seed})"
        assert t.params["form_triggers_auto_zoom"] is (px < 16), \
            f"form_triggers_auto_zoom flag desynced (seed={seed})"
        page_rng = random.Random(
            compute_path_seed(t.params["page_seed"], "/event"))
        html = fn(page_rng)
        assert f"font-size:{px}px" in html, (
            f"page CSS missing font-size:{px}px (seed={seed}); "
            f"head must contain a <style> block.")
        if px < 16:
            zooms_seen += 1
        else:
            no_zooms_seen += 1
    return zooms_seen, no_zooms_seen


def test_rsvp_form_font_size_in_split_range_and_exposed_as_param():
    """Step 5b (2026-06-07): the generator must pick a per-seed
    font-size from {13..18} (iOS Safari auto-zoom threshold = 16 px)
    and expose both the chosen size and a derived
    `form_triggers_auto_zoom` flag as task params. The rendered HTML
    must reflect the same value via the `<style>` block injected by
    `page_skeleton`.

    Also sanity-checks the split: across many seeds, we get BOTH
    zoom-triggering and zoom-safe sizes — the corpus naturally
    probes both conditions.
    """
    z, nz = _sweep_rsvp_generator(gen_safari_rsvp_form, "rsvp_event")
    assert z >= 1 and nz >= 1, (
        f"40 seeds should produce both conditions; "
        f"got zooms={z}, no_zooms={nz}")


def test_rsvp_clipped_form_font_size_renders_correctly_across_seeds():
    """The clipped variant uses the SAME `rsvp_event_choices` RNG path,
    so it must also expose `form_font_size_px` + `form_triggers_auto_zoom`
    AND its rendered HTML must reflect the chosen font-size. Mirrors
    the unclipped sweep so a future regression in either path is
    caught at the same fidelity (post-review nit from Step 5e —
    the original single-seed parity test only inspected params).
    """
    from sibb_task_generator_v3 import gen_safari_rsvp_form_clipped
    z, nz = _sweep_rsvp_generator(
        gen_safari_rsvp_form_clipped, "rsvp_event_clipped")
    assert z >= 1 and nz >= 1, (
        f"40 seeds should produce both conditions for clipped; "
        f"got zooms={z}, no_zooms={nz}")


def test_rsvp_align_in_choices_covers_all_three_values_and_renders():
    """Step 5g (2026-06-07): `rsvp_event_choices` must pick `align`
    from {left, center, right} (~uniform), and the rendered HTML must
    reflect the chosen value via a `text-align:<align>` CSS rule on
    the form+distractor wrapper div. Across many seeds we should
    cover all three values."""
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    from harness_pages import rsvp_event_choices
    seen = {"left": 0, "center": 0, "right": 0}
    for template_name, gen_fn in (
            ("rsvp_event", gen_safari_rsvp_form),
            ("rsvp_event_clipped", None)):  # clipped reuses same cfg path
        fn = PAGE_REGISTRY[template_name]
        for seed in range(60):
            page_rng = random.Random(compute_path_seed(seed * 13 + 1, "/event"))
            cfg = rsvp_event_choices(page_rng)
            assert cfg["align"] in ("left", "center", "right"), \
                f"unexpected align={cfg['align']!r} (seed={seed})"
            seen[cfg["align"]] = seen.get(cfg["align"], 0) + 1
            # Re-derive the same RNG state for the template body — the
            # template re-runs rsvp_event_choices internally with its
            # own copy of the rng, so use a fresh stream.
            page_rng = random.Random(compute_path_seed(seed * 13 + 1, "/event"))
            html = fn(page_rng)
            assert f'text-align:{cfg["align"]}' in html, (
                f"rendered HTML missing text-align:{cfg['align']} "
                f"(template={template_name}, seed={seed})")
    assert min(seen.values()) >= 1, \
        f"60 seeds × 2 templates should cover all three alignments; got {seen}"


def test_rsvp_paired_cancel_landscape_and_rendering():
    """Step 5h (2026-06-07) + 5L-A (2026-06-08): every form has 2-3
    inline decoys rendered IN THE SAME form-action row as the real
    Submit, using `formaction="/__sibb_decoy__"` + `formnovalidate`.
    The paired-cancel field (set on ~50% of seeds) adds ONE additional
    Cancel-like decoy to that row.

    Invariants checked across 80 seeds:
      * Every form has at least one `formaction="/__sibb_decoy__"`
        button (inline-decoy contract).
      * Every form uses a flex-row wrapper for the action area.
      * When paired_cancel is set, that SPECIFIC label appears in the
        rendered HTML.
      * The 80-seed sweep covers BOTH paired and unpaired branches.
      * Paired-label sample covers >=2 of the 3 options across the
        paired subset (Cancel, Reset, Discard Changes).
    """
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    from harness_pages import rsvp_event_choices
    fn = PAGE_REGISTRY["rsvp_event"]
    paired, unpaired = 0, 0
    paired_labels = set()
    for seed in range(80):
        page_rng = random.Random(compute_path_seed(seed * 7 + 3, "/event"))
        cfg = rsvp_event_choices(page_rng)
        assert cfg["paired_cancel"] in (
            None, "Cancel", "Reset", "Discard Changes"), \
            f"unexpected paired_cancel={cfg['paired_cancel']!r}"
        assert isinstance(cfg["paired_first"], bool)
        page_rng = random.Random(compute_path_seed(seed * 7 + 3, "/event"))
        html = fn(page_rng)
        # Inline-decoy contract: every form has formaction + flex row.
        assert 'formaction="/__sibb_decoy__"' in html, (
            f"every form should have inline decoys (seed={seed})")
        assert "formnovalidate" in html, (
            f"every form should have formnovalidate (seed={seed})")
        assert "display:flex" in html, (
            f"every form should have a flex action-row (seed={seed})")
        if cfg["paired_cancel"]:
            paired += 1
            paired_labels.add(cfg["paired_cancel"])
            # The paired-cancel label must appear in the HTML.
            assert cfg["paired_cancel"] in html, (
                f"paired decoy label {cfg['paired_cancel']!r} not "
                f"in HTML (seed={seed})")
        else:
            unpaired += 1
    assert paired >= 10 and unpaired >= 10, (
        f"80 seeds should give ~half paired / ~half unpaired; "
        f"got paired={paired}, unpaired={unpaired}")
    # Across paired seeds we should see at least 2 of the 3 labels
    # (3 options × 50% paired × 80 seeds → plenty of coverage).
    assert len(paired_labels) >= 2, (
        f"expected coverage of >=2 paired labels, got {paired_labels}")


def test_rsvp_paired_decoy_is_inside_real_rsvp_form():
    """The paired decoy must live INSIDE the same `<form action="/rsvp">`
    block as the real Submit — that's the whole point of the
    `formaction` override pattern (a click on the decoy bypasses the
    form's `action` and POSTs to the decoy path instead).
    """
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    from harness_pages import rsvp_event_choices
    fn = PAGE_REGISTRY["rsvp_event"]
    # Find a seed that produces a paired form.
    for seed in range(200):
        page_rng = random.Random(compute_path_seed(seed * 11 + 17, "/event"))
        cfg = rsvp_event_choices(page_rng)
        if cfg["paired_cancel"]:
            page_rng = random.Random(
                compute_path_seed(seed * 11 + 17, "/event"))
            html = fn(page_rng)
            # The /rsvp form opens at `<form action="/rsvp"` and closes
            # at the matching `</form>` — the paired decoy's
            # formaction must land between them.
            rsvp_open = html.index('action="/rsvp"')
            rsvp_close = html.index("</form>", rsvp_open)
            decoy_pos = html.index('formaction="/__sibb_decoy__"')
            assert rsvp_open < decoy_pos < rsvp_close, (
                f"paired decoy must be INSIDE the /rsvp form "
                f"(seed={seed}, rsvp_open={rsvp_open}, "
                f"decoy={decoy_pos}, rsvp_close={rsvp_close})")
            return
    raise AssertionError(
        "no paired seed found in first 200 seeds — coverage is too "
        "low or paired_cancel is broken")


def test_rsvp_submit_position_varies_across_seeds():
    """Step 5L-A (2026-06-08): Submit must NOT always be the first
    button in the rendered DOM order. With the bottom-stack distractors
    pulled INTO the real form and the action-row shuffled via
    `inline_decoy_order`, the real Submit's index among the action-row
    buttons should vary per seed.

    Across 80 seeds, Submit's position index distribution must include
    at least 3 distinct values (else the shuffle isn't randomizing).
    """
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    import re as _re
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    from harness_pages import rsvp_event_choices
    fn = PAGE_REGISTRY["rsvp_event"]
    positions = []
    for seed in range(80):
        page_rng = random.Random(compute_path_seed(seed * 11 + 5, "/event"))
        cfg = rsvp_event_choices(page_rng)
        page_rng = random.Random(compute_path_seed(seed * 11 + 5, "/event"))
        html = fn(page_rng)
        # Find all `<button type="submit"` and locate the real Submit
        # by `submit_label`. Real Submit is the only button WITHOUT a
        # `formaction` attribute (decoys all carry formaction).
        btn_re = _re.compile(
            r'<button[^>]*type="submit"([^>]*)>([^<]+)</button>')
        for idx, m in enumerate(btn_re.finditer(html)):
            attrs, label = m.group(1), m.group(2)
            if "formaction" not in attrs and label == cfg["submit_label"]:
                positions.append(idx)
                break
    assert len(set(positions)) >= 3, (
        f"Submit position should vary across seeds; got distinct "
        f"positions = {sorted(set(positions))}")
    # Sanity: Submit should NOT always be at index 0 (the original bug).
    n_at_zero = sum(1 for p in positions if p == 0)
    assert n_at_zero < 80, (
        f"Submit at index 0 in all 80 seeds — randomization broken")


def test_rsvp_align_maps_to_justify_content_in_flex_row():
    """Step 5L-B (2026-06-08): When the action-row is a flex container
    (2+ buttons, which is now the common case), `justify-content` must
    match the per-seed `align`. text-align inheritance doesn't work
    across a flex container — children are positioned by
    justify-content, defaulting to flex-start (left).

    Map: left → flex-start, center → center, right → flex-end.

    Across 60 seeds we should see at least one form for each align
    value with the corresponding justify-content rendered.
    """
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    from harness_pages import rsvp_event_choices
    fn = PAGE_REGISTRY["rsvp_event"]
    expected = {"left": "flex-start", "center": "center",
                "right": "flex-end"}
    seen = {k: 0 for k in expected}
    for seed in range(60):
        page_rng = random.Random(compute_path_seed(seed * 17 + 9, "/event"))
        cfg = rsvp_event_choices(page_rng)
        page_rng = random.Random(compute_path_seed(seed * 17 + 9, "/event"))
        html = fn(page_rng)
        if "display:flex" not in html:
            continue  # 1-button case wouldn't use a flex container
        wanted_justify = expected[cfg["align"]]
        assert f"justify-content:{wanted_justify}" in html, (
            f"seed={seed} align={cfg['align']!r}: expected "
            f"justify-content:{wanted_justify} in HTML, got: "
            f"{[s for s in html.splitlines() if 'flex' in s][:2]}")
        seen[cfg["align"]] += 1
    assert all(v > 0 for v in seen.values()), (
        f"All three align values should appear across 60 seeds; "
        f"got {seen}")


def test_rsvp_distractor_stack_removed_from_below_form():
    """Step 5L-A: with decoys pulled into the form, there must be NO
    standalone `<form action="/__sibb_decoy__">` block below the real
    `/rsvp` form. Closes the structural shortcut where the real Submit
    was always above the bottom-stack distractors."""
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY, compute_path_seed
    fn = PAGE_REGISTRY["rsvp_event"]
    for seed in range(40):
        page_rng = random.Random(compute_path_seed(seed * 19, "/event"))
        html = fn(page_rng)
        assert '<form action="/__sibb_decoy__"' not in html, (
            f"seed={seed}: standalone decoy form found below /rsvp "
            f"— bottom-stack distractor_buttons() call should be gone")


def test_rsvp_align_exposed_in_generator_params_or_seed_stable():
    """The generator's `params` doesn't need to surface `align`
    directly (it's a presentation detail, not a verifier input), but
    the value MUST be deterministic from `page_seed` — re-deriving the
    rng with the same seed must produce the same align."""
    import harness_pages  # noqa: F401  populate PAGE_REGISTRY
    from harness_layout import compute_path_seed
    from harness_pages import rsvp_event_choices
    for seed in range(20):
        random.seed(seed)
        t = gen_safari_rsvp_form()
        rng_a = random.Random(compute_path_seed(t.params["page_seed"], "/event"))
        rng_b = random.Random(compute_path_seed(t.params["page_seed"], "/event"))
        assert rsvp_event_choices(rng_a)["align"] == \
                rsvp_event_choices(rng_b)["align"]


def test_rsvp_instruction_has_port_placeholder_before_apply():
    """Step 5i (2026-06-07): generator emits a `{port:<site_id>}`
    placeholder in both the instruction and params["event_url"]. The
    placeholder is resolved by `apply_initial_state` AFTER the
    handler spawns MockSite (port is OS-assigned)."""
    random.seed(1)
    t = gen_safari_rsvp_form()
    token = "{port:" + t.params["site_id"] + "}"
    assert token in t.instruction, (
        f"instruction missing port placeholder {token!r}; "
        f"got: {t.instruction!r}")
    assert token in t.params["event_url"], (
        f"params.event_url missing placeholder {token!r}; "
        f"got: {t.params['event_url']!r}")


def test_rsvp_apply_resolves_port_placeholder(patched_no_safari_open):
    """After `apply_initial_state` runs, the placeholder is replaced
    by the live MockSite port in BOTH `task.instruction` and string
    `task.params` values. The agent (reading task.instruction at
    first user turn) gets a navigable URL."""
    from sibb_mock_site import get_site
    random.seed(6)
    t = gen_safari_rsvp_form()
    site_id = t.params["site_id"]
    token = "{port:" + site_id + "}"
    # Pre-apply: placeholder is present (sanity).
    assert token in t.instruction
    reader = FakeXCUITestReader()
    report = _apply(reader, t)
    # Post-apply: placeholder replaced by live port (a digit string).
    site = get_site(site_id)
    assert site is not None and site.port is not None
    port_str = str(site.port)
    assert token not in t.instruction, \
        f"placeholder NOT resolved in instruction: {t.instruction!r}"
    assert f":{port_str}/event" in t.instruction, (
        f"resolved instruction missing :PORT/event with port={port_str}; "
        f"got: {t.instruction!r}")
    assert token not in t.params["event_url"]
    assert port_str in t.params["event_url"]
    # And the apply report records the resolution.
    resolutions = report.get("port_resolutions") or []
    assert any(r["site_id"] == site_id and r["port"] == site.port
               for r in resolutions), \
        f"report missing port_resolutions for {site_id}; got {report}"


def test_rsvp_apply_unresolved_placeholder_is_reported_as_error(
        patched_no_safari_open):
    """If a `{port:<sid>}` token references a site_id that doesn't
    correspond to a bound MockSite (e.g., handler failed earlier),
    the resolver leaves the token as-is and records an error in the
    report so the runner can fail loudly rather than ship the agent
    a literal `{port:...}` URL."""
    from sibb_mock_site import get_site
    random.seed(7)
    t = gen_safari_rsvp_form()
    # Inject a bogus placeholder for a non-existent site_id.
    t.instruction = t.instruction + (
        " (extra: http://example.test:{port:nonexistent-9999}/x)")
    reader = FakeXCUITestReader()
    # Bypass `_apply`'s strict no-errors assertion — this test
    # WANTS to see the unresolved-placeholder error in the report.
    report = asyncio.run(apply_initial_state(reader, t))
    # The legit placeholder still resolves.
    assert "{port:" + t.params["site_id"] + "}" not in t.instruction
    # The bogus one persists.
    assert "{port:nonexistent-9999}" in t.instruction
    # And the report's errors include the unresolved site_id.
    assert any("nonexistent-9999" in e for e in report.get("errors", [])), (
        f"expected unresolved-placeholder error for nonexistent-9999; "
        f"got errors={report.get('errors')}")


def test_rsvp_template_resolves_and_renders():
    """The harness page template registered as `rsvp_event` exists
    in PAGE_REGISTRY and renders semantic HTML5 with the form fields
    we expect."""
    import harness_pages  # populates PAGE_REGISTRY
    from harness_layout import PAGE_REGISTRY
    assert "rsvp_event" in PAGE_REGISTRY
    fn = PAGE_REGISTRY["rsvp_event"]
    html = fn(random.Random(42))
    # Form posts to /rsvp.
    assert 'action="/rsvp"' in html
    assert 'method="POST"' in html
    # Three labelled fields with stable name= attributes (the verifier
    # selects on these; the user-visible LABEL is randomized).
    assert 'name="name"' in html
    assert 'name="contact"' in html
    assert 'name="attending"' in html
    # Submit button is randomized — but it's always inside a form.
    assert "<button type=\"submit\"" in html
    # Always-closed by default; the venue notes section uses the
    # opt-in closed default.
    assert "<details>" in html or "<details " in html


def test_rsvp_verifier_fails_before_action(patched_no_safari_open,
                                              tmp_path):
    random.seed(2)
    t = gen_safari_rsvp_form()
    reader = FakeXCUITestReader()
    _apply(reader, t)

    passed, results = _verify(reader, t)
    assert passed is False, (
        "verifier should FAIL before the agent submits the form")
    # Be specific: at least one of the count / exists / attribute_eq
    # checks must have failed.
    failed_kinds = [r.kind for r in results if r.status != "pass"]
    assert "exists" in failed_kinds or "count" in failed_kinds \
        or "attribute_eq" in failed_kinds


def test_rsvp_verifier_passes_after_correct_submission(
        patched_no_safari_open):
    random.seed(3)
    t = gen_safari_rsvp_form()
    reader = FakeXCUITestReader()
    _apply(reader, t)

    # Reach into the live MockSite via the registry.
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    assert site is not None
    base = site.base_url
    _post(f"{base}/rsvp", {
        "name": t.params["target_name"],
        "contact": t.params["target_contact"],
        "attending": t.params["target_attending"],
    })

    passed, results = _verify(reader, t)
    failed = [r for r in results if r.status != "pass"]
    assert passed is True, (
        f"verifier should PASS after correct submission; "
        f"failed checks: {[(r.kind, r.evidence) for r in failed]}")


def test_rsvp_verifier_fails_on_wrong_field_value(
        patched_no_safari_open):
    """attribute_eq must fail when the agent fills the wrong value
    (e.g. typo'd email)."""
    random.seed(4)
    t = gen_safari_rsvp_form()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    _post(f"{site.base_url}/rsvp", {
        "name": t.params["target_name"],
        "contact": "wrong-contact",  # WRONG
        "attending": t.params["target_attending"],
    })
    passed, results = _verify(reader, t)
    assert passed is False
    # Specifically the contact attribute_eq check should have failed.
    contact_fails = [r for r in results
                     if r.kind == "attribute_eq"
                     and "contact" in r.label and r.status != "pass"]
    assert contact_fails


def test_rsvp_verifier_fails_when_count_is_wrong(
        patched_no_safari_open):
    """`count == 1` catches both no-submission and double-submission."""
    random.seed(5)
    t = gen_safari_rsvp_form()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    # Submit twice with the correct values.
    payload = {
        "name": t.params["target_name"],
        "contact": t.params["target_contact"],
        "attending": t.params["target_attending"],
    }
    _post(f"{site.base_url}/rsvp", payload)
    _post(f"{site.base_url}/rsvp", payload)
    passed, results = _verify(reader, t)
    assert passed is False
    count_fail = next(
        r for r in results
        if r.kind == "count" and r.status != "pass")
    assert count_fail.evidence.get("actual") == 2


def test_rsvp_decoy_click_does_not_satisfy_verifier(
        patched_no_safari_open):
    """A POST to the decoy path (distractor-button click) is filtered
    out of the default `mock_site.submissions` view — so it doesn't
    satisfy the `exists path=/rsvp` check OR the `count==1` check."""
    random.seed(6)
    t = gen_safari_rsvp_form()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    from harness_layout import DECOY_PATH
    site = get_site(t.params["site_id"])
    # Agent clicks a distractor button → POST lands on DECOY_PATH.
    _post(f"{site.base_url}{DECOY_PATH}", {"action": "cancel"})
    passed, results = _verify(reader, t)
    assert passed is False, (
        "decoy-only click should NOT satisfy the verifier — the "
        "default fetcher filters decoys out so `exists path=/rsvp` "
        "and `count==1` both fail")
    # And `count` must be 0 (we filter decoys).
    count_evidence = next(
        r for r in results if r.kind == "count").evidence
    assert count_evidence.get("actual") == 0


def test_rsvp_event_page_is_served_with_form(patched_no_safari_open):
    """The harness page is actually being served at /event with the
    form fields we authored. Sanity check the GET path before relying
    on it for the round-trip."""
    random.seed(7)
    t = gen_safari_rsvp_form()
    reader = FakeXCUITestReader()
    _apply(reader, t)
    from sibb_mock_site import get_site
    site = get_site(t.params["site_id"])
    body = _get(f"{site.base_url}/event").read().decode()
    # Submit label is randomized per seed; the generator's params
    # record what the agent will actually see.
    assert t.params["form_submit_label"] in body
    assert 'name="name"' in body
    assert 'name="contact"' in body
    assert 'name="attending"' in body
    assert 'action="/rsvp"' in body
