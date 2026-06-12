"""Harness page templates — per-generator HTML factories.

Each template is a function taking a `random.Random` (the per-path RNG
MockSite seeded from `page_seed XOR blake2b(path)`) and returning an
HTML string. Templates are registered with `@register_page("name")`;
generators reference them by NAME in their MockSite spec
(`static_pages={"/event": "rsvp_event"}`) and `SafariHandler` resolves
names → fns at apply time.

Convention: filename `harness_pages.py` (this file) holds the registry
of templates. As more land, split into a package (`harness_pages/`)
with one file per task family.
"""

from __future__ import annotations

import hashlib
import random

from harness_layout import (
    FormField, collapsed_section, distractor_buttons, esc,
    filler_paragraphs, page_skeleton, product_card, random_pad,
    register_page, shuffled_fields, submit_form, synth_rating,
)


# ──────────────────────── rsvp_event (first harness gen) ──────────────


# Page-side label pools. These are DIFFERENT from the prompt-side
# pools (the generator picks one of "Name"/"Full Name"/etc. for the
# instruction; the page picks one of these for the form). The
# semantic mapping ("Name" ↔ "Attendee name") is what the agent must
# infer; matching by exact substring should NOT work.
_FORM_NAME_LABELS = [
    "Attendee name",
    "Your full name",
    "Guest name",
    "Registrant name",
    "Name on badge",
]
_FORM_EMAIL_LABELS = [
    "Reply-to email",
    "Contact email address",
    "Email for confirmation",
    "Your email",
    "Where to send the confirmation",
]
_FORM_PHONE_LABELS = [
    "Day-of-event mobile",
    "Contact phone number",
    "Mobile (for day-of updates)",
    "Phone we can reach you at",
    "SMS contact",
]
_FORM_ATTENDING_LABELS = [
    "Will you attend? (yes/no)",
    "RSVP response (yes or no)",
    "Confirming attendance (yes/no)",
    "Going? (yes/no)",
    "Attending — yes or no",
]

_SUBMIT_BUTTON_LABELS = [
    "Send RSVP",
    "Submit RSVP",
    "Send response",
    "Confirm RSVP",
    "Submit response",
]

# Event-page chrome (just decoration — the verifier doesn't read it).
_EVENT_DECORATIONS = [
    ("Aurora Conference 2026",
     "Saturday, September 5th, 2026",
     "Pier 27, San Francisco"),
    ("Helix Symposium 2026",
     "Tuesday, October 14th, 2026",
     "The Reef Pavilion, Oakland"),
    ("Lumen Festival",
     "Friday, July 11th, 2026",
     "Greenwood Park, Berkeley"),
    ("Tessera Summit",
     "Wednesday, November 19th, 2026",
     "Bayfront Hall, Sausalito"),
]


def rsvp_event_choices(rng: random.Random) -> dict:
    """Derive every random choice that affects BOTH the form and the
    instruction. Generator calls this with the SAME RNG state the
    template will (both seeded via MockSite's per-path seed) so the
    two sides agree on `contact_type` etc. without a side channel.

    Contract: every call inside this function consumes RNG state in
    a FIXED ORDER. Adding new choices must APPEND to the sequence
    (never insert) so existing replays keep their seed semantics.
    """
    title, date, venue = rng.choice(_EVENT_DECORATIONS)
    contact_type = rng.choice(["email", "phone"])
    if contact_type == "email":
        contact_label = rng.choice(_FORM_EMAIL_LABELS)
        contact_input_type = "email"
    else:
        contact_label = rng.choice(_FORM_PHONE_LABELS)
        contact_input_type = "tel"
    name_label = rng.choice(_FORM_NAME_LABELS)
    attending_label = rng.choice(_FORM_ATTENDING_LABELS)
    submit_label = rng.choice(_SUBMIT_BUTTON_LABELS)
    # Step 5b (2026-06-07) — APPEND ONLY at the end of the RNG sequence
    # to preserve seed semantics of every earlier choice. Range
    # straddles iOS Safari's auto-zoom threshold (zoom triggers when
    # computed font-size < 16 px): values 13/14/15 → zoom; 16/17/18
    # → no zoom. ~50/50 split per seed.
    font_size_px = rng.choice([13, 14, 15, 16, 17, 18])
    # Step 5g (2026-06-07) — APPEND ONLY (see contract above).
    # Horizontal alignment of form + distractor blocks. Default left
    # in real pages, but center/right is common (modal dialogs,
    # right-aligned action bars). Forces the agent to find buttons
    # by label rather than by an assumed left-edge X coordinate.
    align = rng.choice(["left", "center", "right"])
    # Step 5h (2026-06-07) — APPEND ONLY (see contract above).
    # With ~50% probability, place one Cancel-like decoy ADJACENT to
    # the real Submit in a flex row (real-world UX pattern: paired
    # primary/secondary action buttons). The paired decoy uses
    # `formaction=/__sibb_decoy__` + `formnovalidate` so a click is
    # tagged as a decoy submission (verifier still distinguishes the
    # two even when their AX frames are adjacent). `paired_first`
    # randomizes whether the decoy sits left- or right-of Submit.
    # `rsvp_event_clipped` IGNORES these fields (its submit is built
    # manually in a far-offset position; pairing would dilute the
    # scroll-and-find test).
    if rng.random() < 0.5:
        paired_cancel = rng.choice(
            ["Cancel", "Reset", "Discard Changes"])
        paired_first = rng.choice([True, False])
    else:
        paired_cancel = None
        paired_first = False
    # Step 5L-A (2026-06-08) — APPEND ONLY (see contract above). Move
    # the bottom-stack distractor buttons INTO the real form using
    # `formaction=/__sibb_decoy__` + `formnovalidate`. Combined with the
    # paired-cancel and the real Submit, all buttons live in one flex
    # action-row; `inline_decoy_order` shuffles their final rendered
    # order so the real Submit can appear at any position. Closes the
    # "Submit is always button #1" structural shortcut.
    from harness_layout import _DISTRACTOR_BUTTON_LABELS
    # Exclude the paired Cancel-like label (if any) from the inline-
    # decoy sample so it doesn't appear twice in the same form.
    excluded = {paired_cancel} if paired_cancel else set()
    pool = [l for l in _DISTRACTOR_BUTTON_LABELS if l not in excluded]
    inline_decoy_labels = rng.sample(pool, k=rng.randint(2, 3))
    # Compute the permutation now (the template needs it) — total
    # buttons = paired (0 or 1) + 1 submit + len(inline_decoy_labels).
    n_buttons = (1 if paired_cancel else 0) + 1 + len(inline_decoy_labels)
    inline_decoy_order = rng.sample(range(n_buttons), n_buttons)
    return {
        "title": title, "date": date, "venue": venue,
        "contact_type": contact_type,
        "contact_label": contact_label,
        "contact_input_type": contact_input_type,
        "name_label": name_label,
        "attending_label": attending_label,
        "submit_label": submit_label,
        "font_size_px": font_size_px,
        "align": align,
        "paired_cancel": paired_cancel,
        "paired_first": paired_first,
        "inline_decoy_labels": inline_decoy_labels,
        "inline_decoy_order": inline_decoy_order,
    }


