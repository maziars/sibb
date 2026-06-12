"""Seeded layout primitives for Phase 4 harness pages.

The 2026-06-05 design decision (per the post-bookmark review): static
harness pages must NOT make the agent's life easy. Critical
interactive elements must be RANDOMLY positioned per seed so the
agent has to actually navigate the page — find the right button
among realistic distractors, scroll past filler content, expand the
right collapsed section. The benchmark's value depends on the page
LOOKING and BEHAVING like a real one, not on a layout the agent can
memorize.

Each helper is a pure function that takes a `random.Random` and
returns an HTML fragment. Generators compose them in their static
page templates. Determinism: same RNG state → same HTML, so an
episode-level seed makes the page replayable.

AX hygiene rules these helpers enforce (per IOS_SIM_QUIRKS §14):
- Always semantic HTML5 elements (no <div> as a button / link)
- Every <input> has a paired <label for=>
- Always wrap submits in <form method=POST> — never JS
- ARIA landmarks on structural containers
- No JavaScript anywhere

If a page composed from these helpers doesn't follow the rules,
`test_harness_page_lint.py` catches it at L1.
"""

from __future__ import annotations

import html as _html
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

_TEXTUAL_INPUT_TYPES = frozenset({
    "text", "email", "tel", "number", "url",
    "password", "search", "date", "time", "hidden",
})


def compute_path_seed(page_seed: int, path: str) -> int:
    """Re-derive the per-path seed that `MockSite` uses to construct
    the `random.Random` it passes to a static-page template. Lets
    generators construct the SAME RNG state independently, which is
    how they agree with the template on coupled random choices
    (e.g. `contact_type` — email vs phone — that affects both the
    instruction and the form field).

    Stable across processes (uses blake2b, not Python's randomized
    `hash()`). Must match the derivation in
    `sibb_mock_site.Handler._serve_static_page`.
    """
    import hashlib
    path_digest = int.from_bytes(
        hashlib.blake2b(path.encode("utf-8"),
                          digest_size=4).digest(),
        "big")
    return page_seed ^ path_digest


# Registry of harness-page template factories keyed by NAME. The
# `MockSite` spec dataclass (sibb_spec.py) carries names (not
# callables) so it stays JSON-serializable; `_apply_mock_site` in
# sibb_state.py resolves names to factory fns via this dict at apply
# time. Generators register their page templates here at import time:
#
#   @register_page("rsvp_form")
#   def rsvp_form(rng): return page_skeleton(...)
#
# The decorator is the recommended idiom but generators can also
# call `PAGE_REGISTRY[name] = fn` directly.
PAGE_REGISTRY: dict = {}


def register_page(name: str):
    """Decorator: register a harness-page template factory under
    `name` in `PAGE_REGISTRY`. Re-registration with the same name
    raises so two generators can't silently shadow each other.
    """
    def _wrap(fn):
        if name in PAGE_REGISTRY and PAGE_REGISTRY[name] is not fn:
            raise ValueError(
                f"harness page name {name!r} already registered "
                f"(by {PAGE_REGISTRY[name].__qualname__}); "
                f"pick a different name")
        PAGE_REGISTRY[name] = fn
        return fn
    return _wrap


# ─────────────────────────── data classes ─────────────────────────────


