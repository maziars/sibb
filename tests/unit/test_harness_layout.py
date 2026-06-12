"""Harness layout primitives — L1 tests.

Cover:
  * Seeded determinism (same RNG state → same output)
  * Layout-randomization actually varies output across seeds
  * AX-hygiene rules: every <input> paired with <label for=>, every
    submit lives inside <form method=POST>, no JavaScript, ARIA
    landmarks present
  * MockSite static_pages extension serves templates and routes POSTs
    to arbitrary paths into `submissions`
"""

from __future__ import annotations

import random
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

import pytest

import sibb_mock_site
from harness_layout import (
    FormField, collapsed_section, distractor_buttons, esc,
    filler_paragraphs, page_skeleton, random_pad, shuffled_fields,
    submit_form,
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


class _HtmlAuditor(HTMLParser):
    """Walks an HTML document and records:
      * input names + their labelled-by linkage
      * <form> action + method
      * <button type=submit> nesting under a form
      * ARIA landmarks
      * script tags (must be absent)
    """

    def __init__(self) -> None:
        super().__init__()
        self.inputs: list = []            # (name, id, type)
        self.labels_for: set = set()      # ids that have a <label for=>
        self.forms: list = []             # (action, method)
        self.submit_buttons_in_form: int = 0
        self.submit_buttons_outside_form: int = 0
        self.scripts: int = 0
        self.aria_landmarks: list = []    # tag, label
        self.has_main: bool = False
        self._form_depth: int = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input":
            if a.get("type") == "hidden":
                return
            self.inputs.append(
                (a.get("name", ""), a.get("id", ""),
                 a.get("type", "text")))
        elif tag == "label":
            if a.get("for"):
                self.labels_for.add(a["for"])
        elif tag == "form":
            self._form_depth += 1
            self.forms.append((a.get("action", ""), a.get("method", "")))
        elif tag == "button":
            t = a.get("type", "submit")
            if t == "submit":
                if self._form_depth > 0:
                    self.submit_buttons_in_form += 1
                else:
                    self.submit_buttons_outside_form += 1
        elif tag == "script":
            self.scripts += 1
        elif tag == "main":
            self.has_main = True
            self.aria_landmarks.append(
                ("main", a.get("aria-label", "")))
        elif tag in ("nav", "aside", "header", "footer", "section",
                      "article"):
            if a.get("aria-label"):
                self.aria_landmarks.append((tag, a["aria-label"]))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form_depth = max(0, self._form_depth - 1)


def audit(html: str) -> _HtmlAuditor:
    a = _HtmlAuditor()
    a.feed(html)
    return a


# ─────────────────────── layout primitives — basics ───────────────────


def test_filler_paragraphs_deterministic_for_same_seed():
    a = filler_paragraphs(random.Random(1), n=3)
    b = filler_paragraphs(random.Random(1), n=3)
    assert a == b


def test_filler_paragraphs_varies_across_seeds():
    rngs = [random.Random(s) for s in range(8)]
    outputs = {filler_paragraphs(r, n=2) for r in rngs}
    # At least 5 distinct outputs across 8 seeds — guards against a
    # bug where the same paragraphs always come back.
    assert len(outputs) >= 5


def test_filler_paragraphs_n_paragraphs_match_request():
    html = filler_paragraphs(random.Random(42), n=4)
    assert html.count("<p>") == 4


def test_distractor_buttons_render_n_buttons_each_in_own_form():
    html = distractor_buttons(random.Random(5), n=3)
    a = audit(html)
    assert len(a.forms) == 3
    assert a.submit_buttons_in_form == 3
    assert a.submit_buttons_outside_form == 0


def test_distractor_buttons_n_zero_returns_empty():
    assert distractor_buttons(random.Random(0), n=0) == ""


def test_distractor_buttons_apply_zoom_safe_geometry():
    """Step 5c / 5d (2026-06-07): every distractor form must carry the
    8 px top margin and every button the 44 pt min-size. These styles
    were empirically derived from
    `sibb_probe_zoom_hit_zone.py` after Safari auto-zoom ghosted the
    middle button of a stacked group. See IOS_SIM_QUIRKS §21.

    Pinning the literal strings here means a future refactor that
    drops them — extracting to a class, moving to external CSS — has
    to update this test, which forces it to also update §21 and the
    distractor_buttons docstring.
    """
    html = distractor_buttons(random.Random(5), n=3)
    # Form-wrapper margin — 3 distractors → 3 occurrences.
    assert html.count('style="margin-top:8px"') == 3, (
        "every distractor form must keep the 8 px top margin "
        "(see IOS_SIM_QUIRKS §21).")
    # Per-button HIG geometry.
    assert html.count("min-height:44px") == 3
    assert html.count("min-width:44px") == 3
    assert html.count("padding:8px 16px") == 3


def test_submit_form_button_has_zoom_safe_geometry():
    """The real Submit button rendered via `submit_form` must also
    satisfy Apple HIG 44 pt minimum so it stays hittable under
    Safari auto-zoom. Step 5d (2026-06-07)."""
    from harness_layout import FormField, submit_form
    html = submit_form(
        action="/rsvp",
        fields_html=FormField(
            name="x", label="X", input_type="text").render(),
        submit_label="Send",
        form_label="form")
    assert "min-height:44px" in html
    assert "min-width:44px" in html
    assert "padding:8px 16px" in html


def test_distractor_buttons_labels_vary_across_seeds():
    seeds = list(range(8))
    label_sets = []
    for s in seeds:
        html = distractor_buttons(random.Random(s), n=3)
        labels = re.findall(r"<button[^>]*>([^<]+)</button>", html)
        label_sets.append(tuple(labels))
    distinct = set(label_sets)
    assert len(distinct) >= 4, (
        "distractor button labels should vary across seeds — "
        "generators rely on this for cheat-resistance")


def test_collapsed_section_wraps_content_in_details_summary():
    html = collapsed_section(
        random.Random(0), "Advanced", "<p>inner</p>")
    assert "<details" in html
    assert "<summary>Advanced</summary>" in html
    assert "<p>inner</p>" in html


def test_collapsed_section_defaults_to_closed():
    """Updated 2026-06-05 per critic round: random-by-default was
    hostile to generator authors who expect a closed section.
    Default is now `open_default=False`; randomization opts in via
    `collapsed_section.RANDOMIZE` sentinel."""
    for s in range(10):
        html = collapsed_section(random.Random(s), "T", "<p>x</p>")
        assert "<details open" not in html


def test_collapsed_section_randomize_sentinel_varies_across_seeds():
    states = []
    for s in range(20):
        html = collapsed_section(
            random.Random(s), "T", "<p>x</p>",
            open_default=collapsed_section.RANDOMIZE)
        states.append("<details open" in html)
    # Both states should appear across 20 seeds.
    open_count = sum(states)
    closed_count = len(states) - open_count
    assert open_count >= 3 and closed_count >= 3, (
        f"randomize sentinel produced lopsided split: "
        f"open={open_count} closed={closed_count}")


def test_collapsed_section_open_default_none_also_randomizes():
    """Back-compat: explicit `open_default=None` was the prior
    randomization signal; keep it working alongside the new sentinel.
    """
    states = set()
    for s in range(30):
        html = collapsed_section(
            random.Random(s), "T", "<p>x</p>", open_default=None)
        states.add("<details open" in html)
    assert True in states and False in states


def test_collapsed_section_open_default_overrides_seed():
    html_open = collapsed_section(
        random.Random(0), "T", "<p>x</p>", open_default=True)
    html_closed = collapsed_section(
        random.Random(0), "T", "<p>x</p>", open_default=False)
    assert "<details open" in html_open
    assert "<details open" not in html_closed


def test_random_pad_emits_pixel_margin_in_range():
    out = random_pad(random.Random(1), min_px=50, max_px=300)
    m = re.match(r'style="margin-top:(\d+)px"$', out)
    assert m is not None
    assert 50 <= int(m.group(1)) <= 300


def test_random_pad_varies_across_seeds():
    pxs = []
    for s in range(20):
        out = random_pad(random.Random(s), 40, 400)
        pxs.append(int(re.search(r"(\d+)px", out).group(1)))
    assert len(set(pxs)) >= 10


def test_shuffled_fields_orders_differ_across_seeds():
    fields = [
        FormField("name", "Name"),
        FormField("email", "Email", input_type="email"),
        FormField("phone", "Phone", input_type="tel"),
        FormField("notes", "Notes"),
    ]
    orders = set()
    for s in range(12):
        html = shuffled_fields(random.Random(s), fields)
        names = re.findall(r'name="([^"]+)"', html)
        orders.add(tuple(names))
    # 4! = 24 possible orders; with 12 seeds we should hit at least 5.
    assert len(orders) >= 5


def test_form_field_text_input_pairs_with_label():
    fld = FormField("email", "Email", input_type="email")
    a = audit(fld.render())
    assert len(a.inputs) == 1
    name, fid, itype = a.inputs[0]
    assert name == "email" and itype == "email"
    assert fid in a.labels_for


def test_form_field_hidden_is_emitted_without_label():
    fld = FormField("session_id", "ignored", input_type="hidden",
                     value="abc123")
    html = fld.render()
    assert "session_id" in html
    assert "abc123" in html
    assert "<label" not in html
    assert "<p>" not in html


# ─────────────────────────── AX-hygiene lint ──────────────────────────


def _build_sample_page(seed: int) -> str:
    """A representative composition exercising every helper. This is
    the contract the static-page template authors are expected to
    follow."""
    rng = random.Random(seed)
    fields = [
        FormField("name", "Name", required=True),
        FormField("email", "Email", input_type="email", required=True),
        FormField("guests", "Guests", input_type="number"),
        FormField("token", "ignored", input_type="hidden",
                  value=str(seed)),
    ]
    body = (
        filler_paragraphs(rng, n=2)
        + collapsed_section(
            rng, "Details",
            filler_paragraphs(rng, n=1, min_sentences=1, max_sentences=2))
        + f'<div {random_pad(rng, 60, 240)}>'
        + submit_form(
            action="/rsvp",
            fields_html=shuffled_fields(rng, fields),
            submit_label="RSVP",
            form_label="RSVP form")
        + "</div>"
        + distractor_buttons(rng, n=2)
        + filler_paragraphs(rng, n=2)
    )
    return page_skeleton(title="Event RSVP",
                          description="Sample event page",
                          body=body)


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_lint_every_input_has_a_paired_label(seed):
    """AX rule: every <input> (except hidden) must have a matching
    <label for=>. iOS Safari surfaces the linked label as the AX
    `label` of the input — without it, the agent sees an unlabeled
    text field and has to guess by position."""
    a = audit(_build_sample_page(seed))
    for name, fid, itype in a.inputs:
        assert fid, f"input {name!r} has no id (needed for label[for=])"
        assert fid in a.labels_for, (
            f"input {name!r} (id={fid!r}, type={itype}) is missing a "
            f"matching <label for={fid!r}> — breaks AX label surface")


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_lint_every_submit_button_is_inside_a_form(seed):
    """AX rule: submits must be `<button type=submit>` nested inside a
    `<form method=POST>`. Otherwise iOS treats them as a generic
    button and the form's POST surface never fires."""
    a = audit(_build_sample_page(seed))
    assert a.submit_buttons_outside_form == 0, (
        f"submit button outside <form> count = "
        f"{a.submit_buttons_outside_form}; harness pages must always "
        f"wrap submits in a <form>.")
    # And every form's method MUST be POST.
    for action, method in a.forms:
        assert method.lower() == "post", (
            f"form action={action!r} has method={method!r} — must be POST"
        )


def test_lint_no_javascript_in_composed_page():
    """AX rule: harness pages must be fully server-rendered.
    JavaScript shouldn't appear at all."""
    a = audit(_build_sample_page(42))
    assert a.scripts == 0


def test_lint_page_has_main_landmark():
    a = audit(_build_sample_page(7))
    assert a.has_main, "harness pages must wrap content in <main>"


def test_esc_escapes_html_metacharacters():
    assert esc("<x>") == "&lt;x&gt;"
    assert esc('a "b" c') == "a &quot;b&quot; c"
    assert esc("a & b") == "a &amp; b"


# ─────────────────────── MockSite static_pages extension ──────────────


@pytest.fixture
def static_site(monkeypatch):
    """Fresh MockSite with a couple of seeded static-page templates.
    Each template uses a different layout primitive so the served
    HTML genuinely varies across paths."""
    site_id = f"test-static-{uuid.uuid4().hex[:8]}"

    def event_page(rng):
        body = (
            filler_paragraphs(rng, n=2)
            + submit_form(
                action="/rsvp",
                fields_html=shuffled_fields(rng, [
                    FormField("name", "Name", required=True),
                    FormField("email", "Email", input_type="email"),
                ]),
                submit_label="RSVP",
                form_label="RSVP form")
            + distractor_buttons(rng, n=2)
            + filler_paragraphs(rng, n=2)
        )
        return page_skeleton(title="Event RSVP", body=body)

    def article_page(rng):
        body = (
            "<h1>Sample Article</h1>"
            + filler_paragraphs(rng, n=3)
        )
        return page_skeleton(title="Article", body=body)

    s = MockSite(site_id=site_id, static_pages={
        "/event/42": event_page,
        "/articles/spring-2026": article_page,
        "/about": "<!DOCTYPE html><html><body>"
                   "<main aria-label=\"About\">"
                   "<h1>About</h1></main></body></html>",
    })
    s.page_seed = 12345  # stable for the test
    s.start()
    try:
        yield s
    finally:
        s.stop()


def test_static_pages_get_serves_template_html(static_site):
    resp = _get(f"{static_site.base_url}/event/42")
    assert resp.status == 200
    body = resp.read().decode()
    assert "RSVP" in body
    assert "<main" in body


def test_static_pages_string_template_served_verbatim(static_site):
    resp = _get(f"{static_site.base_url}/about")
    assert resp.status == 200
    body = resp.read().decode()
    assert "<h1>About</h1>" in body


def test_static_pages_visit_recorded(static_site):
    _get(f"{static_site.base_url}/event/42")
    visits = static_site.visits()
    assert any(v["path"] == "/event/42" for v in visits)


def test_static_pages_callable_template_deterministic_per_path(
        static_site):
    """Same path → same rendered HTML across requests (replayability)."""
    a = _get(f"{static_site.base_url}/event/42").read()
    b = _get(f"{static_site.base_url}/event/42").read()
    assert a == b


def test_static_pages_callable_template_varies_across_paths(
        static_site):
    """Different paths → different rendered HTML even with the same
    page_seed (because per-path RNG seed mixes path hash)."""
    a = _get(f"{static_site.base_url}/event/42").read()
    b = _get(f"{static_site.base_url}/articles/spring-2026").read()
    assert a != b


def test_static_pages_unknown_path_returns_404(static_site):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{static_site.base_url}/nonexistent")
    assert exc.value.code == 404


def test_post_to_static_page_path_captured_in_submissions(static_site):
    """A POST to a non-credential path lands as a generic submission
    with `mode=path` and the form fields flattened."""
    _post(f"{static_site.base_url}/rsvp",
           {"name": "Alice", "email": "a@example.com"})
    subs = static_site.submissions()
    rsvp = [s for s in subs if s.get("mode") == "/rsvp"]
    assert len(rsvp) == 1
    assert rsvp[0]["fields"]["name"] == "Alice"
    assert rsvp[0]["fields"]["email"] == "a@example.com"


def test_post_to_static_page_returns_acknowledgement(static_site):
    """POST returns a small confirmation page (so the agent's UI flow
    has a destination after submit)."""
    resp = _post(f"{static_site.base_url}/buy", {"item": "x", "qty": "2"})
    assert resp.status == 200
    body = resp.read().decode()
    assert "Submitted" in body or "submitted" in body.lower()


def test_reset_clears_static_page_submissions(static_site):
    _post(f"{static_site.base_url}/rsvp", {"name": "X"})
    assert any(s.get("mode") == "/rsvp"
                for s in static_site.submissions())
    static_site.reset()
    assert static_site.submissions() == []


def test_static_pages_page_seed_changes_layout(monkeypatch):
    """Two MockSites with the same template but different page_seed
    produce different layouts. Locks in the seed-as-input contract."""
    def evt(rng):
        body = (filler_paragraphs(rng, n=2)
                + submit_form(action="/rsvp",
                              fields_html=FormField("n", "Name").render(),
                              submit_label="RSVP"))
        return page_skeleton(title="E", body=body)

    a = MockSite(site_id=f"test-A-{uuid.uuid4().hex[:8]}",
                  static_pages={"/event": evt})
    a.page_seed = 1
    b = MockSite(site_id=f"test-B-{uuid.uuid4().hex[:8]}",
                  static_pages={"/event": evt})
    b.page_seed = 999
    a.start()
    b.start()
    try:
        html_a = _get(f"{a.base_url}/event").read()
        html_b = _get(f"{b.base_url}/event").read()
        assert html_a != html_b, (
            "different page_seeds must produce different layouts")
    finally:
        a.stop()
        b.stop()