@register_page("rsvp_event")
def rsvp_event(rng: random.Random) -> str:
    """RSVP-form page for `gen_safari_rsvp_form`.

    Randomized per seed:
      * event title / date / venue (decoration only)
      * which contact-type field is present (email OR phone — the
        generator picks the same one because it consumes
        `rsvp_event_choices` over an RNG with the SAME per-path seed)
      * every form-field label (from pools of semantically-equivalent
        but textually-distinct labels — agent must match by meaning)
      * submit-button label
      * field order (`shuffled_fields`), distractor buttons, filler
        paragraphs, collapsed venue notes

    Stable across runs:
      * HTML5 `name=` attributes are FIXED (`name="name"`,
        `name="contact"`, `name="attending"`) so the verifier can
        select by `fields.name` etc. regardless of label.
      * Form `action="/rsvp"` and `method="POST"`.
    """
    cfg = rsvp_event_choices(rng)
    fields = [
        FormField(
            name="name", label=cfg["name_label"],
            input_type="text", required=True),
        FormField(
            name="contact", label=cfg["contact_label"],
            input_type=cfg["contact_input_type"], required=True),
        FormField(
            name="attending", label=cfg["attending_label"],
            input_type="text", required=True),
    ]

    # Step 5g (2026-06-07) — wrap form + distractor stack in a
    # text-align'd div. `text-align` inherits into descendant block
    # containers (the inner <form><p>...</p></form>), and the leaf
    # <button> elements are inline-block, so the cascade aligns the
    # rendered button independent of the form's block width.
    align_open = f'<div style="text-align:{cfg["align"]}">'
    align_close = "</div>"
    body = (
        f"<h1>{esc(cfg['title'])}</h1>\n"
        f"<p><strong>{esc(cfg['date'])}</strong> "
        f"at <em>{esc(cfg['venue'])}</em></p>\n"
        + filler_paragraphs(rng, n=rng.randint(1, 2))
        + collapsed_section(
            rng, "Venue & travel notes",
            filler_paragraphs(rng, n=1,
                               min_sentences=2, max_sentences=4))
        + align_open
        + f'<div {random_pad(rng, min_px=20, max_px=180)}>'
        + submit_form(
            action="/rsvp",
            fields_html=shuffled_fields(rng, fields),
            submit_label=cfg["submit_label"],
            form_label="RSVP form",
            paired_decoy_label=cfg["paired_cancel"],
            paired_decoy_first=cfg["paired_first"],
            inline_decoy_labels=cfg["inline_decoy_labels"],
            inline_decoy_order=cfg["inline_decoy_order"],
            align=cfg["align"])
        + "</div>"
        # Step 5L-A (2026-06-08) — bottom-stack `distractor_buttons`
        # call removed; the decoys are now inline inside the real form
        # (shuffled with Submit). This closes the "Submit is always
        # before the distractor stack" structural shortcut. The
        # `align_close` still wraps the same region so `text-align`
        # cascades into the inline action row's wrapper consistently.
        + align_close
        + filler_paragraphs(rng, n=rng.randint(1, 2))
    )
    return page_skeleton(
        title=cfg["title"],
        description=f"RSVP for {cfg['title']}",
        body=body,
        font_size_px=cfg["font_size_px"])