@dataclass
class FormField:
    """A single labelled form input — used by `shuffled_fields`."""
    name: str
    label: str
    input_type: str = "text"          # text / email / number / tel /
                                       # password / hidden / radio / etc.
    value: str = ""                   # optional pre-fill (or hidden)
    required: bool = False
    placeholder: str = ""

    # Multi-input form widgets need a separate primitive (planned
    # `RadioGroup` / `CheckboxGroup` / `SelectField`) because:
    #   - `radio` / `checkbox` use SHARED `name` across N inputs; this
    #     class is one-input-per-instance
    #   - `select` has nested `<option>` children
    # Until those exist, refuse to render rather than emitting broken
    # HTML that produces a working-looking but unusable page.
    _UNSUPPORTED_TYPES = frozenset({"radio", "checkbox", "select"})

    def render(self) -> str:
        if self.input_type in self._UNSUPPORTED_TYPES:
            raise NotImplementedError(
                f"FormField.input_type={self.input_type!r} is not "
                f"supported by the single-input render path. "
                f"radio / checkbox / select need a multi-input "
                f"primitive (RadioGroup / CheckboxGroup / SelectField) "
                f"— planned but not yet shipped (see harness_layout "
                f"docstring). For now, use {sorted(_TEXTUAL_INPUT_TYPES)}."
            )
        # Hidden inputs are emitted bare (no label / no wrapper).
        if self.input_type == "hidden":
            return (f'<input type="hidden" name="{esc(self.name)}" '
                    f'value="{esc(self.value)}">')
        attrs = [
            f'type="{esc(self.input_type)}"',
            f'name="{esc(self.name)}"',
            f'id="field-{esc(self.name)}"',
        ]
        if self.value:
            attrs.append(f'value="{esc(self.value)}"')
        if self.placeholder:
            attrs.append(f'placeholder="{esc(self.placeholder)}"')
        if self.required:
            attrs.append("required")
        # `autocomplete="off"` keeps iOS' Password AutoFill bar out of
        # plain-text fields (it's only useful on the existing MockSite
        # signin/signup forms; for harness pages it just adds noise).
        if self.input_type not in ("password",):
            attrs.append('autocomplete="off"')
        return (
            f'<p><label for="field-{esc(self.name)}">'
            f'{esc(self.label)}</label> '
            f'<input {" ".join(attrs)}></p>'
        )


# ─────────────────────────── helpers ──────────────────────────────────


def esc(s: str) -> str:
    """HTML-escape user-content strings. Layout primitives use this on
    every dynamic value to keep generators from accidentally injecting
    raw HTML / breaking the lint."""
    return _html.escape(str(s), quote=True)


# Filler vocabulary — short, generic phrases. Composed into paragraphs
# of varied length to make the page feel like real long-form content
# (Wikipedia article, blog post, recipe intro, etc.) without
# pretending to BE one. Generators can layer their own domain-specific
# paragraphs alongside this filler.
_LOREM_SENTENCES = [
    ("This section covers background context relevant to the topic "
     "at hand."),
    ("Several factors should be considered before proceeding to "
     "the next step."),
    ("Recent updates to the policy include a clearer explanation of "
     "the procedure."),
    ("Many readers find this part of the document useful as a "
     "general reference."),
    ("The remainder of the page describes typical use cases in "
     "more detail."),
    ("Please note that the items listed below may vary depending "
     "on availability."),
    ("This information is provided for general guidance and is "
     "subject to change."),
    ("Additional details can be found in the supplemental materials "
     "linked elsewhere on this site."),
    ("Most users will not need to interact with the advanced "
     "options described above."),
    ("Frequently asked questions appear later in the document."),
    ("The contents of this section are intended for informational "
     "purposes only."),
    ("If you encounter any issues, consult the help center for "
     "guidance."),
]


def filler_paragraphs(rng: random.Random,
                       n: Optional[int] = None,
                       *,
                       min_sentences: int = 2,
                       max_sentences: int = 5) -> str:
    """Render `n` paragraphs of filler content (lorem-ish).

    If `n` is None, picks 1-3 paragraphs from `rng`. Each paragraph
    has 2-5 sentences (configurable). This is the primary way to
    push critical buttons below the fold — adding 3-4 paragraphs
    above a form forces the agent to scroll to find the Submit.
    """
    if n is None:
        n = rng.randint(1, 3)
    out: List[str] = []
    for _ in range(n):
        n_sents = rng.randint(min_sentences, max_sentences)
        sents = rng.sample(_LOREM_SENTENCES,
                            min(n_sents, len(_LOREM_SENTENCES)))
        out.append(f"<p>{esc(' '.join(sents))}</p>")
    return "\n".join(out)


# Realistic distractor-button labels. Drawn from common web UIs:
# checkout flows, settings pages, signup forms, document editors.
# Each label suggests an action that would be a NATURAL mistake if
# the agent picks by position or quickly-scanned text alone.
_DISTRACTOR_BUTTON_LABELS = [
    "Save Draft",
    "Save & Continue Later",
    "Cancel",
    "Reset",
    "Back",
    "Skip This Step",
    "Discard Changes",
    "Save for Later",
    "Apply",
    "Help",
    "Print",
    "Export",
    "Preview",
    "Decline",
    "Remind Me Later",
]


#: Canonical decoy path for distractor-button submissions.
#: MockSite tags submissions to this path with `is_decoy=True`, and
#: the public `submissions()` / `mock_site.submissions` fetcher
#: default-filters them out so verifier authors can't accidentally
#: count a decoy click as completion.
DECOY_PATH = "/__sibb_decoy__"