@register_page("rsvp_event_clipped")
def rsvp_event_clipped(rng: random.Random) -> str:
    """Adversarial variant of `rsvp_event` — the FORM INPUTS render at
    a normal-looking position, but the SUBMIT BUTTON is moved far
    away from them: ~800 px below and ~500 px to the right via
    inline absolute-style offsets. The agent sees the form inputs
    fine, fills them, and then has to scroll BOTH down and right (or
    pinch out / swipe) to discover the submit button.

    Used to test:
      * Does the agent realize the submit button isn't in view?
      * Can it scroll both vertically and horizontally?
      * Does it handle a "submit far from inputs" layout?
    """
    import random as _rand
    cfg = rsvp_event_choices(rng)
    fields = [
        FormField(name="name", label=cfg["name_label"],
                   input_type="text", required=True),
        FormField(name="contact", label=cfg["contact_label"],
                   input_type=cfg["contact_input_type"], required=True),
        FormField(name="attending", label=cfg["attending_label"],
                   input_type="text", required=True),
    ]
    # Build the form manually so we can position the submit button
    # FAR from the inputs (rather than the submit_form helper which
    # places them adjacent).
    form_label = "RSVP form"
    submit_label = cfg["submit_label"]
    # Moderate offsets — push the button a few hundred px below and a
    # bit to the right of the inputs. Extreme offsets (margin-left >
    # 380, margin-top > 640) empirically destabilize the iOS Safari
    # tap pipeline and crash the XCUITest runner on the first input
    # tap, so keep these conservative.
    submit_offset_left = rng.randint(60, 160)
    submit_offset_top = rng.randint(320, 480)
    form_html = (
        f'<form action="/rsvp" method="POST" '
        f'aria-label="{esc(form_label)}">\n'
        f'  {shuffled_fields(rng, fields)}\n'
        # Wrap the button in a div that pushes it far right + down.
        # The wrapping div has explicit width to keep it from being
        # truncated by the form's normal content-box width.
        f'  <div style="margin-left:{submit_offset_left}px;'
        f'margin-top:{submit_offset_top}px;width:300px;">\n'
        # Step 5d (2026-06-07) — keep parity with submit_form's
        # button style so the clipped variant's Submit also satisfies
        # Apple HIG 44 pt minimum and stays hittable under
        # auto-zoom. See IOS_SIM_QUIRKS §21.
        f'    <p><button type="submit" '
        f'style="min-height:44px;min-width:44px;padding:8px 16px">'
        f'{esc(submit_label)}</button></p>\n'
        f'  </div>\n'
        f'</form>'
    )
    # Step 5g (2026-06-07) — alignment cascade for clipped variant.
    # The clipped form's submit lives inside an absolute-offset div
    # whose `margin-left` is independent of text-align, so the clipped
    # submit's X position is governed by `submit_offset_left` not by
    # `align`. But the distractor stack and the form inputs still
    # honor `text-align` via the inheritance path.
    align_open = f'<div style="text-align:{cfg["align"]}">'
    align_close = "</div>"
    body = (
        f"<h1>{esc(cfg['title'])}</h1>\n"
        f"<p><strong>{esc(cfg['date'])}</strong> "
        f"at <em>{esc(cfg['venue'])}</em></p>\n"
        + filler_paragraphs(rng, n=rng.randint(1, 2))
        + collapsed_section(
            rng, "Venue & travel notes",
            filler_paragraphs(rng, n=1, min_sentences=2,
                               max_sentences=4))
        + align_open
        + f'<div {random_pad(rng, min_px=20, max_px=180)}>'
        + form_html
        + "</div>"
        + distractor_buttons(rng, n=rng.randint(2, 3))
        + align_close
        + filler_paragraphs(rng, n=rng.randint(1, 2))
    )
    return page_skeleton(
        title=cfg["title"],
        description=f"RSVP for {cfg['title']}",
        body=body,
        font_size_px=cfg["font_size_px"])


# ─────────────────────────────────────────────────────────────────────────────
#  Shop pages — Step 5M (2026-06-08) — V0 `gen_safari_shop_pick_by_attrs`
#
#  V0 keeps the page set minimal: a search-results grid, per-SKU PDPs
#  via the `/product/` prefix route, and a checkout form. No cart step;
#  the PDP's Buy Now link goes directly to `/checkout?sku=<id>`. Cross-
#  app and dark-pattern sub-variants layer on top (V1-V4 in the plan).
# ─────────────────────────────────────────────────────────────────────────────

import urllib.parse as _urlparse

# Persona pool for shipping form fills. Each entry: (first, last,
# street, city, state, zip). Picked deterministically per episode.
_SHOP_PERSONAS = [
    ("Sam",     "Rivera",   "1100 W Sunset Blvd", "Los Angeles",   "CA", "90026"),
    ("Jordan",  "Kim",      "245 Mission St",     "San Francisco", "CA", "94105"),
    ("Avery",   "Patel",    "501 Brannan St",     "San Francisco", "CA", "94107"),
    ("Casey",   "Lopez",    "210 W 14th St",      "Austin",        "TX", "78701"),
    ("Morgan",  "Chen",     "75 Albany St",       "Cambridge",     "MA", "02139"),
    ("Reese",   "Carter",   "330 Roebling Way",   "Brooklyn",      "NY", "11211"),
]


_SHOP_PRICE_CAPS_CENTS = (2500, 5000, 7500, 10000, 15000)

# Probability that a seed's episode adds the V4 "saved cards" axis
# on top of its Q1/Q2 archetype. Orthogonal — every seed independently
# rolls archetype AND V4. Set at 40% so V4 episodes are common enough
# to debug without dominating the corpus.
_SHOP_V4_PROBABILITY = 0.4

# Card-brand pool for V4 saved-cards page. Distinct brands so the
# `personal` and `work` cards always look different in the AX tree.
_SHOP_CARD_BRANDS = ("VISA", "Mastercard", "Amex", "Discover")


def _shop_make_card(rng: random.Random, label: str, brand: str) -> dict:
    """Generate one fake saved-card record. Last4 = 4 random digits
    (NOT a valid Luhn). Expiry = a future MM/YY within the next 3
    years. The MockSite never validates these; verification is just
    string equality."""
    last4 = "".join(str(rng.randint(0, 9)) for _ in range(4))
    exp_mm = f"{rng.randint(1, 12):02d}"
    # Years 26 / 27 / 28 — comfortably "future" past the 2026 test date.
    exp_yy = str(rng.choice([26, 27, 28]))
    return {
        "label": label,
        "brand": brand,
        "last4": last4,
        "exp_mm": exp_mm,
        "exp_yy": exp_yy,
    }