def distractor_buttons(rng: random.Random,
                        n: Optional[int] = None,
                        *,
                        form_path: str = DECOY_PATH) -> str:
    """Render N distractor buttons. Each is wrapped in its own
    `<form method=POST action=form_path>` so they're real, clickable,
    AX-readable submit buttons.

    Default `form_path` is `DECOY_PATH` ("/__sibb_decoy__"); the
    MockSite tags those submissions with `is_decoy=True` and the
    standard `mock_site.submissions` fetcher filters them out unless
    the verifier opts in with `include_decoys=True`. The path is
    namespaced (double-underscore + project prefix) so it can't be
    confused with a real harness route.

    Returns an empty string when `n=0` so the helper can be safely
    no-op'd in tests that don't want decoys.
    """
    if n is None:
        n = rng.randint(2, 4)
    if n <= 0:
        return ""
    picks = rng.sample(_DISTRACTOR_BUTTON_LABELS,
                        min(n, len(_DISTRACTOR_BUTTON_LABELS)))
    blocks: List[str] = []
    for label in picks:
        # Step 5c / 5d (2026-06-07): zoom-safe button geometry.
        #
        # 5c — 8 px top margin per form. Under iOS Safari page-zoom,
        # WebKit inflates adjacent button AX frames by ~2 pt each
        # (line-box rounding via the zoom transform). Without a gap,
        # neighbor frames overlap by 2 pt and iOS hit-test snaps the
        # sandwiched middle button to its neighbor.
        #
        # 5d — `min-height/min-width: 44px` per Apple HIG §Layout
        # ("minimum tappable area"). Empirical probe
        # `sibb_probe_zoom_hit_zone.py` showed the post-5c layout
        # STILL ghosted the narrow middle button (a 94-px-wide
        # "Preview" between two 162-px-wide neighbors) because
        # iOS's "fat finger" hit-test inflates wide neighbors'
        # touch zones by ~11 pt each direction under zoom while
        # the narrower middle button doesn't compensate. Forcing
        # every button to >=44 pt × 44 pt gives the middle button
        # enough rendered hit-area to win the contest. `padding`
        # gives breathing room for the rendered visual.
        #
        # See IOS_SIM_QUIRKS §21 ("Distractor-stack hit-zones
        # under auto-zoom") for the empirical hit-zone data.
        button_style = (
            "min-height:44px;min-width:44px;padding:8px 16px")
        blocks.append(
            f'<form action="{esc(form_path)}" method="POST" '
            f'aria-label="{esc(label)} action" '
            f'style="margin-top:8px">\n'
            f'  <button type="submit" name="action" '
            f'value="{esc(label.lower().replace(" ", "_"))}" '
            f'style="{button_style}">'
            f'{esc(label)}</button>\n'
            f'</form>'
        )
    return "\n".join(blocks)


_RANDOMIZE_OPEN = object()  # sentinel: "randomize via rng" vs "closed"


def collapsed_section(rng: random.Random,
                       title: str,
                       content: str,
                       *,
                       open_default: Any = False) -> str:
    """Wrap `content` in a `<details>` element — a real expand/collapse
    that iOS Safari exposes via AX.

    **Default changed 2026-06-05**: `open_default=False` so callers that
    expect "the agent must scroll to find this content and then TAP
    the summary to expand it" get that behavior consistently. Pass
    `open_default=True` for always-open. Pass
    `open_default=collapsed_section.RANDOMIZE` (or
    `open_default=None`) to randomize 50/50 via `rng` — that's the
    opt-in randomization, no longer the default.

    Reasonable usage: tuck "advanced options" or "terms checkbox"
    into one of these. The agent must `TAP` the `<summary>` to expand.

    NOTE on iOS AX (UNVERIFIED 2026-06-05 — Critic 3 flagged): WebKit
    is expected to prune inner content from the AX tree when the
    `<details>` is closed, so a closed section is invisible to the
    agent until expanded. If a probe reveals otherwise, this docstring
    will be updated.
    """
    if open_default is None or open_default is collapsed_section.RANDOMIZE:
        is_open = bool(rng.random() < 0.5)
    else:
        is_open = bool(open_default)
    open_attr = " open" if is_open else ""
    return (
        f'<details{open_attr}>\n'
        f'  <summary>{esc(title)}</summary>\n'
        f'  {content}\n'
        f'</details>'
    )