def _v4_card_axis(page_seed: int) -> dict:
    """Page-seed-only V4 saved-cards axis (Step 5P, 2026-06-09).

    Used by BOTH `shop_pick_by_attrs_choices` (Q1/Q2) and
    `shop_filter_sort_choices` (Q4) so the V4 state for a given
    page_seed is identical regardless of which archetype-helper
    produced it. This lets shop_pdp + shop_account_cards +
    shop_checkout be reused across archetypes — they only need
    page_seed-derived V4 fields, never the winner/archetype/persona.

    The axis-rng is seeded from `page_seed` mixed with a fixed
    namespace tag, NOT from a path-RNG, so the V4 axis is fully
    decoupled from the archetype helpers' rng-consumption order.

    Returns: {use_saved_cards: bool, personal_card: dict|None,
              work_card: dict|None}
    """
    v4_seed = (page_seed ^ 0x5A7E_DCA4) & 0xFFFF_FFFF
    v4_rng = random.Random(v4_seed)
    use_saved_cards = v4_rng.random() < _SHOP_V4_PROBABILITY
    if use_saved_cards:
        brand_pair = v4_rng.sample(_SHOP_CARD_BRANDS, 2)
        personal_card = _shop_make_card(v4_rng, "Personal", brand_pair[0])
        work_card = _shop_make_card(v4_rng, "Work", brand_pair[1])
    else:
        personal_card = None
        work_card = None
    return {
        "use_saved_cards": use_saved_cards,
        "personal_card": personal_card,
        "work_card": work_card,
    }


def _sku_stable_rating(sku):
    """Return (rating, n_reviews) for a SKU, derived from a per-SKU
    seeded rng so the same SKU has identical ratings on /search,
    /product/<id>, and any future cart/checkout summary. Closes a
    consistency bug (Reviewer D #3) where /search and /product
    produced different ratings for the same item."""
    seed = int.from_bytes(
        hashlib.blake2b(sku.sku_id.encode(),
                         digest_size=8).digest(),
        "big", signed=False)
    return synth_rating(random.Random(seed))


def shop_pick_by_attrs_choices(rng: random.Random,
                                 page_seed: int = 0):
    """Deterministic targeting for `gen_safari_shop_pick_by_attrs`.

    The generator and the `/landing` + `/search` templates ALL call
    this with the same per-/path rng so they agree on the same
    winner + archetype + persona without any side channel.

    Step 5N (2026-06-08) — V0.5 rewrite:
      * Drop sponsored decoy slot (no more `[Sponsored]` badges).
      * Drop pre-sampled distractor list. Distractors emerge from the
        BM25 search instead (`Catalog.bm25_search(query, n=8)`),
        which is what real agents see.
      * Add winner-UNIQUENESS invariant: pick winner s.t. exactly
        ONE SKU in the catalog satisfies (brand, global_category,
        max_price). Closes Reviewer E's F3 (multiple-matches verifier
        ambiguity) and A's F1 (brand-substring cheat).
      * Add archetype split (Q1/Q2 50/50). Q1 = attribute-prose; Q2
        = literal search hint baked into the instruction.

    Step 5P (2026-06-09) — V4 axis decoupled from rng. V4 fields
    (`use_saved_cards`, `personal_card`, `work_card`) are now
    derived from `_v4_card_axis(page_seed)`, NOT from rng-state
    consumption order. This lets Q4 (`shop_filter_sort_choices`)
    share the page_seed-only V4 axis so shop_pdp + shop_account_cards
    + shop_checkout render identically across Q1/Q2 and Q4 episodes
    that share a page_seed.

    APPEND-ONLY contract (same as `rsvp_event_choices`): every call
    to `rng.choice` / `rng.random` etc. inside this helper consumes
    state in a FIXED ORDER. Adding a new field MUST be appended at
    the end of the consumption sequence so existing seeds keep their
    semantics.
    """
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    persona = rng.choice(_SHOP_PERSONAS)
    # Pick from the precomputed eligibility list: ~60 SKUs that are
    # the UNIQUE cheapest in their (brand, category) family under some
    # cap from `_SHOP_PRICE_CAPS_CENTS`. The instruction adds a
    # "cheapest" superlative so the verifier's
    # `attribute_eq fields.sku == winner` is always defensible.
    # Empirically (WebMall webmall_1.csv): 61 eligibles. Cache lives on
    # the Catalog instance.
    eligibles = cat.eligible_cheapest_winners(_SHOP_PRICE_CAPS_CENTS)
    if not eligibles:
        raise RuntimeError(
            "shop_pick_by_attrs_choices: catalog has no eligible "
            "cheapest-winner candidates; the bundled CSV may have "
            "drifted from expected diversity")
    winner, max_price_cents = rng.choice(eligibles)
    # Archetype: Q1 = attribute-prose; Q2 = literal search hint from
    # the winner's title. 50/50 split per seed.
    archetype = rng.choice(["Q1", "Q2"])
    # For Q2, derive a 2-4 token hint from the winner's name. Tokens
    # are the first whitespace-delimited words of the title that
    # aren't pure punctuation. Cap at 4 words so the hint stays a
    # plausible search query rather than a verbose product spec.
    title_tokens = winner.name.split()
    # Drop trailing-comma artifacts from CSV titles.
    cleaned = [t.strip(",.") for t in title_tokens]
    cleaned = [t for t in cleaned if t]
    n_hint_tokens = min(4, max(2, len(cleaned)))
    search_hint = " ".join(cleaned[:n_hint_tokens])
    # Step 5O (2026-06-09) — V4 saved-cards axis is orthogonal to
    # Q1/Q2 archetype. As of Step 5P, V4 is page_seed-only (not rng-
    # consumed) so Q4 can share the same V4 cards for the same
    # page_seed.
    v4 = _v4_card_axis(page_seed)
    return {
        "category": winner.global_category,
        "max_price_cents": max_price_cents,
        "winner": winner,
        "persona": persona,
        "archetype": archetype,
        "search_hint": search_hint,
        "use_saved_cards": v4["use_saved_cards"],
        "personal_card": v4["personal_card"],
        "work_card": v4["work_card"],
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Q4 — gen_safari_shop_filter_and_sort: filter + sort cascade
# ─────────────────────────────────────────────────────────────────────────────

# Sort options exposed by the Q4 /browse page. Order matters for UI
# rendering; the FIRST entry is the canonical "cheapest-first" sort the
# instruction tells the agent to apply.
_SHOP_Q4_SORTS = (
    ("price_asc",  "Price (low to high)"),
    ("price_desc", "Price (high to low)"),
    ("rating",     "Rating (high to low)"),
)


def shop_filter_sort_choices(rng: random.Random,
                              page_seed: int = 0):
    """Deterministic targeting for `gen_safari_shop_filter_and_sort`
    (Q4 archetype — agent reaches winner via filter+sort cascade
    instead of typed search).

    Reuses the V0.5 cheapest-winner eligibility (same uniqueness
    invariant: winner is THE unique cheapest in its (brand, category)
    family under the cap), so sort=price_asc + filter brand=X +
    filter category=Y always surfaces the winner FIRST.

    V4 axis is page_seed-only via `_v4_card_axis`, identical to Q1/Q2,
    so shop_pdp/shop_checkout/shop_account_cards work for Q4
    unchanged.

    APPEND-ONLY contract — append new fields after V4, never reorder.
    """
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    persona = rng.choice(_SHOP_PERSONAS)
    eligibles = cat.eligible_cheapest_winners(_SHOP_PRICE_CAPS_CENTS)
    if not eligibles:
        raise RuntimeError(
            "shop_filter_sort_choices: catalog has no eligible "
            "cheapest-winner candidates")
    winner, max_price_cents = rng.choice(eligibles)
    v4 = _v4_card_axis(page_seed)
    return {
        "category":        winner.global_category,
        "brand":           winner.brand,
        "max_price_cents": max_price_cents,
        "winner":          winner,
        "persona":         persona,
        "canonical_sort":  _SHOP_Q4_SORTS[0][0],  # "price_asc"
        "use_saved_cards": v4["use_saved_cards"],
        "personal_card":   v4["personal_card"],
        "work_card":       v4["work_card"],
    }


def _shop_q4_task_cfg(page_seed: int):
    """Re-derive Q4 cfg from page_seed. Mirror of `_shop_task_cfg`
    for Q4 templates (`shop_q4_landing`, `shop_q4_browse`)."""
    from harness_layout import compute_path_seed
    landing_rng = random.Random(compute_path_seed(page_seed, "/"))
    return shop_filter_sort_choices(landing_rng, page_seed=page_seed)


def _shop_task_cfg(page_seed: int):
    """Re-derive the same task-level config that the generator used.

    Both /shop_landing path-rng AND a fresh `compute_path_seed(page_seed,
    "/")` rng produce identical `shop_pick_by_attrs_choices(...)` output
    — so /product/, /checkout, /account/cards (which each have their
    own per-path rngs) can call this helper to get the SAME `winner`,
    `archetype`, `use_saved_cards`, `personal_card`, `work_card` as the
    landing page. Used by V4 templates that need cross-path state.

    Step 5P (2026-06-09) — V4 fields in the returned cfg are now
    page_seed-only via `_v4_card_axis(page_seed)`, so the same cards
    are returned whether the active episode is Q1/Q2 (this helper) or
    Q4 (which has its own task-cfg helper but agrees on V4 fields).
    """
    from harness_layout import compute_path_seed
    landing_rng = random.Random(compute_path_seed(page_seed, "/"))
    return shop_pick_by_attrs_choices(landing_rng, page_seed=page_seed)


@register_page("shop_landing")
def shop_landing(rng: random.Random):
    """V0.5 minimal storefront landing — modeled on WebShop's
    `search_page.html` (ONE input + button + filler). No nav, no
    category dropdown, no featured carousel: every homepage element
    added is grading noise for an attribute-pick probe per the
    `sibb_runs/shopping_landing_and_search.md` survey.
    """
    body = (
        f'<h1>SIBB-Mart</h1>\n'
        f'<p>Find what you need from our catalog of computers, '
        f'peripherals, and electronics.</p>\n'
        f'<form action="/search" method="GET" aria-label="Product search">\n'
        f'  <p><label for="search-input">Search</label>\n'
        f'  <input id="search-input" name="q" type="search" '
        f'placeholder="Search products" '
        f'style="min-height:44px;min-width:200px;padding:8px;font-size:16px">\n'
        f'  <button type="submit" '
        f'style="min-height:44px;min-width:80px;padding:8px 16px;'
        f'font-size:16px">Search</button></p>\n'
        f'</form>\n'
        + filler_paragraphs(rng, n=rng.randint(1, 2))
    )
    return page_skeleton(
        title="SIBB-Mart",
        description="SIBB-Mart storefront",
        body=body,
        font_size_px=16)


@register_page("shop_search_results")
def shop_search_results(rng: random.Random, *, query: str = ""):
    """V0.5 BM25-backed search results. The agent's `?q=` query is
    tokenized + scored against the catalog's title+brand+description
    corpus (`Catalog.bm25_search`). Top 8 results render as
    AX-friendly product cards.

    Empty query → friendly empty-state with the search bar repeated.
    Non-empty query with zero matches → "No results for X" + retry
    hint. The agent is expected to reformulate; we don't pre-bias
    the result set.
    """
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    q = _urlparse.parse_qs(query).get("q", [""])[0].strip()
    # Tiny header search bar so the agent can re-search from this
    # page without going back to /.
    search_bar = (
        f'<form action="/search" method="GET" aria-label="Refine search">\n'
        f'  <p><label for="search-input">Search</label>\n'
        f'  <input id="search-input" name="q" type="search" '
        f'value="{esc(q)}" '
        f'style="min-height:44px;min-width:200px;padding:8px;font-size:16px">\n'
        f'  <button type="submit" '
        f'style="min-height:44px;min-width:80px;padding:8px 16px;'
        f'font-size:16px">Search</button></p>\n'
        f'</form>')
    if not q:
        body = (
            f'<h1>SIBB-Mart Search</h1>\n'
            f'<p>Enter a search term above to browse products.</p>\n'
            f'{search_bar}\n'
            + filler_paragraphs(rng, n=1)
        )
        return page_skeleton(
            title="Search",
            description="SIBB-Mart product search",
            body=body,
            font_size_px=16)
    results = cat.bm25_search(q, n=8)
    cards_html = []
    for sku in results:
        rating, n_reviews = _sku_stable_rating(sku)
        cards_html.append(product_card(
            sku, rating=rating, review_count=n_reviews))
    if results:
        header = (
            f'<h1>Search results</h1>\n'
            f'<p>Showing {len(results)} results for '
            f'<em>{esc(q)}</em>.</p>')
        body_mid = "\n".join(cards_html)
    else:
        header = (
            f'<h1>No results</h1>\n'
            f'<p>No products matched <em>{esc(q)}</em>. '
            f'Try a different keyword or brand name.</p>')
        body_mid = ""
    body = (
        f"{header}\n{search_bar}\n"
        + filler_paragraphs(rng, n=1)
        + body_mid
        + filler_paragraphs(rng, n=1)
    )
    return page_skeleton(
        title=f"Search: {q}",
        description="SIBB-Mart product search",
        body=body,
        font_size_px=16)


@register_page("shop_q4_landing")
def shop_q4_landing(rng: random.Random, *, page_seed: int = 0):
    """Q4 landing page — minimal storefront with NO search bar.
    Distinguishes from V0.5's `shop_landing` which DOES have a
    search bar. Q4 agent must navigate to /browse and apply
    filter+sort, never type a query.

    Single link to /browse + filler text. No category-grid teasers
    on landing (those would mislead the agent into thinking they can
    pick a winner directly from the homepage)."""
    body = (
        f'<h1>SIBB-Mart</h1>\n'
        f'<p>Browse our full catalog of computers, peripherals, '
        f'and electronics. Filter by category and brand, sort by '
        f'price or rating, and find what you need.</p>\n'
        f'<p><a href="/browse" '
        f'style="display:inline-block;padding:12px 24px;'
        f'background:#0066cc;color:white;text-decoration:none;'
        f'min-height:44px;min-width:120px">Browse all products</a></p>\n'
        + filler_paragraphs(rng, n=rng.randint(1, 2))
    )
    return page_skeleton(
        title="SIBB-Mart",
        description="SIBB-Mart catalog browse",
        body=body,
        font_size_px=16)


def _q4_sort_skus(skus, sort_key: str):
    """Apply Q4 sort to a list of SKUs. Stable sort so ties keep
    catalog order."""
    if sort_key == "price_asc":
        return sorted(skus, key=lambda s: s.price_cents)
    if sort_key == "price_desc":
        return sorted(skus, key=lambda s: -s.price_cents)
    if sort_key == "rating":
        # Rating-desc — uses stable per-SKU rating so search/PDP
        # rendering matches the sort.
        return sorted(skus,
                       key=lambda s: -_sku_stable_rating(s)[0])
    # Fallback — catalog order.
    return list(skus)


@register_page("shop_q4_browse")
def shop_q4_browse(rng: random.Random, *, query: str = "",
                    page_seed: int = 0):
    """Q4 /browse page — filter + sort cascade UI.

    Query params:
      cat=<global_category>  — narrow to one category
      brand=<brand>          — narrow to one brand
      sort=<key>             — price_asc / price_desc / rating
      max_price=<cents>      — cap the result set; optional, defaults
                                to the task's `max_price_cents` so the
                                expected-results set matches the
                                instruction's "under $X" framing

    Each filter row exposes link options (one per choice). Clicking a
    link replaces ONLY that param in the URL — preserves the rest.
    This is the multi-step "filter cascade" Q4 exercises: agent must
    apply 2-3 filters + a sort before the winner is the first
    result.

    The winner is guaranteed to be UNIQUELY first when
    (cat=winner.category, brand=winner.brand, sort=price_asc,
    max_price=task.cap) — same uniqueness invariant as V0.5.
    """
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    cfg = _shop_q4_task_cfg(page_seed)
    qs = _urlparse.parse_qs(query)
    cur_cat   = qs.get("cat",   [""])[0].strip()
    cur_brand = qs.get("brand", [""])[0].strip()
    cur_sort  = qs.get("sort",  [""])[0].strip()
    try:
        cur_max_price = int(qs.get(
            "max_price", [str(cfg["max_price_cents"])])[0])
    except (TypeError, ValueError):
        cur_max_price = cfg["max_price_cents"]

    # Build the choice set for each facet from the data that's
    # ACTUALLY reachable given the OTHER filters. Keeps the page
    # short and avoids dead-end facet picks. (Brand list depends on
    # current category; category list depends on current brand.)
    def _url(cat_val: str, brand_val: str, sort_val: str,
             max_price_val: int) -> str:
        parts = []
        if cat_val:    parts.append(f"cat={_urlparse.quote(cat_val)}")
        if brand_val:  parts.append(f"brand={_urlparse.quote(brand_val)}")
        if sort_val:   parts.append(f"sort={_urlparse.quote(sort_val)}")
        parts.append(f"max_price={max_price_val}")
        return "/browse?" + "&".join(parts)

    # Cap the catalog universe by max_price first; everything else
    # narrows from that pool.
    pool_capped = cat.filter(max_price_cents=cur_max_price)
    # Top categories — by count, top 8 most populated.
    cat_counter: Dict[str, int] = {}
    for s in pool_capped:
        cat_counter[s.global_category] = cat_counter.get(
            s.global_category, 0) + 1
    cats_sorted = sorted(cat_counter.items(),
                          key=lambda kv: (-kv[1], kv[0]))
    cat_choices = [k for k, _ in cats_sorted[:8]]
    # Always include the cfg's category so the canonical answer is
    # reachable even when distinct categories pile up at the bottom
    # of the cap.
    if cfg["category"] not in cat_choices:
        cat_choices.append(cfg["category"])

    # Top brands within current category (or globally if no cat).
    brand_pool = (cat.filter(category=cur_cat,
                              max_price_cents=cur_max_price)
                   if cur_cat else pool_capped)
    brand_counter: Dict[str, int] = {}
    for s in brand_pool:
        brand_counter[s.brand] = brand_counter.get(s.brand, 0) + 1
    brands_sorted = sorted(brand_counter.items(),
                            key=lambda kv: (-kv[1], kv[0]))
    brand_choices = [k for k, _ in brands_sorted[:8]]
    # Pin the cfg's brand into the brand row regardless of pool order.
    if cfg["brand"] not in brand_choices:
        brand_choices.append(cfg["brand"])

    # Build filter UI rows.
    def _row(label: str, options: List[Tuple[str, str]],
             current: str, query_key: str) -> str:
        links = []
        for val, display in options:
            is_current = (val == current) or (val == "" and not current)
            if is_current:
                links.append(
                    f'<strong aria-current="true">{esc(display)}</strong>')
            else:
                if query_key == "cat":
                    href = _url(val, cur_brand, cur_sort, cur_max_price)
                elif query_key == "brand":
                    href = _url(cur_cat, val, cur_sort, cur_max_price)
                elif query_key == "sort":
                    href = _url(cur_cat, cur_brand, val, cur_max_price)
                else:
                    href = "/browse"
                links.append(
                    f'<a href="{esc(href)}" '
                    f'style="min-height:44px;display:inline-block;'
                    f'padding:8px 12px;margin:2px">{esc(display)}</a>')
        return (
            f'<p><strong>{esc(label)}:</strong> '
            + " · ".join(links) + "</p>")

    cat_row = _row(
        "Category",
        [("", "All")] + [(c, c) for c in cat_choices],
        cur_cat, "cat")
    brand_row = _row(
        "Brand",
        [("", "All")] + [(b, b) for b in brand_choices],
        cur_brand, "brand")
    sort_row = _row(
        "Sort by",
        [(k, label) for k, label in _SHOP_Q4_SORTS],
        cur_sort, "sort")

    # Build the filtered result set + sort it.
    filtered = cat.filter(
        category=cur_cat or None,
        brand=cur_brand or None,
        max_price_cents=cur_max_price)
    sorted_skus = _q4_sort_skus(
        filtered, cur_sort or "price_asc")
    cards_html = []
    for sku in sorted_skus[:10]:
        rating, n_reviews = _sku_stable_rating(sku)
        cards_html.append(product_card(
            sku, rating=rating, review_count=n_reviews))

    if not sorted_skus:
        result_block = (
            "<p>No products match these filters. Adjust the "
            "category or brand to see more.</p>")
    else:
        result_block = (
            f"<p>Showing {min(len(sorted_skus), 10)} of "
            f"{len(sorted_skus)} matching products.</p>\n"
            + "\n".join(cards_html))

    body = (
        f'<h1>Browse the catalog</h1>\n'
        f'<p>Use the filter and sort options below to narrow your '
        f'search.</p>\n'
        f'{cat_row}\n{brand_row}\n{sort_row}\n'
        + filler_paragraphs(rng, n=1)
        + result_block
    )
    return page_skeleton(
        title="Browse",
        description="SIBB-Mart catalog browse",
        body=body,
        font_size_px=16)


@register_page("shop_account_cards")
def shop_account_cards(rng: random.Random, *, page_seed: int = 0):
    """V4 saved-cards page (Step 5O). Lists the persona's two cards —
    Personal + Work — with brand, last4, expiry. The instruction tells
    a V4 agent to use the PERSONAL card; the verifier later asserts
    `fields.pay_card_last4` matches the personal card.

    Renders even when use_saved_cards is False — non-V4 agents who
    explore /account/cards see an empty-state message rather than a
    404. Keeps the explore-path forgiving and doesn't leak whether V4
    is active for the seed.
    """
    cfg = _shop_task_cfg(page_seed)
    if not cfg["use_saved_cards"]:
        body = (
            f"<h1>Saved cards</h1>\n"
            f"<p>You have no saved cards on this account.</p>\n"
            f"<p>You can complete checkout with a one-time payment "
            f"by entering card details directly on the order page.</p>\n"
        )
        return page_skeleton(
            title="Saved cards",
            description="SIBB-Mart saved payment methods",
            body=body,
            font_size_px=16)
    personal = cfg["personal_card"]
    work = cfg["work_card"]
    # AX-friendly table-ish rendering: each card is its own
    # `<article>` with the four labelled fields. Avoids `<table>`
    # because iOS VoiceOver collapses small tables awkwardly.
    def _card_block(c: dict) -> str:
        return (
            f'<article aria-label="{esc(c["label"])} card">\n'
            f'  <h2>{esc(c["label"])} card</h2>\n'
            f'  <dl>\n'
            f'    <dt>Brand</dt><dd>{esc(c["brand"])}</dd>\n'
            f'    <dt>Last 4 digits</dt><dd>{esc(c["last4"])}</dd>\n'
            f'    <dt>Expires</dt>'
            f'<dd>{esc(c["exp_mm"])}/{esc(c["exp_yy"])}</dd>\n'
            f'  </dl>\n'
            f'</article>'
        )
    body = (
        f"<h1>Saved cards</h1>\n"
        f"<p>These cards are saved on your account. Pick whichever "
        f"one your purchase calls for and type its last 4 digits "
        f"and expiration on the checkout form.</p>\n"
        + _card_block(personal) + "\n"
        + _card_block(work) + "\n"
    )
    return page_skeleton(
        title="Saved cards",
        description="SIBB-Mart saved payment methods",
        body=body,
        font_size_px=16)


@register_page("shop_pdp")
def shop_pdp(rng: random.Random, *, path: str = "", page_seed: int = 0):
    """Product detail page served by the `/product/` prefix route.
    SKU id is the last path segment. We look it up in the catalog
    rather than relying on the search-page rng — the agent can
    navigate to ANY `/product/<sku>` URL and see the right product.

    "Buy Now" goes to `/checkout?sku=<id>` so the checkout template
    knows what's being purchased without any session state.

    V4 (Step 5O, 2026-06-09): when the task seed has
    `use_saved_cards`, the Buy Now link appends `&pay=1` so the
    checkout template knows to render the extra payment fields.
    The agent still completes the same shopping flow — V4 just adds
    a payment-fetch detour through /account/cards.
    """
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    sku_id = path.rsplit("/", 1)[-1] if "/" in path else ""
    sku = next((s for s in cat.skus if s.sku_id == sku_id), None)
    if sku is None:
        body = (
            f"<h1>Product not found</h1>\n"
            f"<p>No product with id <code>{esc(sku_id)}</code>.</p>")
        return page_skeleton(title="Not found", body=body)

    price = f"${sku.price_cents / 100:.2f}"
    # Use the stable per-SKU rating so the PDP shows the same
    # numbers as the search card (Reviewer D #3 — old code re-rolled
    # synth_rating with the per-path rng and gave inconsistent
    # ratings between /search and /product/<id>).
    rating, n_reviews = _sku_stable_rating(sku)
    star_visual = '★' * int(rating) + '☆' * (5 - int(rating))
    cfg = _shop_task_cfg(page_seed)
    if cfg["use_saved_cards"]:
        buy_link = (
            f"/checkout?sku={_urlparse.quote(sku.sku_id)}&pay=1")
    else:
        buy_link = f"/checkout?sku={_urlparse.quote(sku.sku_id)}"
    body = (
        f"<h1>{esc(sku.name)}</h1>\n"
        f"<p><strong>{esc(price)}</strong> · "
        f"<span aria-label=\"{rating:.1f} stars\">{star_visual}</span> "
        f"({n_reviews})</p>\n"
        f"<dl>"
        f"<dt>Brand</dt><dd>{esc(sku.brand)}</dd>"
        f"<dt>Category</dt><dd>{esc(sku.category_path)}</dd>"
        f"</dl>\n"
        f"<p>{esc(sku.short_description)}</p>\n"
        f"<p style=\"margin-top:16px\">"
        f"<a href=\"{esc(buy_link)}\" "
        f"style=\"display:inline-block;padding:12px 24px;"
        f"background:#0066cc;color:white;text-decoration:none;"
        f"min-height:44px;min-width:44px\">Buy Now</a></p>")
    return page_skeleton(
        title=sku.name[:60],
        description=f"PDP for {sku.sku_id}",
        body=body,
        font_size_px=16)


@register_page("shop_checkout")
def shop_checkout(rng: random.Random, *, query: str = ""):
    """Checkout form. SKU comes from `?sku=` (set by the PDP's Buy
    Now link). Agent fills name + street + city + state + zip and
    submits. On POST the MockSite records `path=/checkout`; the
    verifier reads `fields.sku` + `fields.ship_*`.
    """
    from sibb_mock_site_catalog import load_catalog
    cat = load_catalog()
    sku_id = _urlparse.parse_qs(query).get("sku", [""])[0]
    sku = next((s for s in cat.skus if s.sku_id == sku_id), None)
    if sku is None:
        body = (
            f"<h1>Checkout</h1>\n"
            f"<p>No item in your cart. Browse products and pick one "
            f"first.</p>")
        return page_skeleton(title="Checkout", body=body)

    price = f"${sku.price_cents / 100:.2f}"
    fields = [
        FormField(name="ship_name",   label="Full name",
                   input_type="text", required=True),
        FormField(name="ship_street", label="Street address",
                   input_type="text", required=True),
        FormField(name="ship_city",   label="City",
                   input_type="text", required=True),
        FormField(name="ship_state",  label="State (2-letter)",
                   input_type="text", required=True),
        FormField(name="ship_zip",    label="ZIP code",
                   input_type="text", required=True),
    ]
    # V4 (Step 5O, 2026-06-09): when ?pay=1 (set by V4 PDP), append
    # 3 extra fields the agent must fill from /account/cards. We use
    # the PDP-emitted query param — NOT the page_seed cfg — to keep
    # this route self-describing. CVV is intentionally omitted: it's
    # not stored on /account/cards, so requiring it would be a typing
    # hazard with no information value.
    qs = _urlparse.parse_qs(query)
    pay_mode = qs.get("pay", ["0"])[0] == "1"
    if pay_mode:
        fields = fields + [
            FormField(name="pay_card_last4",
                       label="Card last 4 digits",
                       input_type="text", required=True),
            FormField(name="pay_exp_mm",
                       label="Expiration month (MM)",
                       input_type="text", required=True),
            FormField(name="pay_exp_yy",
                       label="Expiration year (YY)",
                       input_type="text", required=True),
        ]
    hidden_sku = (
        f'<input type="hidden" name="sku" '
        f'value="{esc(sku.sku_id)}">')
    summary = (
        f"<section aria-label=\"Order summary\">\n"
        f"<h2>Your order</h2>\n"
        f"<p><strong>{esc(sku.name)}</strong></p>\n"
        f"<p>Total: <strong>{esc(price)}</strong></p>\n"
        f"</section>")
    form_html = submit_form(
        action="/checkout",
        fields_html=hidden_sku + "\n" + shuffled_fields(rng, fields),
        submit_label="Place order",
        form_label="Checkout",
        align="left")
    addr_heading = (
        "<h2>Shipping &amp; payment</h2>" if pay_mode
        else "<h2>Shipping address</h2>")
    body = (
        f"<h1>Checkout</h1>\n"
        f"{summary}\n"
        f"{addr_heading}\n"
        f"{form_html}")
    return page_skeleton(
        title="Checkout",
        description="Complete your order",
        body=body,
        font_size_px=16)