# Sentinel exposed as an attribute so callers can write
# `collapsed_section(rng, ..., open_default=collapsed_section.RANDOMIZE)`
# without importing a separate symbol.
collapsed_section.RANDOMIZE = _RANDOMIZE_OPEN  # type: ignore[attr-defined]


def random_pad(rng: random.Random,
                min_px: int = 40,
                max_px: int = 400) -> str:
    """Return a `style="margin-top:Npx"` snippet with N drawn from
    `[min_px, max_px]`. Use to vary vertical position of a critical
    button or section so the agent can't depend on a stable y.

    Yes, raw inline style — iOS Safari treats it the same as a CSS
    rule, no AX implications.
    """
    px = rng.randint(min_px, max_px)
    return f'style="margin-top:{px}px"'


def shuffled_fields(rng: random.Random,
                     fields: Iterable[FormField]) -> str:
    """Render form fields in a random order. Each field's label
    travels with it (paired via `<label for=>`), so the agent must
    match by label text rather than by position."""
    flist = list(fields)
    rng.shuffle(flist)
    return "\n".join(f.render() for f in flist)


# ─────────────────────────── page skeletons ───────────────────────────


def page_skeleton(*, title: str, body: str,
                   description: str = "",
                   font_size_px: Optional[int] = None) -> str:
    """Wrap `body` in a minimal HTML5 page with the right scaffolding:
    DOCTYPE, charset, meta-description for AX context, and a top-level
    `<main>` landmark. Returns the full document string.

    No external CSS / no JS — keeps AX behavior fully predictable.

    `font_size_px` (Step 5b, 2026-06-07): when set, emits a single
    `<style>` block fixing the computed font-size of every form
    input / select / textarea / button. iOS Safari auto-zooms an
    `<input>` on focus when its computed font-size is **strictly less
    than 16 px** (stable Apple behavior since iOS 5, documented across
    CSS-Tricks / Rick Strahl / Telerik). RSVP-form generators
    randomize this per seed so a single corpus run probes both
    conditions naturally; the chosen value (and a derived
    `form_triggers_auto_zoom = font_size_px < 16` flag) is logged on
    the task params so a future result-aggregation pass can stratify
    pass-rate by zoom condition. Default None means no override →
    page falls back to the browser's default (~13-14 px), which on
    iOS DOES trigger zoom.
    """
    meta_desc = (
        f'<meta name="description" content="{esc(description)}">\n'
        if description else ""
    )
    style_block = ""
    if font_size_px is not None:
        style_block = (
            f"  <style>input,select,textarea,button"
            f"{{font-size:{int(font_size_px)}px}}</style>\n"
        )
    return (
        f"<!DOCTYPE html>\n"
        f"<html lang=\"en\">\n"
        f"<head>\n"
        f"  <meta charset=\"utf-8\">\n"
        f"  <meta name=\"viewport\" "
        f"content=\"width=device-width, initial-scale=1\">\n"
        f"  {meta_desc}"
        f"{style_block}"
        f"  <title>{esc(title)}</title>\n"
        f"</head>\n"
        f"<body>\n"
        f"  <main aria-label=\"{esc(title)}\">\n"
        f"{body}\n"
        f"  </main>\n"
        f"</body>\n"
        f"</html>\n"
    )


_ALIGN_TO_JUSTIFY = {
    "left":   "flex-start",
    "center": "center",
    "right":  "flex-end",
}


def submit_form(*, action: str, fields_html: str,
                 submit_label: str = "Submit",
                 form_label: Optional[str] = None,
                 paired_decoy_label: Optional[str] = None,
                 paired_decoy_first: bool = False,
                 paired_decoy_action: str = DECOY_PATH,
                 inline_decoy_labels: Optional[List[str]] = None,
                 inline_decoy_order: Optional[List[int]] = None,
                 align: str = "left") -> str:
    """Wrap arbitrary field HTML in a `<form method=POST>` with a
    submit button. ALWAYS POST — never GET (the existing visited /
    submissions infra treats POST as the verification surface).

    Inline validation: any input rendered with `data-required="true"`
    (FormField(required=True) emits this) is checked on submit. If
    one or more are empty, the form is NOT submitted; instead a
    `role="alert"` div appears above the submit button listing the
    missing fields. This gives the agent observable feedback when an
    iOS focus-state quirk or focus-race wipes a field value — without
    that signal, the agent loops forever tapping a button that
    silently does nothing. `novalidate` disables the browser's
    built-in HTML5 validation tooltip (it's not exposed in the iOS
    AX tree, so the agent can't see it).

    Step 5h (2026-06-07) — `paired_decoy_label`: place ONE Cancel-like
    decoy adjacent to Submit, ordered by `paired_decoy_first`.

    Step 5L-A (2026-06-08) — `inline_decoy_labels`: render N additional
    decoy buttons INSIDE the real form using `formaction=<decoy_path>`
    + `formnovalidate`. Combined with the paired-cancel and the real
    Submit, every button lives in one flex-row action block — and the
    `inline_decoy_order` permutation (built per-seed by the caller,
    typically `rng.sample(range(N+1+paired), N+1+paired)`) decides
    where Submit lands among them. Closes the "Submit is always
    button #1" structural shortcut: now Submit can appear at any
    position in the rendered button list.

    Step 5L-B (2026-06-08) — `align`: maps to `justify-content` on the
    flex container so the buttons honor the per-seed alignment. The
    prior implementation defaulted to `flex-start` (left) regardless
    of the wrapping `text-align`, because flex containers ignore
    `text-align` for child positioning.
    """
    aria = (f' aria-label="{esc(form_label)}"'
            if form_label else "")
    # Step 5d (2026-06-07): real Submit button must also satisfy
    # Apple HIG 44 pt minimum so it remains hittable under
    # auto-zoom. See IOS_SIM_QUIRKS §21 + `distractor_buttons`
    # for the empirical motivation.
    button_style = (
        "min-height:44px;min-width:44px;padding:8px 16px")
    submit_btn = (
        f'<button type="submit" style="{button_style}">'
        f'{esc(submit_label)}</button>'
    )

    def _decoy_btn(label: str) -> str:
        return (
            f'<button type="submit" '
            f'formaction="{esc(paired_decoy_action)}" '
            f'formnovalidate '
            f'name="action" '
            f'value="{esc(label.lower().replace(" ", "_"))}" '
            f'style="{button_style}">'
            f'{esc(label)}</button>'
        )

    # Collect every button that will live inside the form, in a stable
    # "canonical" order: paired-decoy (if before-submit), submit,
    # paired-decoy (if after-submit), then inline decoys in label order.
    # The shuffle permutation (`inline_decoy_order`) — if provided —
    # then permutes this canonical list to its final rendered order.
    buttons: List[str] = []
    if paired_decoy_label and paired_decoy_first:
        buttons.append(_decoy_btn(paired_decoy_label))
    buttons.append(submit_btn)
    if paired_decoy_label and not paired_decoy_first:
        buttons.append(_decoy_btn(paired_decoy_label))
    for d in (inline_decoy_labels or []):
        buttons.append(_decoy_btn(d))

    if inline_decoy_order is not None:
        if sorted(inline_decoy_order) != list(range(len(buttons))):
            raise ValueError(
                f"submit_form.inline_decoy_order must be a permutation "
                f"of range({len(buttons)}); got {inline_decoy_order!r}")
        buttons = [buttons[i] for i in inline_decoy_order]

    if len(buttons) == 1:
        # Single-button case: keep the legacy `<p><button></button></p>`
        # wrapper so the inherited text-align continues to govern
        # alignment (no flex container to override it).
        action_row = f'<p>{buttons[0]}</p>'
    else:
        justify = _ALIGN_TO_JUSTIFY.get(align, "flex-start")
        action_row = (
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;'
            f'margin-top:8px;justify-content:{justify}">'
            + "".join(buttons)
            + '</div>'
        )
    return (
        f'<form action="{esc(action)}" method="POST"{aria}>\n'
        f'  {fields_html}\n'
        f'  {action_row}\n'
        f'</form>'
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Shop primitives (Step 5M, 2026-06-08)
#
#  Per `sibb_runs/shopping_mock_site_realism.md` the load-bearing
#  micro-decision for shop pages is the AX shape of a product card —
#  `<article aria-labelledby="t-X"><h2 id="t-X"><a>…</a></h2>…</article>`
#  exposes ONE AX node per card on iOS Safari; a whole-card `<a>`
#  wrapper collapses the same content into one giant concatenated
#  label and hides the secondary actions. We keep `product_card()` as
#  the single primitive every shop template composes over.
# ─────────────────────────────────────────────────────────────────────────────


def star_glyphs(rating: float) -> str:
    """Render a 0-5 rating as a 5-glyph star string `"★★★★☆"`.

    Half-stars round to the nearest half. Used inside an
    `aria-label="<n> stars"` span so iOS VoiceOver gets the numeric
    rating while the visual shows the standard star-row.
    """
    if rating < 0:
        rating = 0.0
    if rating > 5:
        rating = 5.0
    full = int(rating)
    half = 1 if (rating - full) >= 0.5 else 0
    empty = 5 - full - half
    return "★" * full + ("½" if half else "") + "☆" * empty


def product_card(sku, *,
                  rating: Optional[float] = None,
                  review_count: Optional[int] = None,
                  sponsored: bool = False,
                  detail_link: Optional[str] = None,
                  show_description: bool = True) -> str:
    """Render one product card with the AX-correct article + named-link
    shape. The `sku` argument is a duck-typed SKU object that exposes
    `sku_id`, `name`, `brand`, `price_cents`, `short_description` (any
    object with these attributes works — usually a
    `sibb_mock_site_catalog.SKU`).

    Optional fields:
      * `rating` / `review_count` — when both are set, a rating block
        renders with `aria-label="N stars"` so VoiceOver reads the
        numeric value, plus the visual star row.
      * `sponsored=True` — adds a `[Sponsored]` badge before the title.
        Tests whether the agent ignores sponsored decoys in search
        results (DECEPTICON / SusBench-class adversarial signal).
      * `detail_link` — overrides the default `/product/<sku_id>` URL.
      * `show_description` — toggle the short-description paragraph.

    The card is intentionally CSS-poor (zero classes, inline style only
    on the placeholder image div) so the iOS sim's AX tree matches what
    the verifier expects without depending on stylesheet loading order.
    """
    link_target = detail_link or f"/product/{sku.sku_id}"
    title_id = f"t-{sku.sku_id}"
    price = f"${sku.price_cents / 100:.2f}"
    bits: List[str] = []
    bits.append(f'<article aria-labelledby="{esc(title_id)}">')
    # Placeholder "image" — emoji + role=img + aria-label so iOS
    # VoiceOver gets a descriptive label without us shipping real
    # image bytes (see realism report §5 — real Amazon URLs 403 by
    # year 2).
    bits.append(
        f'  <div role="img" aria-label="Photo of {esc(sku.name)}" '
        f'style="width:120px;height:120px;background:#eee;'
        f'display:inline-block;text-align:center;line-height:120px;'
        f'font-size:48px">📦</div>')
    sponsored_badge = ""
    if sponsored:
        sponsored_badge = '<span aria-label="Sponsored">[Sponsored] </span>'
    bits.append(
        f'  <h2 id="{esc(title_id)}">{sponsored_badge}'
        f'<a href="{esc(link_target)}">{esc(sku.name)}</a></h2>')
    rating_html = ""
    if rating is not None and review_count is not None:
        rating_html = (
            f' · <span aria-label="{rating:.1f} stars">'
            f'{star_glyphs(rating)}</span> ({review_count})')
    bits.append(
        f'  <p><strong>{esc(price)}</strong>{rating_html} '
        f'· <span>{esc(sku.brand)}</span></p>')
    if show_description and sku.short_description:
        bits.append(f'  <p>{esc(sku.short_description)}</p>')
    bits.append('</article>')
    return "\n".join(bits)


def synth_rating(rng: random.Random) -> Tuple[float, int]:
    """Generate a plausible (rating, review_count) pair. Rating is
    skewed toward 4-5 stars (real e-commerce distribution). Review
    counts follow a long-tail distribution — most products have
    <100 reviews, a few have thousands.
    """
    # Beta(8, 2) shape compressed into [3.0, 5.0] — matches real
    # Amazon distributions where 80% of ratings cluster ≥ 4.
    rating = round(3.0 + rng.betavariate(8, 2) * 2.0, 1)
    # Long tail: 90% of products have 1-150 reviews; 10% have
    # 150-9999. Drawn from log-uniform.
    if rng.random() < 0.9:
        review_count = rng.randint(1, 150)
    else:
        review_count = rng.randint(150, 9999)
    return rating, review_count
