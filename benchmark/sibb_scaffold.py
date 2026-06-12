"""
SIBB Scaffold — iOS ↔ LLM Bridge
=================================
The layer between the iOS simulator and the LLM that:

1. Reads AX tree from iOS (via idb)
2. Enriches/repairs the tree using VLM when elements are missing labels/traits
3. Normalizes and tokenizes the tree into a compact LLM-friendly representation
4. Programmatically controls AX focus to flush stale trees
5. Accepts LLM actions and translates them to iOS commands — including
   coordinate-based fallback for elements with no original AX identity

Architecture:
  iOSSimulator  ──→  AXReader  ──→  AXEnricher  ──→  AXTokenizer  ──→  LLM
                                                                          │
  iOSSimulator  ←──  ActionExecutor  ←──────────────────────────────────  │

All public classes have async interfaces. Designed to run one instance per
simulator UDID; instantiate N of them for N parallel episodes.
"""

import asyncio
import json
import os
import re
import subprocess
import hashlib
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, List, Dict, Tuple
from enum import Enum


# Labeled-but-noise [other] elements emitted by UIKit. Anchored regex
# avoids matching legitimate labels containing these words as
# substrings (e.g. an app named "Loading dock"). en-US only — sim is
# locale-pinned via `sibb_prewarm.sh`. See IOS_SIM_QUIRKS §20.
_NOISE_OTHER_LABEL_RE = re.compile(
    r"^(?:Vertical|Horizontal)\s+scroll bar(?:,.*)?$"  # UIScrollView indicators
    r"|^Loading…?$"                                      # transient activity indicator
    r"|^Dimming View$",                                  # modal-presentation backdrop
    re.IGNORECASE,
)

# ─────────────────────────────────────────────────────────────────────────────
#  DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

class ElementRole(str, Enum):
    BUTTON          = "Button"
    TEXT_FIELD      = "TextField"
    STATIC_TEXT     = "StaticText"
    IMAGE           = "Image"
    CELL            = "Cell"
    SCROLL_AREA     = "ScrollArea"
    SWITCH          = "Switch"
    PICKER          = "Picker"           # wheel pickers (date, time, etc.)
    ADJUSTABLE      = "Adjustable"       # sliders, steppers
    TAB_BAR         = "TabBar"
    TAB             = "Tab"
    NAVIGATION_BAR  = "NavigationBar"
    TOOLBAR         = "Toolbar"
    ALERT           = "Alert"
    SHEET           = "Sheet"
    TEXT_VIEW       = "TextView"         # multiline editable
    OTHER           = "Other"
    UNKNOWN         = "Unknown"


@dataclass
class AXFrame:
    x: float
    y: float
    width: float
    height: float

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x <= self.x + self.width and \
               self.y <= y <= self.y + self.height


@dataclass
class AXElement:
    """
    One node in the accessibility tree.
    ref:        stable short identifier assigned by the scaffold (not from iOS)
    label:      AXLabel — what VoiceOver says. May be None if missing.
    role:       element type
    value:      current value (text field content, switch on/off, picker value)
    hint:       accessibilityHint (rarely set; extra context)
    frame:      bounding box in simulator coordinates
    enabled:    whether the element can be interacted with
    visible:    whether it's on screen (not scrolled out)
    children:   child elements
    # Enrichment fields (added by AXEnricher, not from iOS)
    inferred_label:   VLM-inferred label for unlabeled elements
    inferred_role:    VLM-inferred role if wrong/missing
    confidence:       0-1 confidence in enrichment
    enrichment_src:   "ax_native" | "vlm_icon" | "vlm_screenshot" | "heuristic"
    # Action routing fields (added by ActionExecutor)
    tap_x:      coordinate to tap (center_x unless overridden)
    tap_y:      coordinate to tap (center_y unless overridden)
    """
    ref:             str              # e.g. "e042"
    label:           Optional[str]
    role:            ElementRole
    value:           Optional[str]    = None
    hint:            Optional[str]    = None
    frame:           Optional[AXFrame] = None
    enabled:         bool              = True
    visible:         bool              = True
    adjustable:      bool              = False  # UIAccessibilityTrait.adjustable
    children:        list              = field(default_factory=list)
    # Enrichment
    inferred_label:  Optional[str]    = None
    inferred_role:   Optional[ElementRole] = None
    confidence:      float             = 1.0
    enrichment_src:  str               = "ax_native"
    # Action routing
    tap_x:           Optional[float]  = None
    tap_y:           Optional[float]  = None
    raw_label:       Optional[str]    = None    # original AXLabel before normalization

    @property
    def effective_label(self) -> Optional[str]:
        """The best available label — native first, inferred fallback."""
        return self.label or self.inferred_label

    @property
    def effective_role(self) -> ElementRole:
        # UIAccessibilityTrait.adjustable overrides the base role — this
        # is critical for COMPACT date pickers / value pickers / steppers
        # whose underlying XCUIElement.ElementType is .textField but whose
        # interaction contract is TAP-then-SCROLL, NOT type. iOS sets the
        # adjustable trait bit on every wheel-style control regardless of
        # the surface role.
        if self.adjustable:
            return ElementRole.ADJUSTABLE
        return self.inferred_role or self.role

    @property
    def effective_tap_x(self) -> Optional[float]:
        if self.tap_x is not None:
            return self.tap_x
        return self.frame.center_x if self.frame else None

    @property
    def effective_tap_y(self) -> Optional[float]:
        if self.tap_y is not None:
            return self.tap_y
        return self.frame.center_y if self.frame else None

    def is_unlabeled(self) -> bool:
        JUNK_LABELS = {"button", "image", "cell", "switch", "text field", ""}
        if self.label is None:
            return True
        return self.label.strip().lower() in JUNK_LABELS

    def to_dict(self) -> dict:
        return {
            "ref":   self.ref,
            "label": self.effective_label,
            "role":  self.effective_role.value,
            "value": self.value,
            "frame": asdict(self.frame) if self.frame else None,
            "enabled": self.enabled,
            "children": [c.to_dict() for c in self.children],
            "_enriched": self.enrichment_src != "ax_native",
        }


@dataclass
class AXTree:
    elements:     List[AXElement]         # flat list (for fast lookup)
    root:         Optional[AXElement]      # hierarchical root
    timestamp_ms: float = field(default_factory=lambda: time.time() * 1000)
    udid:         str = ""
    app_bundle:   str = ""

    def find_by_ref(self, ref: str) -> Optional[AXElement]:
        return next((e for e in self.elements if e.ref == ref), None)

    def find_by_label(self, label: str,
                      role: Optional[ElementRole] = None) -> List[AXElement]:
        label_lower = label.lower()
        return [
            e for e in self.elements
            if e.effective_label and label_lower in e.effective_label.lower()
            and (role is None or e.effective_role == role)
        ]

    def unlabeled(self) -> List[AXElement]:
        return [e for e in self.elements if e.is_unlabeled()]

    def scrollable(self) -> List[AXElement]:
        return [e for e in self.elements
                if e.effective_role == ElementRole.SCROLL_AREA]


# ─────────────────────────────────────────────────────────────────────────────
#  AX READER  —  pulls the raw tree from iOS via idb
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_JUNK_LABELS = {
    # SF Symbol names that leak through as labels
    "star.fill", "heart.fill", "plus", "minus", "xmark", "checkmark",
    "ellipsis", "pencil", "trash", "magnifyingglass", "arrow.left",
    "arrow.right", "arrow.up", "arrow.down", "gear", "info.circle",
    "chevron.right", "chevron.left", "chevron.up", "chevron.down",
    "square.and.arrow.up", "bell", "bell.fill", "person.fill",
    # Generic fallback labels
    "button", "image", "cell", "view", "text field", "text view",
    "switch", "slider", "stepper", "segmented control",
}

ROLE_MAP = {
    # AX-prefixed (standard macOS accessibility)
    "AXButton":           ElementRole.BUTTON,
    "AXTextField":        ElementRole.TEXT_FIELD,
    "AXStaticText":       ElementRole.STATIC_TEXT,
    "AXImage":            ElementRole.IMAGE,
    "AXCell":             ElementRole.CELL,
    "AXScrollArea":       ElementRole.SCROLL_AREA,
    "AXSwitch":           ElementRole.SWITCH,
    "AXPicker":           ElementRole.PICKER,
    "AXAdjustable":       ElementRole.ADJUSTABLE,
    "AXTabBar":           ElementRole.TAB_BAR,
    "AXTab":              ElementRole.TAB,
    "AXNavigationBar":    ElementRole.NAVIGATION_BAR,
    "AXToolbar":          ElementRole.TOOLBAR,
    "AXAlert":            ElementRole.ALERT,
    "AXSheet":            ElementRole.SHEET,
    "AXTextView":         ElementRole.TEXT_VIEW,
    # Non-prefixed variants (returned by some idb versions)
    "Button":             ElementRole.BUTTON,
    "TextField":          ElementRole.TEXT_FIELD,
    "StaticText":         ElementRole.STATIC_TEXT,
    "Image":              ElementRole.IMAGE,
    "Cell":               ElementRole.CELL,
    "ScrollArea":         ElementRole.SCROLL_AREA,
    "Switch":             ElementRole.SWITCH,
    "Picker":             ElementRole.PICKER,
    "Adjustable":         ElementRole.ADJUSTABLE,
    "TabBar":             ElementRole.TAB_BAR,
    "Tab":                ElementRole.TAB,
    "NavigationBar":      ElementRole.NAVIGATION_BAR,
    "Toolbar":            ElementRole.TOOLBAR,
    "Alert":              ElementRole.ALERT,
    "Sheet":              ElementRole.SHEET,
    "TextView":           ElementRole.TEXT_VIEW,
    "Other":              ElementRole.OTHER,
    "Application":        ElementRole.OTHER,
    "Window":             ElementRole.OTHER,
    "ScrollView":         ElementRole.SCROLL_AREA,
    "Table":              ElementRole.SCROLL_AREA,
    "CollectionView":     ElementRole.SCROLL_AREA,
    "PageIndicator":      ElementRole.OTHER,
    "Segmented":          ElementRole.OTHER,
    "Link":               ElementRole.BUTTON,
}


# Role string (from XCUITestReader) → ElementRole enum
XCUITEST_ROLE_MAP: dict = {
    "btn":         ElementRole.BUTTON,
    "input":       ElementRole.TEXT_FIELD,
    "textarea":    ElementRole.TEXT_VIEW,
    "text":        ElementRole.STATIC_TEXT,
    "img":         ElementRole.IMAGE,
    "cell":        ElementRole.CELL,
    "scroll":      ElementRole.SCROLL_AREA,
    "switch":      ElementRole.SWITCH,
    "picker":      ElementRole.PICKER,
    "pickerWheel": ElementRole.PICKER,
    "adj":         ElementRole.ADJUSTABLE,
    "tab":         ElementRole.TAB,
    "tabbar":      ElementRole.TAB_BAR,
    "nav":         ElementRole.NAVIGATION_BAR,
    "toolbar":     ElementRole.TOOLBAR,
    "ALERT":       ElementRole.ALERT,
    "SHEET":       ElementRole.SHEET,
    "search":      ElementRole.TEXT_FIELD,
    "link":        ElementRole.BUTTON,
    "other":       ElementRole.OTHER,
    "app":         ElementRole.OTHER,
    "window":      ElementRole.OTHER,
    "collection":  ElementRole.SCROLL_AREA,
    "table":       ElementRole.SCROLL_AREA,
    "spinner":     ElementRole.OTHER,
    "icon":        ElementRole.IMAGE,
    "web":         ElementRole.OTHER,
    "segmented":   ElementRole.OTHER,
}


# ─── helpers used by AXReader._read_xcuitest ─────────────────────────
#
# Module-level (not nested) so they're easy to unit-test against
# synthetic AX inputs without standing up the full reader.

# Labels that identify iOS Safari chrome elements. Used to derive
# chrome-region y-bounds from the live AX (instead of hardcoding pixel
# thresholds tuned for one device + orientation). Match is
# case-insensitive against `effective_label`.
#
# NOTE on localization: these are English literals. Non-en_US sims
# would silently miss matches. The lookup is now GATED behind role +
# geometric checks (see `_derive_chrome_bounds`), so even when locale
# changes shift the labels out, the role/geometry path still catches
# the chrome — at the cost of being slightly less precise. We're
# accidentally safe today because prewarm pins en_US (task #163).
_SAFARI_TOP_CHROME_LABELS = frozenset({
    # iOS Safari's top status / tab strip — present on some iOS
    # versions, absent on others. The kept set is intentionally narrow:
    # we want false negatives (filter shows more) over false positives
    # (filter shows less).
})
_SAFARI_BOTTOM_CHROME_LABELS = frozenset({
    "address",      # URL bar — primary anchor when kb is down
})
# Pruned 2026-06-06 (Step 3): "previous", "next", "done", "typing
# predictions" — these were treated as chrome so the chrome region
# extended up past them, shrinking the agent's usable area. After
# Step 3, the keyboard accessory bar's elements (Previous/Next/Done/
# predictions) are AGENT-VISIBLE INTERACTIVE UI (Done dismisses kb,
# Next/Previous walk form focus, predictions autofill). Treating them
# as chrome is wrong now. Form fields behind them are still filtered
# by the bare `kb_frame.y` occlusion check.
#
# Pruned earlier: "toolbar" and "tab bar" — tall toolbar containers
# rejected by height ≤ 60 gate; Safari tab bar is a top strip
# rejected by position guard.

# Role keys that may legitimately appear inside the bottom chrome strip
# (URL bar wrapper or keyboard accessory toolbar). Used by
# `_derive_chrome_bounds` to gate label-only matches behind a role
# check. Plain text or status elements with the label "Done" elsewhere
# in the app are rejected by this gate.
#
# CRITICAL: `_derive_chrome_bounds` runs against `xc_tree.elements`
# which carry raw lowercase XCUITest role strings (`"btn"`, `"input"`,
# `"toolbar"`, ...) — they have NOT been mapped through
# XCUITEST_ROLE_MAP yet. We also want unit-test fixtures that set
# `effective_role` to an `ElementRole` enum value to keep working.
# `_chrome_role_key` normalizes both forms to a lowercase string we
# compare against this set.
_BOTTOM_CHROME_ROLE_KEYS = frozenset({
    "btn", "button",        # XCUITest "btn" / ElementRole.BUTTON
    "input", "textfield",   # XCUITest "input" / ElementRole.TEXT_FIELD
    "search",               # XCUITest "search" (URL bar variant)
    "toolbar",
    "other",                # accessory wrapper role is unstable
})


def _chrome_role_key(role) -> "str | None":
    """Normalize a role value to the form stored in
    `_BOTTOM_CHROME_ROLE_KEYS`. Accepts None, a raw lowercase XCUITest
    string (production path — chrome bounds run before role mapping),
    or an `ElementRole` enum (test fixtures, future inversion). Returns
    None when no role info is available (caller treats this as a
    backward-compat pass-through)."""
    if role is None:
        return None
    if hasattr(role, "value"):
        return str(role.value).lower()
    return str(role).lower()


# A bottom-chrome element must be physically short — keyboard accessory
# bars are a single row (~44 px nominal). 60 px gives ample headroom
# for landscape variants without admitting full-height controls that
# happen to share a label.
#
# Cross-language note: the Swift side (`gatherAccessory` in
# `sibb_xcuitest_setup.sh`) uses TWO thresholds — `< 50` for accessory
# BUTTONS (Done / Next / Previous, individually tappable, fixed 44 px
# nominal) and `< 60` for the predictive-bar WRAPPER (`.other` role,
# typically slightly taller). Python's `_derive_chrome_bounds` operates
# at the COARSER granularity of "is this labeled element part of the
# bottom chrome strip at all", so the looser 60-px ceiling matches both
# Swift gates. If the Swift gates ever tighten further, mirror them
# here. Drift here is asymmetric — Swift is the producer, Python the
# consumer.
_BOTTOM_CHROME_MAX_HEIGHT = 60.0




def _derive_chrome_bounds(
        elements, kb_y_top, screen_w, screen_h
) -> "tuple[float, float]":
    """Return `(top_chrome_bottom, bottom_chrome_top)` derived from
    the LIVE AX, falling back to safe defaults if nothing identifies
    as chrome.

    * top_chrome_bottom: y below which an element is considered top-
      chrome. Derived from the max-bottom of small AX text elements
      sitting in the top ~80px (status bar / tab strip). Defaults to
      50px when nothing matches.
    * bottom_chrome_top: y above which an element is considered
      bottom-chrome (URL bar / toolbar / keyboard / accessory bar).
      Derived from the URL bar (label "Address") top if present,
      else the keyboard top if visible, else `screen_h - 100`.

    NOTE: this is INTENTIONALLY conservative. Top-chrome defaults to
    a small strip; bottom-chrome only shrinks when we have positive
    evidence. Over-filtering hurts the agent (it hides usable
    elements); under-filtering only loses the safety net the
    coord-zoomed branch wants. Erring under-filtering.
    """
    # ── top chrome ──
    top_chrome_bottom = 50.0  # safe default — status bar area
    for e in elements:
        fr = getattr(e, "frame", None)
        if fr is None:
            continue
        # Treat any small text/image near y=0 as status-bar candidate.
        if fr.y < 60 and fr.height < 40:
            top_chrome_bottom = max(
                top_chrome_bottom, fr.y + fr.height + 5.0)
    top_chrome_bottom = min(top_chrome_bottom, screen_h * 0.15)

    # ── bottom chrome ──
    bottom_chrome_top = screen_h - 100.0  # safe default
    label_match_min = None
    for e in elements:
        lbl = (getattr(e, "label", None) or "").strip().lower()
        if not lbl:
            continue
        fr = getattr(e, "frame", None)
        if fr is None:
            continue
        if lbl not in _SAFARI_BOTTOM_CHROME_LABELS:
            continue
        # Gate the label match behind role + geometric checks. A "Done"
        # button on a sheet confirmation, a "Next" label on a tutorial,
        # or a tall toolbar elsewhere in the app must NOT shrink the
        # usable area. Real keyboard-accessory chrome is short, sits
        # above the keyboard top (orientation-independent), and has one
        # of the expected roles.
        role_key = _chrome_role_key(getattr(e, "effective_role", None))
        if role_key is not None and role_key not in _BOTTOM_CHROME_ROLE_KEYS:
            continue
        if fr.height > _BOTTOM_CHROME_MAX_HEIGHT:
            continue
        # Position guard. Prefer kb_y_top (orientation-independent and
        # tight: chrome must sit ABOVE the keyboard). When kb_y_top is
        # unknown — kb is dismissed or hasn't been polled yet — fall
        # back to the bottom-half heuristic, which catches the URL bar
        # case (Safari portrait has URL bar at y ~ 0.92 * screen_h).
        if kb_y_top is not None:
            if fr.y >= kb_y_top:
                continue
        else:
            if fr.y < screen_h * 0.5:
                # In portrait, an "Address" / "Done" in the upper half
                # is never the keyboard's accessory or the URL bar.
                continue
        if label_match_min is None or fr.y < label_match_min:
            label_match_min = fr.y
    candidates = [v for v in (label_match_min, kb_y_top) if v is not None]
    if candidates:
        bottom_chrome_top = min(min(candidates) - 5.0, bottom_chrome_top)

    # Sanity clamp: top must stay above bottom (degenerate small
    # screens). The earlier `middle = screen_h * 0.5` floor was too
    # aggressive when the keyboard is up — in landscape iPhone the kb
    # genuinely covers >50% of the screen, so the legitimate bottom-
    # chrome region (kb + accessory) can extend well above the half-
    # screen mark. Clamping bottom UP to `middle + 1` would then mis-
    # classify the kb area as "middle/content".
    #
    # New strategy:
    #   * top_chrome_bottom stays bounded by a small fraction of screen
    #     (`screen_h * 0.15` upstream), so it can't run away.
    #   * bottom_chrome_top is left as-is when there's positive evidence
    #     (label match OR kb signal). If absolutely nothing identified
    #     bottom chrome, fall back to the `screen_h - 100` default.
    #   * Only enforce the non-inversion invariant: bottom > top.
    if bottom_chrome_top <= top_chrome_bottom:
        bottom_chrome_top = top_chrome_bottom + 1.0
    return top_chrome_bottom, bottom_chrome_top


class AXReader:
    """
    Fetches the full accessibility tree from a simulator.

    Primary backend: XCUITestReader (persistent XCUITest server).
      - Full iOS 26 tree, all nested elements, viewport-filtered.
      - Requires sibb_xcuitest_client.py + ~/SIBBHelper built once.
      - Call start(bundle_id) once per episode, stop() at end.

    Fallback: idb (if XCUITest not available).
      - Partial tree on iOS 26 (misses nested elements).
      - No setup required beyond brew install idb-companion.
    """

    def __init__(self, udid: str):
        self.udid          = udid
        self._xcuitest     = None
        self._using_xctest = False
        self._ref_counter  = 0
        # No cross-snapshot state. Zoom detection is per-frame
        # (computed fresh from kb_above_screen + Swift zoom_scale
        # signals). Step 4 (2026-06-07) dropped the latch — empirical
        # probe showed signals are stable across consecutive frames,
        # and the previously-load-bearing overflow heuristic was a
        # false positive on every Safari page with content wider than
        # the viewport (i.e. nearly all of them).

    def _next_ref(self) -> str:
        self._ref_counter += 1
        return f"e{self._ref_counter:04d}"

    async def start(self, bundle_id: str = "com.apple.springboard"):
        """
        Start the XCUITest persistent server.
        Call once per episode before the first read().
        Raises on failure — no silent fallback.
        """
        import importlib.util
        here = os.path.dirname(__file__)
        candidates = [
            os.path.join(here, "sibb_xcuitest_client.py"),
            os.path.join(here, "..", "simulator", "sibb_xcuitest_client.py"),
        ]
        client_path = next((p for p in candidates if os.path.exists(p)), None)
        if not client_path:
            raise RuntimeError(
                "sibb_xcuitest_client.py not found in benchmark/ or simulator/"
            )
        spec = importlib.util.spec_from_file_location(
            "sibb_xcuitest_client", client_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        self._xcuitest = mod.XCUITestReader(self.udid, bundle_id)
        await self._xcuitest.start()
        self._using_xctest = True

    async def stop(self):
        """Stop the XCUITest server. Call at end of episode."""
        if self._xcuitest:
            await self._xcuitest.stop()
            self._xcuitest     = None
            self._using_xctest = False

    async def read(self, use_cache_ms: float = 0) -> AXTree:
        """Fetch the current AX tree. Requires start() to have been called."""
        if not self._using_xctest or not self._xcuitest:
            raise RuntimeError(
                "AXReader not started. Call await reader.start(bundle_id) first."
            )
        return await self._read_xcuitest()

    async def _read_xcuitest(self) -> AXTree:
        """Read via XCUITest — full tree, viewport + keyboard-occlusion
        filtered."""
        xc_tree = await self._xcuitest.observe()
        SKIP_IF_UNLABELED = {
            ElementRole.OTHER,
            ElementRole.NAVIGATION_BAR, ElementRole.TOOLBAR,
        }

        # Visibility filter: drop any element whose frame is NOT fully
        # inside the visible, non-keyboard region of the screen. This
        # covers two concerns:
        #
        #   (a) Keyboard occlusion. When the iOS software keyboard is
        #       up, elements partially or fully below the keyboard top
        #       are unreachable by coordinate tap (taps land on the
        #       keyboard, not on the underlying element). iOS Contacts'
        #       new-contact sheet is the classic case — it doesn't
        #       auto-scroll the form, so the phone/email/address
        #       fields below the keyboard are silently un-tappable.
        #
        #   (b) Viewport clipping. Elements whose frame extends past
        #       the screen edges (top, bottom, left, right) are
        #       partially or fully off-screen and create noise — the
        #       agent might try to tap them and either miss the visible
        #       portion or have the tap reroute to whatever is on top.
        #
        # The focused element is exempt from both — the agent must be
        # able to see what they're currently typing into, even if iOS
        # has scrolled it under the keyboard or partially off-screen.
        kb_frame = getattr(xc_tree, "keyboard_frame", None)
        kb_y_top = kb_frame.get("y") if kb_frame else None
        # NOTE (2026-06-06): the accessory bar (predictive text strip
        # + `Previous`/`Next`/`Done` toolbar) used to be unioned into
        # `kb_y_top` so the visibility filter ALSO rejected elements
        # behind the bar. Empirical probe
        # (`sibb_probe_autozoom_lifecycle.py`) proved the bar's elements
        # are first-class labeled buttons in the AX tree — tapping
        # `Done` dismisses the kb, `Next`/`Previous` walk form focus,
        # prediction words autofill. The agent should SEE and USE them.
        # We keep `accessory_bar_frame` on the tree for diagnostics but
        # no longer use it for occlusion. Form fields BEHIND the bar
        # are still rejected by being below the bare `kb_frame.y` —
        # they ARE genuinely occluded by the kb itself.
        acc_frame = getattr(xc_tree, "accessory_bar_frame", None)
        screen_w = getattr(xc_tree, "screen_width", 402)
        screen_h = getattr(xc_tree, "screen_height", 874)
        # WKWebView zoom scale, if Swift exposed it via KVC on the
        # underlying scrollView. 1.0 = unzoomed. None = unknown (older
        # SIBBHelper builds, or non-Safari context). When known, this
        # is the AUTHORITATIVE zoom signal; the heuristics below are
        # fallbacks.
        zoom_scale_swift = getattr(xc_tree, "zoom_scale", None)
        TOL = 1.0   # px tolerance for AX rounding error

        # ─── orientation ───────────────────────────────────────────────
        # Derived from runtime screen dims rather than queried — Swift
        # already reports the rotated `app.frame`, so screen_w / screen_h
        # implicitly carry the orientation. Exposed on the tree so the
        # tokenizer header and downstream filters can branch.
        orientation = "landscape" if screen_w > screen_h else "portrait"

        # ─── auto-zoom detection (per-frame, stateless) ───────────────
        # iOS Safari auto-zooms when the user focuses an input whose
        # computed font-size is < 16px. We surface this as an
        # informational `AUTO-ZOOMED` tag in the observation header
        # so the agent knows to use `DOUBLE_TAP` to reset.
        #
        # Step 4 (2026-06-07) — design history:
        # - The previous design used a 3-signal cascade (Swift
        #   zoom_scale > overflow heuristic > kb_above_screen) with a
        #   2-snapshot release latch on `AXReader`.
        # - Empirical probe (`sibb_probe_autozoom_lifecycle.py`,
        #   2026-06-06) showed signals are STABLE across consecutive
        #   frames — no flicker. The latch was solving a problem that
        #   didn't exist.
        # - The same probe showed the overflow heuristic is a FALSE
        #   POSITIVE on every Safari page with content wider than the
        #   viewport: baseline state (no zoom) reported max_w/screen_w
        #   = 1.60 because the WebView exposes off-viewport content
        #   at full content width. Overflow as a zoom signal is dead.
        # - The Swift `zoom_scale` KVC probe is a placeholder
        #   (never populated yet); the `kb_above_screen` signal hasn't
        #   been observed firing. So zoom detection is effectively
        #   always False today. That's HONEST — when the agent
        #   experiences a zoom problem, they use DOUBLE_TAP defensively
        #   rather than trusting a header tag we can't reliably set.
        if zoom_scale_swift is not None and zoom_scale_swift > 1.0 + 0.05:
            coord_system_zoomed = True
            zoom_factor = float(zoom_scale_swift)
            zoom_source = "swift"
        elif kb_y_top is not None and kb_y_top > screen_h + TOL:
            coord_system_zoomed = True
            zoom_factor = None
            zoom_source = "kb_above_screen"
        else:
            coord_system_zoomed = False
            zoom_factor = (
                float(zoom_scale_swift)
                if zoom_scale_swift is not None else 1.0)
            zoom_source = None

        # ─── chrome detection (runtime-derived) ────────────────────────
        # The previous version used fixed pixel thresholds (50 px top /
        # screen_h - 100 px bottom) tuned for iPhone 16 portrait. That
        # silently breaks in landscape (screen_h≈402 → cutoff=302 sweeps
        # in form-field y-coords) and on differently-sized devices
        # (SE / Pro Max / iPad have different chrome strips).
        #
        # Instead, derive chrome bounds from observable AX elements:
        #   * top_chrome_bottom: max y of any status-bar-like element
        #     (label is a time-of-day pattern, or role is `text` with
        #     small height near y=0). Falls back to 50px.
        #   * bottom_chrome_top: min y of the URL bar (label "Address"
        #     in Safari) or the keyboard top, whichever is higher up.
        #     Falls back to screen_h - 100.
        # Bounds are computed once per snapshot; surfaced on the tree so
        # downstream diagnostics (turn-log JSONL) can record what the
        # chrome derivation decided this frame.
        top_chrome_bottom, bottom_chrome_top = _derive_chrome_bounds(
            xc_tree.elements, kb_y_top, screen_w, screen_h)

        def _is_fully_visible(frame) -> bool:
            """True iff the element's entire frame is within the
            on-screen, non-keyboard region. Tolerates 1px of frame-
            geometry rounding error."""
            if frame is None:
                return False
            if frame.x < -TOL:
                return False
            if frame.y < -TOL:
                return False
            if frame.x + frame.width > screen_w + TOL:
                return False
            if frame.y + frame.height > screen_h + TOL:
                return False
            if (kb_y_top is not None
                    and frame.y + frame.height > kb_y_top + TOL):
                return False
            return True

        elements: List[AXElement] = []
        kb_filtered_count = 0
        viewport_filtered_count = 0
        for xc_el in xc_tree.elements:
            role = XCUITEST_ROLE_MAP.get(xc_el.role, ElementRole.OTHER)

            # Skip unlabeled structural containers — but carve out
            # adjustable elements (sliders, steppers, picker wheels)
            # which are commonly unlabeled in iOS yet are critical
            # interactive surfaces. The adjustable flag below promotes
            # them to ADJUSTABLE via effective_role.
            if (not xc_el.label and role in SKIP_IF_UNLABELED
                    and not getattr(xc_el, "adjustable", False)):
                continue

            frame = None
            if xc_el.frame:
                frame = AXFrame(
                    x=xc_el.frame.x, y=xc_el.frame.y,
                    width=xc_el.frame.width, height=xc_el.frame.height
                )

            # Visibility filter. Focused element bypasses to preserve
            # "what am I typing into" context for the agent.
            if (frame is not None
                    and not getattr(xc_el, "focused", False)
                    and not _is_fully_visible(frame)):
                # Distinguish kb-occluded from viewport-clipped for
                # diagnostic logging (helps debug "where did element
                # X go?" by inspecting tree.kb_filtered_count vs
                # tree.viewport_filtered_count).
                if (kb_y_top is not None
                        and frame.y + frame.height > kb_y_top + TOL):
                    kb_filtered_count += 1
                else:
                    viewport_filtered_count += 1
                continue

            # Note (2026-06-06): an earlier version of this block
            # filtered ALL non-chrome elements when zoom was detected,
            # on the theory that AX coords are in zoomed-doc space and
            # would mis-tap. That theory turned out to be wrong —
            # screenshot overlay (`sibb_probe_pinch_recovery.py`)
            # confirmed AX element frames are in real screen coords
            # even when auto-zoomed. Elements that extend past the
            # screen edges (e.g. a 563-wide form on a 402 screen) are
            # iOS reporting their FULL extent; the visible portion is
            # tappable at the reported center, and the existing
            # `_is_fully_visible` filter above correctly drops the
            # ones whose entire frame falls outside the viewport.
            # Zoom detection stays as an informational signal in the
            # observation header, but no further filtering.

            el = AXElement(
                ref=self._next_ref(),
                label=xc_el.label,
                raw_label=xc_el.raw_label or xc_el.label,
                role=role,
                value=xc_el.value,
                hint=None,
                frame=frame,
                enabled=xc_el.enabled,
                visible=True,
                adjustable=getattr(xc_el, "adjustable", False),
            )
            el.enrichment_src = "ax_native"
            el.focused = xc_el.focused  # snapshot-derived keyboard focus
            elements.append(el)
        tree = AXTree(
            elements=elements,
            root=elements[0] if elements else None,
            udid=self.udid,
        )
        tree.method = getattr(xc_tree, "method", "snapshot")
        tree.keyboard_visible = getattr(xc_tree, "keyboard_visible", False)
        tree.screen_width  = getattr(xc_tree, "screen_width",  402)
        tree.screen_height = getattr(xc_tree, "screen_height", 874)
        tree.bundle_id     = getattr(xc_tree, "bundle_id", "")
        # Pass keyboard_frame through so downstream consumers (tokenizer
        # header, executor pre-tap occlusion check) can see it.
        tree.keyboard_frame = kb_frame
        tree.kb_filtered_count = kb_filtered_count
        tree.viewport_filtered_count = viewport_filtered_count
        # Surface the zoomed-coord-system detection so the tokenizer
        # can warn the agent (the AX is intentionally sparse in this
        # state — they need to dismiss the keyboard or scroll to
        # restore the unzoomed coord system).
        tree.coord_system_zoomed = coord_system_zoomed
        tree.zoom_factor = zoom_factor
        tree.zoom_source = zoom_source
        # Orientation derived from runtime screen dims.
        tree.orientation = orientation
        # Keyboard-top y (= `kb_frame.y` when kb visible, else None).
        # As of 2026-06-06 this is NO LONGER unioned with the accessory
        # bar — the bar's elements are agent-visible interactive surfaces
        # (Done/Next/Previous/predictions). Replay's pre-tap occlusion
        # guard still consumes this field; semantics unchanged for it.
        tree.keyboard_y_min = kb_y_top
        # Accessory-bar frame kept for diagnostics (JSONL post-hoc).
        # No longer load-bearing for filter decisions; informational only.
        tree.accessory_bar_frame = acc_frame
        # Derived chrome bounds — useful for post-hoc debugging of
        # "why did element X get filtered" questions in the JSONL log.
        tree.top_chrome_bottom = top_chrome_bottom
        tree.bottom_chrome_top = bottom_chrome_top
        # Backend marker for JSONL analysts: the IDB fallback path
        # (_read_idb) doesn't populate orientation / zoom / kb-filter
        # fields. Without this marker, an IDB-backed turn looks
        # indistinguishable from a Safari turn with no signal at all.
        tree.ax_backend = "xcuitest"
        return tree

    async def _read_idb(self, use_cache_ms: float = 0) -> AXTree:
        """Read via idb — partial tree fallback."""
        raw_json = await self._fetch_raw()
        if not hasattr(self, "_cache"):
            self._cache: dict = {}
        tree_hash = hashlib.md5(raw_json.encode()).hexdigest()

        if use_cache_ms > 0 and tree_hash in self._cache:
            cached_tree, cached_time = self._cache[tree_hash]
            if (time.time() * 1000 - cached_time) < use_cache_ms:
                return cached_tree

        raw = json.loads(raw_json)
        elements_flat: List[AXElement] = []
        if isinstance(raw, list):
            root = None
            for item in raw:
                if isinstance(item, dict):
                    el = self._parse_element(item, elements_flat)
                    if root is None:
                        root = el
        else:
            root = self._parse_element(raw, elements_flat)

        tree = AXTree(elements=elements_flat, root=root, udid=self.udid)
        # Backend marker (see _read_xcuitest equivalent). IDB-backed
        # trees lack the orientation / zoom / kb-filter diagnostics —
        # this lets the assistant's JSONL distinguish them from a
        # Safari turn that simply had no signal.
        tree.ax_backend = "idb"
        self._cache[tree_hash] = (tree, time.time() * 1000)
        return tree

    async def _fetch_raw(self) -> str:
        proc = await asyncio.create_subprocess_exec(
            "idb", "ui", "describe-all", "--udid", self.udid, "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode()

    def _parse_element(self, raw: dict, flat: list) -> AXElement:
        raw_label = (raw.get("AXLabel") or raw.get("label") or
                     raw.get("title") or raw.get("accessibility_label") or "")
        raw_role  = (raw.get("AXRole") or raw.get("role") or
                     raw.get("type") or "Unknown")
        raw_value = (raw.get("AXValue") or raw.get("value"))
        raw_hint  = (raw.get("AXHint")  or raw.get("hint"))
        raw_frame = (raw.get("AXFrame") or raw.get("frame"))

        # Normalize label — treat junk as None but keep original for SF lookup
        raw_label_original = raw_label
        label = raw_label
        if label and label.strip().lower() in SYSTEM_JUNK_LABELS:
            label = None

        frame = None
        if raw_frame and isinstance(raw_frame, dict):
            frame = AXFrame(
                x=float(raw_frame.get("x", 0)),
                y=float(raw_frame.get("y", 0)),
                width=float(raw_frame.get("width", 0)),
                height=float(raw_frame.get("height", 0)),
            )
        elif raw_frame and isinstance(raw_frame, str):
            # idb sometimes returns frame as "{{x, y}, {w, h}}" string
            import re
            nums = re.findall(r"[-+]?\d*\.?\d+", raw_frame)
            if len(nums) >= 4:
                frame = AXFrame(x=float(nums[0]), y=float(nums[1]),
                                width=float(nums[2]), height=float(nums[3]))

        el = AXElement(
            ref=self._next_ref(),
            label=label,
            raw_label=raw_label_original,
            role=ROLE_MAP.get(raw_role, ElementRole.UNKNOWN),
            value=str(raw_value) if raw_value is not None else None,
            hint=raw_hint,
            frame=frame,
            enabled=raw.get("AXEnabled", raw.get("enabled", True)),
            visible=raw.get("visible", True),
        )

        for child_raw in (raw.get("AXChildren") or
                           raw.get("children") or []):
            child = self._parse_element(child_raw, flat)
            el.children.append(child)

        flat.append(el)
        return el


# ─────────────────────────────────────────────────────────────────────────────
#  AX ENRICHER  —  fills in missing labels/roles using VLM or heuristics
#  This is where we "change the JSON" by adding inferred fields
# ─────────────────────────────────────────────────────────────────────────────

SF_SYMBOL_LABELS = {
    # Maps SF Symbol name (from raw AXLabel that leaked through) → semantic label
    "star.fill":             "Add to Favorites",
    "star":                  "Favorites",
    "heart.fill":            "Like",
    "heart":                 "Like",
    "plus":                  "Add",
    "plus.circle":           "Add",
    "plus.circle.fill":      "Add",
    "minus":                 "Remove",
    "xmark":                 "Close",
    "xmark.circle.fill":     "Clear",
    "checkmark":             "Done",
    "checkmark.circle.fill": "Completed",
    "ellipsis":              "More options",
    "ellipsis.circle":       "More",
    "pencil":                "Edit",
    "trash":                 "Delete",
    "trash.fill":            "Delete",
    "magnifyingglass":       "Search",
    "arrow.left":            "Back",
    "arrow.right":           "Forward",
    "square.and.arrow.up":   "Share",
    "bell":                  "Notifications",
    "bell.fill":             "Notifications",
    "gear":                  "Settings",
    "info.circle":           "Info",
    "chevron.right":         "Expand",
    "chevron.left":          "Collapse",
    "chevron.up":            "Collapse",
    "chevron.down":          "Expand",
    "person.fill":           "Profile",
    "person.crop.circle":    "Account",
    "calendar":              "Calendar",
    "clock":                 "Clock",
    "map":                   "Map",
    "location.fill":         "Location",
    "phone.fill":            "Call",
    "envelope.fill":         "Email",
    "message.fill":          "Message",
    "camera.fill":           "Camera",
    "photo":                 "Photo",
    "mic.fill":              "Record",
    "speaker.wave.2.fill":   "Audio",
    "play.fill":             "Play",
    "pause.fill":            "Pause",
    "stop.fill":             "Stop",
    "forward.fill":          "Next",
    "backward.fill":         "Previous",
    "shuffle":               "Shuffle",
    "repeat":                "Repeat",
    "list.bullet":           "List",
    "square.grid.2x2":       "Grid",
    "folder.fill":           "Folder",
    "doc.fill":              "Document",
    "lock.fill":             "Locked",
    "lock.open.fill":        "Unlocked",
}


class AXEnricher:
    """
    Enriches an AXTree by filling in missing/junk labels and roles.

    Three enrichment paths (in order of preference):
      1. SF Symbol lookup  — deterministic, ~0ms
      2. Position heuristic — navigation bars, tab bars, etc.
      3. VLM icon crop     — fast, ~200ms per element
      4. VLM full screenshot — slowest, used when icon crop insufficient
    """

    def __init__(self, vlm_client=None):
        """
        vlm_client: async callable(image_bytes: bytes, prompt: str) -> str
        Pass None to use heuristics only (no VLM).
        """
        self.vlm = vlm_client

    async def enrich(self, tree: AXTree,
                     screenshot: Optional[bytes] = None) -> AXTree:
        """
        Enrich unlabeled elements in-place.
        Returns the same tree object (modified).
        Note: we are modifying the Python objects — the JSON that gets
        sent to the LLM will reflect the enrichment via to_dict().
        """
        unlabeled = tree.unlabeled()
        if not unlabeled:
            return tree     # fast path — nothing to do

        for el in unlabeled:
            # Path 1: SF Symbol lookup — check raw_label too (catches stripped junk labels)
            raw_for_sf = (el.raw_label or el.label or "").lower().strip()
            if raw_for_sf in SF_SYMBOL_LABELS:
                el.inferred_label = SF_SYMBOL_LABELS[raw_for_sf]
                el.enrichment_src = "heuristic_sfsymbol"
                el.confidence = 0.95
                continue

            # Path 2: structural heuristic
            inferred = self._structural_heuristic(el, tree)
            if inferred:
                el.inferred_label = inferred
                el.enrichment_src = "heuristic_structural"
                el.confidence = 0.80
                continue

            # Path 3: VLM on cropped icon bounding box
            if self.vlm and screenshot and el.frame:
                label = await self._vlm_icon_crop(el, screenshot)
                if label:
                    el.inferred_label = label
                    el.enrichment_src = "vlm_icon"
                    el.confidence = 0.75
                    continue

            # Path 4: VLM on full screenshot with element highlight
            if self.vlm and screenshot:
                label = await self._vlm_full_screenshot(el, screenshot)
                if label:
                    el.inferred_label = label
                    el.enrichment_src = "vlm_screenshot"
                    el.confidence = 0.60

        # Also check for role mismatches
        for el in tree.elements:
            self._fix_role(el)

        return tree

    def _structural_heuristic(self, el: AXElement,
                               tree: AXTree) -> Optional[str]:
        """
        Infer label from element's position and parent context.
        E.g., a Button in a NavigationBar with no label is likely "Back"
        if it's at x < 60.
        """
        if not el.frame:
            return None

        # Navigation bar back button is almost always at far left
        if (el.role == ElementRole.BUTTON
                and el.frame.x < 60 and el.frame.y < 100):
            nav_bars = [e for e in tree.elements
                        if e.role == ElementRole.NAVIGATION_BAR]
            if nav_bars:
                return "Back"

        # Top-right navigation button is almost always "Done" or "Edit"
        if (el.role == ElementRole.BUTTON
                and el.frame.x > 300 and el.frame.y < 100):
            return None  # too ambiguous without VLM

        return None

    def _fix_role(self, el: AXElement):
        """
        Correct clearly wrong role declarations.
        Key case: an element that looks like a Picker but is declared
        as StaticText (common when custom pickers don't set the trait).
        We detect this from the value format.
        """
        if el.role == ElementRole.STATIC_TEXT and el.value:
            # Time format like "6:45 AM" or "07" suggests a time picker cell
            import re
            if re.match(r'^\d{1,2}:\d{2}\s?(AM|PM)?$', el.value.strip()):
                el.inferred_role = ElementRole.ADJUSTABLE
                el.enrichment_src = el.enrichment_src + "+role_fix"

    async def _vlm_icon_crop(self, el: AXElement,
                              screenshot: bytes) -> Optional[str]:
        """
        Crop the element's bounding box from the screenshot and ask the VLM
        what it is. Efficient because the image is tiny and the prompt is short.
        """
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(screenshot))
            f = el.frame
            crop = img.crop((int(f.x), int(f.y),
                              int(f.x + f.width), int(f.y + f.height)))
            crop_bytes = io.BytesIO()
            crop.save(crop_bytes, format="PNG")

            prompt = (
                "This is a cropped UI element from an iPhone app. "
                "What does this button/element do? "
                "Reply with 3-5 words only, no punctuation. "
                "Example good answers: 'Add to favorites', 'Delete item', 'Share content'. "
                "If you cannot tell, reply 'unknown'."
            )
            label = await self.vlm(crop_bytes.getvalue(), prompt)
            label = label.strip()
            return None if label.lower() == "unknown" else label
        except Exception:
            return None

    async def _vlm_full_screenshot(self, el: AXElement,
                                   screenshot: bytes) -> Optional[str]:
        """
        Show the full screenshot with the element highlighted.
        Used when the cropped icon gives insufficient context
        (e.g., a button that only makes sense in the context of the surrounding UI).
        """
        if not el.frame:
            return None
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(io.BytesIO(screenshot)).convert("RGBA")
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            f = el.frame
            draw.rectangle(
                [f.x, f.y, f.x + f.width, f.y + f.height],
                outline=(255, 80, 0, 255), width=3
            )
            combined = Image.alpha_composite(img, overlay)
            out = io.BytesIO()
            combined.save(out, format="PNG")

            prompt = (
                f"This is an iPhone screenshot. The element highlighted with "
                f"an orange border at position ({f.center_x:.0f}, {f.center_y:.0f}) "
                f"has no accessibility label. What does this element do? "
                f"Reply with 3-5 words only."
            )
            label = await self.vlm(out.getvalue(), prompt)
            return label.strip() or None
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
#  AX TOKENIZER  —  compresses the tree into a compact LLM-friendly format
# ─────────────────────────────────────────────────────────────────────────────

class AXTokenizer:
    """
    Converts an AXTree to a compact string the LLM can consume efficiently.

    Two output formats:
      "flat"   — simple @ref [Role] "label" = value (one line per element)
      "nested" — indented hierarchy (better for navigation-heavy tasks)

    Strips invisible, disabled, and container-only elements.
    Estimates token count before serializing.
    """

    ROLE_SHORT = {
        ElementRole.BUTTON:         "btn",
        ElementRole.TEXT_FIELD:     "input",
        ElementRole.STATIC_TEXT:    "text",
        ElementRole.IMAGE:          "img",
        ElementRole.CELL:           "cell",
        ElementRole.SCROLL_AREA:    "scroll",
        ElementRole.SWITCH:         "switch",
        ElementRole.PICKER:         "picker",
        ElementRole.ADJUSTABLE:     "adj",
        ElementRole.TAB_BAR:        "tabs",
        ElementRole.TAB:            "tab",
        ElementRole.NAVIGATION_BAR: "nav",
        ElementRole.TOOLBAR:        "toolbar",
        ElementRole.ALERT:          "alert",
        ElementRole.SHEET:          "sheet",
        ElementRole.TEXT_VIEW:      "textarea",
        ElementRole.OTHER:          "el",
        ElementRole.UNKNOWN:        "?",
    }

    def tokenize(self, tree: AXTree, fmt: str = "flat",
                 max_elements: int = 150) -> str:
        """
        Serialize the tree. Filters to actionable/informational elements only.

        max_elements: truncate at this count to avoid token overflow.
        Returns a string ready to embed in an LLM prompt.
        """
        actionable = self._filter(tree.elements)[:max_elements]

        if fmt == "flat":
            lines = []
            for el in actionable:
                lines.append(self._format_flat(el))
            return "\n".join(lines)
        else:
            return self._format_nested(tree.root, 0) if tree.root else ""

    def estimate_tokens(self, tree: AXTree) -> int:
        """Rough estimate: 4 chars ≈ 1 token."""
        text = self.tokenize(tree)
        return len(text) // 4

    def _filter(self, elements: List[AXElement]) -> List[AXElement]:
        """Keep only elements that matter to the agent.

        Special case: text-input elements (TEXT_FIELD / TEXT_VIEW) with no
        `effective_label` are still kept (even when their `value` is also
        empty, e.g. just after the user cleared the field). iOS Calendar's
        Edit Event title field is exposed exactly this way — `[input]`
        with empty label and value=current text. Without this carve-out
        the agent never sees the title field and can't rename events.

        NOTE on the `e.visible` check below: our Swift XCUITest handler
        does NOT populate the `visible` key in the response, so
        `AXElement.__init__` defaults it to True via
        `raw.get("visible", True)`. The `if e.visible` test therefore
        never drops anything in practice (audited 2026-05-22 via
        `sibb_probe_visible_flag.py`). It's left here as a no-op safety
        net for the day the Swift side starts populating `visible`
        properly (viewport-clipping). If you're chasing missing elements
        in the agent's observation, the filter that's actually doing the
        work is `effective_label is not None` — not `visible`.
        """
        TEXT_INPUT_ROLES = (ElementRole.TEXT_FIELD, ElementRole.TEXT_VIEW)
        result = []
        for e in elements:
            # Carve-out for unlabeled text inputs. Includes the empty-
            # value case (e.g. the agent has just cleared the field) so
            # the field stays visible across the edit cycle. We still
            # require `visible` here; the visibility-filter audit will
            # adjust this separately if needed.
            if (e.effective_role in TEXT_INPUT_ROLES
                    and e.visible
                    and e.effective_label is None):
                result.append(e)
                continue
            # Carve-out for unlabeled adjustable elements (compact date
            # pickers, value pickers, steppers). iOS Contacts birthday
            # row exposes the date display as a label-less .textField
            # that's actually wheel-driven. The adjustable trait override
            # (AXElement.effective_role) tags it as ADJUSTABLE, but the
            # element's label is still empty. Without this carve-out
            # the agent never sees the picker trigger.
            if (e.adjustable
                    and e.visible
                    and e.effective_label is None):
                result.append(e)
                continue
            # Standard filter.
            if (e.visible
                    and e.effective_label is not None
                    and e.effective_role not in (
                        ElementRole.NAVIGATION_BAR,
                        ElementRole.SCROLL_AREA,
                    )):
                # Labeled [other] cells carry app-emitted info text
                # iOS doesn't promote to [btn]/[cell] — e.g. Maps'
                # per-route summary lines ("21 min, 10:16 ETA · 6.5 mi,
                # Fastest") which sit alongside [btn] "Steps". We
                # surface them as context for the agent. The unlabeled-
                # [other] noise (empty containers) is already pruned
                # by the SKIP_IF_UNLABELED filter earlier in
                # `_read_xcuitest`.
                #
                # Drop the small set of UIKit-emitted noise labels that
                # leak through with OTHER unfiltered (scrollbar chrome,
                # transient loading indicators, modal-backdrop dimming).
                # Anchored regex matches (`^…$`) avoid false-positives
                # on legitimate labels that happen to contain "Loading"
                # or "scroll bar" as a substring. Localized variants
                # are not handled — simulator is locale-pinned to
                # en_US in `sibb_prewarm.sh`; see IOS_SIM_QUIRKS §20.
                if (e.effective_role == ElementRole.OTHER
                        and _NOISE_OTHER_LABEL_RE.match(
                            (e.effective_label or "").strip())):
                    continue
                result.append(e)
        return result

    def _format_flat(self, el: AXElement) -> str:
        """
        Format: @e042 [btn] "Add Alarm" @(335,822)
                @e017 [input] "Title" = "Team Standup" @(201,300)
                @e033 [switch] "Snooze" = on  ✦ (disabled)

        When an element has no `effective_label` (e.g. iOS Calendar's
        Edit Event title field) but does have a value, omit the empty
        label slot — the agent sees just the role + value, like:
            @e021 [input] = "Date Night" @(201,176)
        """
        role_str = self.ROLE_SHORT.get(el.effective_role, "?")
        if el.effective_label is not None:
            label_str = f' "{el.effective_label}"'
            value_str = f' = {el.value}' if el.value else ""
        else:
            # Unlabeled element — kept by _filter only when role is a
            # text input AND value is non-empty. Quote the value so the
            # agent reads it as the field's current text content.
            label_str = ""
            value_str = f' = "{el.value}"' if el.value else ""
        enriched_marker = " ✦" if el.enrichment_src != "ax_native" else ""
        disabled_marker = " (disabled)" if not el.enabled else ""
        focused_marker  = " (focused)"  if getattr(el, "focused", False) else ""
        coords = ""
        if el.frame and (el.frame.width > 0 or el.frame.height > 0):
            # Only emit coords if the element has a non-zero frame.
            # Snapshot-mode AX cells sometimes have (0,0,0,0) frames
            # (e.g. Maps' route-summary info cells which are labeled
            # but not directly tappable). Emitting "@(0,0)" would
            # mislead the agent into trying to tap unreachable points.
            cx = round(el.frame.center_x)
            cy = round(el.frame.center_y)
            coords = f" @({cx},{cy})"
        return (f"@{el.ref} [{role_str}]{label_str}"
                f"{value_str}{coords}{enriched_marker}{focused_marker}{disabled_marker}")

    def _format_nested(self, el: Optional[AXElement], depth: int) -> str:
        if el is None or not el.visible:
            return ""
        indent = "  " * depth
        role_str = self.ROLE_SHORT.get(el.effective_role, "?")
        label_str = f'"{el.effective_label}"' if el.effective_label else "(unlabeled)"
        value_str = f' = {el.value}' if el.value else ""
        line = f"{indent}@{el.ref} [{role_str}] {label_str}{value_str}"
        children_str = "\n".join(
            self._format_nested(c, depth + 1)
            for c in el.children
            if self._format_nested(c, depth + 1)
        )
        return line + ("\n" + children_str if children_str else "")


# ─────────────────────────────────────────────────────────────────────────────
#  AX FOCUS CONTROLLER  —  programmatically moves AX focus to flush stale tree
# ─────────────────────────────────────────────────────────────────────────────

class AXFocusController:
    """
    Controls the iOS accessibility focus programmatically.

    The key technique: moving AX focus forces the OS to re-materialize
    the accessibility tree near the new focus point, flushing stale cached state.

    Two strategies:
      ping_focus  — move focus to a known-stable element (e.g. nav bar title)
                    then move it back. Forces full tree refresh.
      screen_change_notification — simulate a screenChanged notification
                    by triggering a focus move through idb.
    """

    def __init__(self, udid: str):
        self.udid = udid

    async def flush_stale_tree(self, strategy: str = "tap_offscreen") -> None:
        """
        Force the AX server to rebuild the tree by moving focus.

        strategy options:
          "tap_offscreen"   — tap a safe coordinate (status bar area) to
                              shift focus, then tap back. Very reliable.
          "wait"            — just wait 300ms. Simpler but slower.
        """
        if strategy == "tap_offscreen":
            # Tap the very top of the screen (status bar — no interactive elements)
            # This moves VoiceOver cursor to the status bar, forcing tree refresh
            await self._idb_tap(195, 10)
            await asyncio.sleep(0.05)
            # Do NOT tap back — the agent's next read will get the fresh tree
        elif strategy == "wait":
            await asyncio.sleep(0.30)

    async def wait_for_stable(self, timeout: float = 3.0,
                               silence_window: float = 0.15) -> bool:
        """
        Block until the UI has been quiet for silence_window seconds.
        Uses AXObserver-style polling (real AXObserver requires Obj-C;
        this approximates it by checking if the tree hash changes).
        Returns True if stable was reached, False if timeout.
        """
        reader = AXReader(self.udid)
        last_hash = None
        last_change_time = time.time()
        start = time.time()

        while time.time() - start < timeout:
            await asyncio.sleep(0.05)
            try:
                raw = await reader._fetch_raw()
                current_hash = hashlib.md5(raw.encode()).hexdigest()
                if current_hash != last_hash:
                    last_hash = current_hash
                    last_change_time = time.time()
                elif time.time() - last_change_time >= silence_window:
                    return True   # stable
            except Exception:
                pass

        return False   # timed out

    async def _idb_tap(self, x: float, y: float) -> None:
        proc = await asyncio.create_subprocess_exec(
            "idb", "ui", "tap", str(x), str(y), "--udid", self.udid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()


# ─────────────────────────────────────────────────────────────────────────────
#  ACTION EXECUTOR  —  translates LLM actions → iOS commands
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AgentAction:
    """
    The action the LLM wants to take. Parsed from the LLM's output.
    """
    action_type: str      # "tap" | "double_tap" | "type" | "scroll" | "adjust" | "swipe" | "press" | "done" | "fail" | "answer" | "pinch" | "fling" | "clear" | "observe"
    target_ref:  Optional[str]   = None    # @e042
    target_label: Optional[str]  = None    # fallback if ref not found
    target_x:    Optional[float] = None    # raw coordinate tap (no element lookup)
    target_y:    Optional[float] = None
    text:         Optional[str]  = None    # for "type"
    direction:    Optional[str]  = None    # for "scroll"/"swipe": up/down/left/right
    amount:       float           = 1.0    # for "scroll": pages; for "adjust": steps
    reason:       Optional[str]  = None    # for "fail"/"done"
    # `raw_verb` records the literal verb the agent emitted, BEFORE any
    # aliasing or translation. SCROLL_PAGE dispatches as `action_type=
    # "swipe"` with an inverted direction; without raw_verb the JSONL
    # can't distinguish a genuine SWIPE from a SCROLL_PAGE. Set in the
    # parser branch; downstream may emit it next to action_type.
    raw_verb:     Optional[str]  = None
    # ANSWER terminal carries a structured JSON object payload. The
    # generator declares the expected shape in the per-task instruction;
    # the verifier reads `answer_payload` via the `agent.answer` resource
    # and matches against an `agent_answer` check kind. Strict by design:
    # a non-dict JSON value (list/string/number at top level) is rejected
    # at parse time; `parse_error` records the reason and `answer_payload`
    # stays None so the verifier scores 0.
    answer_payload: Optional[Dict[str, Any]] = None  # parsed JSON object
    parse_error:    Optional[str]            = None  # set when ANSWER JSON is malformed


class ActionExecutor:
    """
    Translates AgentAction → idb commands.

    Critical design point: ALL actions resolve through coordinates,
    not through AX element identifiers. idb does not support tapping
    by AX ref — it only supports tap(x, y). The scaffold bridges this:

    1. Look up the element by ref in the tree → get its frame
    2. If the element was VLM-enriched (no native AX identity),
       we still have its frame from the screenshot analysis
    3. Tap frame.center_x, frame.center_y

    This means EVEN ELEMENTS THAT DON'T EXIST IN THE NATIVE AX TREE
    can be acted upon, as long as the VLM gave us their coordinates.
    """

    def __init__(self, udid: str, focus_ctrl: AXFocusController):
        self.udid = udid
        self.focus = focus_ctrl

    async def execute(self, action: AgentAction,
                      tree: AXTree) -> dict:
        """
        Execute an action and return a result dict with:
          success: bool
          error:   str (if failed)
          coords:  (x, y) that were tapped (for logging)
        """
        try:
            if action.action_type == "tap":
                return await self._execute_tap(action, tree)
            elif action.action_type == "type":
                return await self._execute_type(action, tree)
            elif action.action_type == "scroll":
                return await self._execute_scroll(action, tree)
            elif action.action_type == "adjust":
                return await self._execute_adjust(action, tree)
            elif action.action_type == "swipe":
                return await self._execute_swipe(action, tree)
            elif action.action_type in ("done", "fail"):
                return {"success": True, "terminal": True,
                        "reason": action.reason}
            else:
                return {"success": False,
                        "error": f"Unknown action type: {action.action_type}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _resolve_element(self, action: AgentAction,
                                tree: AXTree) -> Optional[AXElement]:
        """
        Find the element from ref or label. Returns None if not found.
        The element's tap coordinates come from its frame — which is present
        even for VLM-enriched elements.
        """
        if action.target_ref:
            el = tree.find_by_ref(action.target_ref)
            if el:
                return el

        if action.target_label:
            candidates = tree.find_by_label(action.target_label)
            if candidates:
                # Prefer enabled elements
                enabled = [c for c in candidates if c.enabled]
                return (enabled or candidates)[0]

        return None

    async def _execute_tap(self, action: AgentAction,
                            tree: AXTree) -> dict:
        el = await self._resolve_element(action, tree)
        if not el:
            return {"success": False,
                    "error": f"Element not found: ref={action.target_ref} "
                              f"label={action.target_label}"}
        if not el.enabled:
            return {"success": False,
                    "error": f"Element {el.ref} is disabled"}

        x = el.effective_tap_x
        y = el.effective_tap_y
        if x is None or y is None:
            return {"success": False,
                    "error": f"Element {el.ref} has no frame/coordinates"}

        await self._idb_tap(x, y)
        return {"success": True, "coords": (x, y), "ref": el.ref}

    async def _execute_type(self, action: AgentAction,
                             tree: AXTree) -> dict:
        # First tap the target to focus it
        tap_result = await self._execute_tap(action, tree)
        if not tap_result.get("success"):
            return tap_result

        await asyncio.sleep(0.2)   # wait for keyboard

        if action.text:
            proc = await asyncio.create_subprocess_exec(
                "idb", "ui", "text", action.text, "--udid", self.udid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()

        return {"success": True, "typed": action.text}

    async def _execute_scroll(self, action: AgentAction,
                               tree: AXTree) -> dict:
        """
        Scroll a ScrollArea element. Uses idb swipe with computed
        start/end coordinates based on the scroll area's frame.
        """
        el = await self._resolve_element(action, tree)
        if not el:
            # Fall back: scroll the center of the screen
            el_frame = AXFrame(x=0, y=100, width=390, height=620)
        else:
            el_frame = el.frame or AXFrame(x=0, y=100, width=390, height=620)

        cx = el_frame.center_x
        cy = el_frame.center_y
        h  = el_frame.height * 0.4 * action.amount   # scroll distance

        DIRECTION_VECTORS = {
            "up":    (cx, cy + h, cx, cy - h),   # drag up = content scrolls up
            "down":  (cx, cy - h, cx, cy + h),
            "left":  (cx + h, cy, cx - h, cy),
            "right": (cx - h, cy, cx + h, cy),
        }
        direction = (action.direction or "down").lower()
        sx, sy, ex, ey = DIRECTION_VECTORS.get(direction,
                                                DIRECTION_VECTORS["down"])

        proc = await asyncio.create_subprocess_exec(
            "idb", "ui", "swipe",
            str(sx), str(sy), str(ex), str(ey), "0.3",
            "--udid", self.udid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return {"success": True, "direction": direction}

    async def _execute_adjust(self, action: AgentAction,
                               tree: AXTree) -> dict:
        """
        Adjust an adjustable element (picker, slider, stepper).
        idb does not natively support AX adjust — we simulate it with
        swipe gestures on the element.

        For a wheel picker: swipe up = increment, swipe down = decrement.
        amount = number of steps.
        """
        el = await self._resolve_element(action, tree)
        if not el:
            return {"success": False,
                    "error": f"Adjustable element not found: {action.target_ref}"}

        if not el.frame:
            return {"success": False, "error": "Element has no frame"}

        # Tap to focus first
        await self._idb_tap(el.effective_tap_x, el.effective_tap_y)
        await asyncio.sleep(0.1)

        direction = (action.direction or "up").lower()
        cx = el.frame.center_x
        cy = el.frame.center_y
        step_distance = min(el.frame.height * 0.3, 40)

        for _ in range(int(action.amount)):
            if direction == "up":
                sx, sy, ex, ey = cx, cy + step_distance, cx, cy - step_distance
            else:
                sx, sy, ex, ey = cx, cy - step_distance, cx, cy + step_distance

            proc = await asyncio.create_subprocess_exec(
                "idb", "ui", "swipe",
                str(sx), str(sy), str(ex), str(ey), "0.2",
                "--udid", self.udid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            await asyncio.sleep(0.15)

        return {"success": True, "steps": int(action.amount)}

    async def _execute_swipe(self, action: AgentAction,
                              tree: AXTree) -> dict:
        """
        Swipe gesture on an element — used for swipe-to-delete,
        swipe-to-archive, and similar context actions.
        """
        el = await self._resolve_element(action, tree)
        if not el or not el.frame:
            return {"success": False, "error": "Element/frame not found"}

        direction = (action.direction or "left").lower()
        cx = el.frame.center_x
        cy = el.frame.center_y
        w  = el.frame.width * 0.7

        SWIPE_VECTORS = {
            "left":  (cx + w/2, cy, cx - w/2, cy),
            "right": (cx - w/2, cy, cx + w/2, cy),
            "up":    (cx, cy + w/2, cx, cy - w/2),
            "down":  (cx, cy - w/2, cx, cy + w/2),
        }
        sx, sy, ex, ey = SWIPE_VECTORS.get(direction, SWIPE_VECTORS["left"])

        proc = await asyncio.create_subprocess_exec(
            "idb", "ui", "swipe",
            str(sx), str(sy), str(ex), str(ey), "0.25",
            "--udid", self.udid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return {"success": True, "direction": direction}

    async def _idb_tap(self, x: float, y: float) -> None:
        proc = await asyncio.create_subprocess_exec(
            "idb", "ui", "tap", str(x), str(y), "--udid", self.udid,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()


# ─────────────────────────────────────────────────────────────────────────────
#  SIBB SCAFFOLD  —  the unified interface
# ─────────────────────────────────────────────────────────────────────────────

class SIBBScaffold:
    """
    Main entry point. Wraps all subsystems into a clean per-turn interface.

    Usage:
        scaffold = SIBBScaffold(udid="...", vlm_client=my_vlm)
        await scaffold.setup()

        for turn in range(max_turns):
            obs = await scaffold.observe()
            llm_response = await llm(obs.prompt_text)
            action = scaffold.parse_action(llm_response)
            result = await scaffold.act(action)
            if result.get("terminal"):
                break
    """

    def __init__(self, udid: str, vlm_client=None,
                 tokenizer_fmt: str = "flat",
                 max_elements: int = 120,
                 stale_strategy: str = "tap_offscreen"):
        self.udid = udid
        self.reader    = AXReader(udid)
        self.enricher  = AXEnricher(vlm_client)
        self.tokenizer = AXTokenizer()
        self.focus     = AXFocusController(udid)
        self.tokenizer_fmt = tokenizer_fmt
        self.max_elements  = max_elements
        self.stale_strategy = stale_strategy
        self._last_tree: Optional[AXTree] = None
        self._screenshot_cache: Optional[bytes] = None

    @dataclass
    class Observation:
        tree:         AXTree
        prompt_text:  str        # ready to inject into LLM prompt
        token_count:  int
        screenshot:   Optional[bytes]
        enriched_count: int      # how many elements were VLM-enriched

    async def observe(self, force_flush: bool = False) -> "SIBBScaffold.Observation":
        """
        Full observation pipeline:
          1. Optionally flush stale tree
          2. Wait for UI to stabilize
          3. Read raw AX tree
          4. Enrich unlabeled elements
          5. Tokenize to LLM-friendly string
          6. Return Observation
        """
        if force_flush:
            await self.focus.flush_stale_tree(self.stale_strategy)

        # Wait for UI stable (debounce)
        await self.focus.wait_for_stable(timeout=3.0, silence_window=0.15)

        # Read raw tree
        tree = await self.reader.read()

        # Get screenshot only if enrichment will be needed
        screenshot = None
        if tree.unlabeled() and self.enricher.vlm:
            screenshot = await self._take_screenshot()
            self._screenshot_cache = screenshot

        # Enrich
        tree = await self.enricher.enrich(tree, screenshot)
        enriched_count = sum(
            1 for e in tree.elements if e.enrichment_src != "ax_native"
        )

        # Tokenize
        token_text = self.tokenizer.tokenize(
            tree, fmt=self.tokenizer_fmt, max_elements=self.max_elements
        )
        token_count = self.tokenizer.estimate_tokens(tree)

        self._last_tree = tree

        return SIBBScaffold.Observation(
            tree=tree,
            prompt_text=token_text,
            token_count=token_count,
            screenshot=screenshot,
            enriched_count=enriched_count,
        )

    def parse_action(self, llm_output: str) -> AgentAction:
        """Parse the LLM's action output into an AgentAction. Records
        the literal verb the agent emitted on `action.raw_verb` so the
        JSONL can distinguish aliased verbs (SCROLL_PAGE → swipe) from
        their canonical dispatch."""
        self._last_parsed_verb = None
        action = self._parse_action_impl(llm_output)
        if action is not None and action.raw_verb is None:
            action.raw_verb = self._last_parsed_verb
        return action

    def _parse_action_impl(self, llm_output: str) -> AgentAction:
        """
        Parse the LLM's action output into an AgentAction.

        Expected LLM output format (simple, robust to variation):
          TAP @e042
          TAP "Add Alarm"
          TAP (200, 400)            raw-coordinate tap, no AX lookup
          TYPE @e017 "Team Standup"
          SCROLL @e033 down 1.5
          ADJUST @e044 up 3
          SWIPE @e021 left
          DONE "Alarm created successfully"
          FAIL "Cannot find Settings app"
          ANSWER {"items": [{"title": "Buy milk"}], "count": 5}
                                    terminal — payload checked by the
                                    `agent_answer` verifier kind. JSON
                                    must be a single-line object. The
                                    per-task instruction declares the
                                    exact shape the verifier expects.
        """
        import re
        # Scan for the LAST line that starts with a recognized action
        # verb. LLMs typically emit reasoning prose first and the
        # action on the last line; older versions of this parser took
        # the first line and silently misparsed reasoning. The first-
        # line fallback is preserved for callers (like human replay)
        # that pass a single bare action.
        _VERBS = {"TAP", "DOUBLE_TAP", "TYPE", "CLEAR",
                   "SCROLL", "SCROLL_PAGE",
                   "FLING", "ADJUST",
                   "SWIPE", "PRESS", "PINCH", "OBSERVE",
                   "RETURN",
                   "DONE", "FAIL", "ANSWER", "CLARIFY"}
        # English-ambiguous verbs: also common English words. Require
        # exact uppercase to avoid false-positives on reasoning prose
        # like "I am done with this challenge" (would otherwise parse
        # as DONE) or "the FAIL case was..." (would otherwise parse as
        # FAIL). Action-only verbs (TAP / FLING / SWIPE etc.) remain
        # case-insensitive — LLMs sometimes emit them lowercase.
        _AMBIGUOUS_VERBS = {"DONE", "FAIL", "ANSWER", "CLARIFY"}
        _ACTION_VERBS = _VERBS - _AMBIGUOUS_VERBS
        raw_lines = [l.strip() for l in llm_output.strip().splitlines()
                     if l.strip()]
        if not raw_lines:
            return AgentAction(action_type="fail",
                                reason="Empty LLM output")
        line = raw_lines[0]
        # Step 5L-D (2026-06-08) — flag the multi-action emission
        # pattern. Walk every line and tag it as either a "verb-line"
        # (first token is a known verb) or "prose". If we find 2+
        # CONSECUTIVE verb-lines (no prose between), that's a true
        # multi-action turn — the LLM intended to fire them in
        # sequence. Reject loudly so the agent fixes its emission.
        #
        # If verb-lines are separated by prose, treat as self-
        # correction ("TAP @x\nActually let me ANSWER...") and keep
        # the prior "last verb wins" behavior — that pattern is
        # common in well-behaved LLM reasoning.
        line_kinds: List[Tuple[str, str]] = []  # (kind, line)
        for cand in raw_lines:
            cand_parts = cand.split()
            if not cand_parts:
                continue
            first_upper = cand_parts[0].upper()
            is_verb_line = (
                first_upper in _VERBS
                and not (first_upper in _AMBIGUOUS_VERBS
                          and cand_parts[0] != first_upper))
            line_kinds.append(
                ("verb", cand) if is_verb_line else ("prose", cand))
        # Detect a consecutive verb-line run of length >= 2.
        consecutive_verbs: List[str] = []
        run: List[str] = []
        for kind, _line in line_kinds:
            if kind == "verb":
                run.append(_line)
            else:
                if len(run) >= 2:
                    consecutive_verbs = run
                    break
                run = []
        if len(run) >= 2 and not consecutive_verbs:
            consecutive_verbs = run
        if consecutive_verbs:
            verbs_emitted = [
                l.split()[0].upper() for l in consecutive_verbs]
            return AgentAction(
                action_type="fail",
                reason=(
                    "Multi-action turn detected — emit EXACTLY ONE "
                    f"action per response. Saw "
                    f"{len(consecutive_verbs)} consecutive verb-lines: "
                    f"{verbs_emitted}. Pick one and re-issue. The "
                    "runner will NOT silently execute only the last "
                    "one (the prior dropped-on-the-floor behavior)."))
        # Otherwise: gather verb-line candidates (possibly interleaved
        # with prose). Pick the LAST one as the chosen action.
        action_candidates = [l for k, l in line_kinds if k == "verb"]
        if action_candidates:
            line = action_candidates[-1]
        else:
            # No line *starts* with a verb. Fall back to scanning the
            # last line for the last occurrence of a verb word anywhere
            # (handles agents that append the action inline with
            # reasoning, e.g. "...so I'll go.SWIPE left"). Take
            # everything from that verb onward as the action.
            last = raw_lines[-1]
            # Two regexes: action verbs are case-insensitive (LLMs
            # may emit lowercase mid-sentence); ambiguous verbs are
            # uppercase-only to block English-prose false-positives.
            # Longest-first so `SCROLL_PAGE` beats `SCROLL` in the
            # alternation (regex picks the first matching alternative,
            # not the longest).
            action_re = re.compile(
                r"\b(" + "|".join(
                    sorted(_ACTION_VERBS, key=len, reverse=True))
                + r")\b",
                re.IGNORECASE)
            ambig_re = re.compile(
                r"\b(" + "|".join(_AMBIGUOUS_VERBS) + r")\b")
            all_matches = (list(action_re.finditer(last))
                           + list(ambig_re.finditer(last)))
            all_matches.sort(key=lambda mm: mm.start())
            if all_matches:
                m = all_matches[-1]
                line = last[m.start():]
        parts = line.split()
        verb = parts[0].upper()
        # Stash the actual scanned verb so the wrapper can attach it as
        # raw_verb on the returned AgentAction. The scan above picks the
        # LAST recognized verb on the LAST non-empty line — same logic
        # the dispatch below uses — so this matches reality.
        self._last_parsed_verb = verb

        def extract_ref(tokens):
            for t in tokens:
                if t.startswith("@") and not t.startswith("@("):
                    return t[1:]   # strip the @
            return None

        def extract_quoted(text):
            m = re.search(r'"([^"]+)"', text)
            return m.group(1) if m else None

        def extract_coord(text):
            # Matches "(123, 456)" or "@(123,456)" or "(123.5, 400)"
            m = re.search(r'@?\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)', text)
            if m:
                return float(m.group(1)), float(m.group(2))
            return None, None

        if verb == "TAP":
            cx, cy = extract_coord(line)
            return AgentAction(
                action_type="tap",
                target_ref=extract_ref(parts[1:]),
                target_label=extract_quoted(line),
                target_x=cx,
                target_y=cy,
            )
        elif verb == "DOUBLE_TAP":
            # Coordinate-based double-tap. Dispatches through XCUITest's
            # native gesture pipeline (XCUICoordinate.doubleTap()), which
            # is the path that fires WebKit's double-tap-to-zoom
            # recognizer. Two rapid TAPs do NOT — verified empirically
            # (see IOS_SIM_QUIRKS §21).
            #
            # PRIMARY USE: reset Safari's auto-zoom after input focus.
            # The agent emits `DOUBLE_TAP (x, y)` on a non-input page
            # region; WebKit's recognizer zooms the WebView back to fit-
            # page. Also useful on Maps (zoom-in) and Photos (fit toggle).
            #
            # Grammar matches TAP exactly:
            #   DOUBLE_TAP @e042             — by ref
            #   DOUBLE_TAP (200, 400)        — by coord
            #   DOUBLE_TAP "Heading"         — by label (case-insensitive substring)
            cx, cy = extract_coord(line)
            return AgentAction(
                action_type="double_tap",
                target_ref=extract_ref(parts[1:]),
                target_label=extract_quoted(line),
                target_x=cx,
                target_y=cy,
            )
        elif verb == "TYPE":
            # Two grammar forms (SYSTEM_PROMPT documented):
            #   TYPE @e017 "text"     — tap-to-focus then type
            #   TYPE "text"           — type to currently-focused element
            #
            # The earlier parser treated `TYPE "x"` as `TYPE label="x"`
            # (label fallback like TAP) — that's wrong. There's no
            # `TYPE "label-substring"` form. When no @ref is given,
            # the quoted string is ALWAYS the text payload.
            ref = extract_ref(parts[1:])
            if ref:
                # `TYPE @ref "text"` — the text is in parts[2:].
                return AgentAction(
                    action_type="type",
                    target_ref=ref,
                    text=extract_quoted(" ".join(parts[2:])),
                )
            # `TYPE "text"` — no ref, no label, just raw text.
            return AgentAction(
                action_type="type",
                text=extract_quoted(line),
            )
        elif verb == "CLEAR":
            # `CLEAR @ref` — wipe the current content of a text field
            # via Swift-side triple-tap-select-all + delete. Use this
            # instead of `TYPE @ref ""` (which is a no-op) when you've
            # typed into the wrong field or want to replace existing
            # text rather than append.
            return AgentAction(
                action_type="clear",
                target_ref=extract_ref(parts[1:]),
                target_label=(extract_quoted(line)
                              if not extract_ref(parts[1:]) else None),
            )
        elif verb == "SCROLL":
            # Accept `SCROLL direction [amount]` (whole-app) or
            # `SCROLL @ref direction [amount]`. Previously the loop
            # started at parts[2:], so a bare `SCROLL down` left
            # direction=None and relied on the executor's default.
            direction = None
            amount = 1.0
            for p in parts[1:]:
                if p.lower() in ("up", "down", "left", "right"):
                    direction = p.lower()
                else:
                    try: amount = float(p)
                    except ValueError: pass
            return AgentAction(
                action_type="scroll",
                target_ref=extract_ref(parts[1:]),
                direction=direction,
                amount=amount,
            )
        elif verb == "FLING":
            # `FLING @ref direction [amount]` — fast, big-jump gesture.
            # Mirrors SCROLL's grammar; differs in gesture parameters
            # (high velocity, larger distance, smaller cap).
            direction = None
            amount = 1.0
            for p in parts[1:]:
                if p.lower() in ("up", "down", "left", "right"):
                    direction = p.lower()
                else:
                    try: amount = float(p)
                    except ValueError: pass
            return AgentAction(
                action_type="fling",
                target_ref=extract_ref(parts[1:]),
                direction=direction,
                amount=amount,
            )
        elif verb == "ADJUST":
            direction = "up"
            amount = 1.0
            for p in parts[2:]:
                if p.lower() in ("up","down"):
                    direction = p.lower()
                else:
                    try: amount = float(p)
                    except ValueError: pass
            return AgentAction(
                action_type="adjust",
                target_ref=extract_ref(parts[1:]),
                direction=direction,
                amount=amount,
            )
        elif verb == "PRESS":
            # Hardware buttons / system gestures:
            #   PRESS home          exits to home screen
            #   PRESS back          left-edge swipe (in-app back nav)
            #   PRESS app_switcher  swipe-up-and-hold (recent apps)
            button = (parts[1].lower() if len(parts) > 1 else "home")
            return AgentAction(
                action_type="press",
                direction=button,   # reuse `direction` field for the button name
            )
        elif verb == "SWIPE":
            # Accept either `SWIPE direction` (whole-app) or
            # `SWIPE @ref direction`. Previously the parser required a
            # ref slot before the direction, so a bare `SWIPE down` was
            # silently rewritten to `SWIPE left`.
            direction = "left"
            for p in parts[1:]:
                if p.lower() in ("up", "down", "left", "right"):
                    direction = p.lower()
                    break
            return AgentAction(
                action_type="swipe",
                target_ref=extract_ref(parts[1:]),
                direction=direction,
            )
        elif verb == "SCROLL_PAGE":
            # Content-direction page scroll — a semantic synonym for
            # SWIPE that maps to the iOS-correct finger direction.
            # iOS SWIPE's `direction` is the FINGER direction (e.g.
            # SWIPE down = finger moves down = content moves down with
            # the finger = page actually scrolls UP). LLMs reliably
            # confuse this with "I want to scroll the page down" and
            # emit SWIPE down to see lower content, then loop when it
            # doesn't work (the clipped-button benchmark surfaced this:
            # Gemini emitted 17 consecutive `SWIPE down` for a task
            # that needed `SWIPE up`).
            #
            # SCROLL_PAGE takes CONTENT direction:
            #   SCROLL_PAGE down   → see lower content → emits SWIPE up
            #   SCROLL_PAGE up     → see higher content → emits SWIPE down
            #   SCROLL_PAGE right  → see content to the right → SWIPE left
            #   SCROLL_PAGE left   → see content to the left → SWIPE right
            #
            # Optional amount (parity with SCROLL): `SCROLL_PAGE down 3`
            # repeats the swipe 3 times. Defaults to 1.
            content_dir = None
            amount = None
            for p in parts[1:]:
                low = p.lower()
                if low in ("up", "down", "left", "right"):
                    content_dir = low
                    continue
                if amount is None:
                    try:
                        amount = float(p)
                    except ValueError:
                        pass
            # Default to "down" (most common — agent wants to see more
            # content below the fold).
            if content_dir is None:
                content_dir = "down"
            # Defensive: tolerate future direction additions without
            # crashing. Unknown directions fall through to "up" (the
            # finger direction that reveals content below), matching
            # the default content-down case.
            inverted = {
                "up": "down", "down": "up",
                "left": "right", "right": "left",
            }.get(content_dir, "up")
            return AgentAction(
                action_type="swipe",
                target_ref=extract_ref(parts[1:]),
                direction=inverted,
                amount=(amount if amount is not None else 1.0),
            )
        elif verb == "PINCH":
            # Two-finger pinch. Accept:
            #   PINCH out         — zoom out  (scale 0.5)
            #   PINCH in          — zoom in   (scale 2.0)
            #   PINCH <scale>     — explicit  (e.g. `PINCH 0.6`)
            # Primary use: recover from iOS Safari's auto-zoom on
            # input focus (the AUTO-ZOOMED tag in the obs header). Also
            # general-purpose for Maps / Photos pinch interactions.
            #
            # `direction` field carries "out"/"in"; `amount` carries the
            # explicit scale when the agent provided one (otherwise the
            # executor maps direction → scale).
            direction = None
            scale = None
            for p in parts[1:]:
                low = p.lower()
                if low in ("out", "in"):
                    direction = low
                    continue
                # Try parse as float.
                try:
                    f = float(p)
                    if f > 0 and f < 100:
                        scale = f
                except ValueError:
                    pass
            if direction is None and scale is None:
                direction = "out"   # default = the canonical recovery
            return AgentAction(
                action_type="pinch",
                direction=direction,
                amount=scale if scale is not None else 1.0,
            )
        elif verb == "RETURN":
            # `RETURN` — fire the Return key into the currently keyboard-
            # focused element. Used to COMMIT a typed URL in Safari's URL
            # bar, submit an in-app search, confirm a "name this" dialog,
            # or submit a form on its last field. iOS dispatches the
            # Return event against the focused field's configuration, so
            # the same verb covers `Go` / `Search` / `Done` / `Next` /
            # plain `Return` semantics without us labelling them.
            #
            # Argless on purpose: there's only one keyboard-focused
            # element at a time, and `\n` flows through that focus.
            # Compose with `TAP @ref` + `RETURN` on two turns if you need
            # to focus first.
            return AgentAction(action_type="return")
        elif verb == "OBSERVE":
            # `OBSERVE` / `OBSERVE <ms>` — pure no-op on the simulator's
            # state. Sleeps for `ms` (clamped to [0, 10000]) then returns,
            # so the loop's top-of-turn observation picks up whatever
            # changed in the meantime. Useful when an async UI process
            # is in flight (Maps route computation, network spinners,
            # animation settling) — the agent shouldn't TAP/SCROLL to
            # "force" progress because those can interrupt the
            # background work. Bare `OBSERVE` (no arg) waits 0 ms — an
            # immediate re-observe.
            wait_ms = 0.0
            for tok in parts[1:]:
                try:
                    wait_ms = float(tok)
                    break
                except ValueError:
                    continue
            # Clamp: negative = 0, max 10 s so a confused agent can't
            # park for minutes.
            wait_ms = max(0.0, min(wait_ms, 10000.0))
            return AgentAction(action_type="observe", amount=wait_ms)
        elif verb == "DONE":
            return AgentAction(action_type="done",
                               reason=extract_quoted(line) or "Task complete")
        elif verb == "FAIL":
            return AgentAction(action_type="fail",
                               reason=extract_quoted(line) or "Task failed")
        elif verb == "ANSWER":
            # Everything after the verb on the same line is the JSON.
            # `line` is already the first line of llm_output (multi-line
            # JSON is intentionally unsupported — single-line keeps the
            # contract unambiguous for the parser and the model).
            raw = line[len("ANSWER"):].strip()
            # Tolerate one set of surrounding backtick fences and an
            # optional `json` language tag — common LLM habits even
            # when the prompt says "one line".
            if raw.startswith("```"):
                raw = raw[3:]
                if raw.lower().startswith("json"):
                    raw = raw[len("json"):]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            elif raw.startswith("`") and raw.endswith("`") and len(raw) >= 2:
                raw = raw[1:-1].strip()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as e:
                return AgentAction(
                    action_type="answer",
                    answer_payload=None,
                    parse_error=f"ANSWER JSON parse error: {e}",
                )
            if not isinstance(payload, dict):
                return AgentAction(
                    action_type="answer",
                    answer_payload=None,
                    parse_error=(
                        f"ANSWER payload must be a JSON object, got "
                        f"{type(payload).__name__}"
                    ),
                )
            return AgentAction(action_type="answer",
                               answer_payload=payload)
        else:
            return AgentAction(action_type="fail",
                               reason=f"Unrecognized action: {verb}")

    async def act(self, action: AgentAction) -> dict:
        """
        Execute an action, then flush focus to prevent stale tree on next observe().
        """
        executor = ActionExecutor(self.udid, self.focus)
        result = await executor.execute(action, self._last_tree or AXTree([], None))

        if result.get("success") and action.action_type not in ("done", "fail"):
            # Post-action: flush focus proactively so next observe() gets fresh tree
            await self.focus.flush_stale_tree(self.stale_strategy)

        return result

    async def _take_screenshot(self) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "xcrun", "simctl", "io", self.udid, "screenshot", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return stdout


# ─────────────────────────────────────────────────────────────────────────────
#  DEMO
# ─────────────────────────────────────────────────────────────────────────────

async def demo():
    """
    Shows how the scaffold would be used in an RL rollout turn.
    Uses mock data since idb/simulator isn't running here.
    """
    print("=== SIBB Scaffold — Component Demo ===\n")

    # ── 1. AXReader: parse a raw idb JSON (mocked) ─────────────────────────
    reader = AXReader("MOCK_UDID")
    mock_raw = {
        "AXRole": "AXApplication",
        "AXLabel": "Clock",
        "AXFrame": {"x": 0, "y": 0, "width": 390, "height": 844},
        "AXChildren": [
            {
                "AXRole": "AXNavigationBar",
                "AXLabel": "Alarm",
                "AXFrame": {"x": 0, "y": 44, "width": 390, "height": 44},
                "AXChildren": [
                    {
                        "AXRole": "AXButton",
                        "AXLabel": "plus",          # ← junk SF symbol label
                        "AXFrame": {"x": 340, "y": 52, "width": 40, "height": 28},
                    }
                ]
            },
            {
                "AXRole": "AXScrollArea",
                "AXLabel": "Alarms",
                "AXFrame": {"x": 0, "y": 88, "width": 390, "height": 700},
                "AXChildren": [
                    {
                        "AXRole": "AXCell",
                        "AXLabel": "6:45 AM, Mon Wed Fri, Gym, alarm",
                        "AXFrame": {"x": 0, "y": 100, "width": 390, "height": 60},
                        "AXChildren": [
                            {
                                "AXRole": "AXSwitch",
                                "AXLabel": "6:45 AM",
                                "AXValue": "1",
                                "AXFrame": {"x": 320, "y": 110, "width": 51, "height": 31},
                            }
                        ]
                    }
                ]
            },
            {
                "AXRole": "AXTabBar",
                "AXFrame": {"x": 0, "y": 780, "width": 390, "height": 64},
                "AXChildren": [
                    {"AXRole": "AXTab", "AXLabel": "World Clock",
                     "AXFrame": {"x": 0, "y": 785, "width": 98, "height": 49}},
                    {"AXRole": "AXTab", "AXLabel": "Alarm",
                     "AXFrame": {"x": 98, "y": 785, "width": 98, "height": 49}},
                    {"AXRole": "AXTab", "AXLabel": "Stopwatch",
                     "AXFrame": {"x": 196, "y": 785, "width": 98, "height": 49}},
                    {"AXRole": "AXTab", "AXLabel": "Timer",
                     "AXFrame": {"x": 294, "y": 785, "width": 96, "height": 49}},
                ]
            }
        ]
    }
    flat = []
    root = reader._parse_element(mock_raw, flat)
    tree = AXTree(elements=flat, root=root, udid="MOCK_UDID")

    print(f"Parsed {len(flat)} elements from mock AX tree")
    unlabeled = tree.unlabeled()
    print(f"Unlabeled elements: {len(unlabeled)}")
    for el in unlabeled:
        print(f"  {el.ref}: role={el.role.value}, raw_label={el.label!r}, "
              f"frame=({el.frame.center_x:.0f},{el.frame.center_y:.0f})")

    # ── 2. AXEnricher: repair the SF symbol label ──────────────────────────
    print("\n--- After enrichment ---")
    enricher = AXEnricher(vlm_client=None)   # no VLM in demo
    tree = await enricher.enrich(tree, screenshot=None)
    for el in flat:
        if el.enrichment_src != "ax_native":
            print(f"  {el.ref}: '{el.label}' → inferred '{el.inferred_label}' "
                  f"(src={el.enrichment_src}, conf={el.confidence})")

    # ── 3. AXTokenizer: compact LLM-ready representation ──────────────────
    tokenizer = AXTokenizer()
    flat_output = tokenizer.tokenize(tree, fmt="flat")
    est_tokens  = tokenizer.estimate_tokens(tree)
    print(f"\n--- Tokenized tree (flat format, ~{est_tokens} tokens) ---")
    print(flat_output)

    # ── 4. ActionParser: parse LLM outputs ─────────────────────────────────
    scaffold = SIBBScaffold("MOCK_UDID")
    print("\n--- Action parsing ---")
    test_outputs = [
        'TAP @e0002',
        'TAP "Add"',
        'TYPE @e0005 "Gym session"',
        'SCROLL @e0003 down 1.5',
        'ADJUST @e0007 up 3',
        'SWIPE @e0004 left',
        'DONE "Alarm created"',
        'FAIL "Button not found"',
    ]
    for out in test_outputs:
        action = scaffold.parse_action(out)
        print(f"  {out!r:40s} → {action.action_type:8s} "
              f"ref={action.target_ref} label={action.target_label!r} "
              f"text={action.text!r} dir={action.direction} "
              f"amount={action.amount if action.amount != 1.0 else ''}")

    # ── 5. Enrichment-to-action round-trip ─────────────────────────────────
    print("\n--- Enrichment → action round-trip ---")
    # Find the enriched "plus" → "Add" button
    add_btn = next((e for e in flat if e.inferred_label == "Add"), None)
    if add_btn:
        print(f"Enriched button: ref={add_btn.ref} "
              f"effective_label='{add_btn.effective_label}' "
              f"tap_coords=({add_btn.effective_tap_x:.0f}, "
              f"{add_btn.effective_tap_y:.0f})")
        print(f"LLM output: 'TAP @{add_btn.ref}'")
        action = scaffold.parse_action(f"TAP @{add_btn.ref}")
        print(f"Parsed: action_type={action.action_type} "
              f"target_ref={action.target_ref}")
        print(f"Would execute: idb ui tap "
              f"{add_btn.effective_tap_x:.0f} "
              f"{add_btn.effective_tap_y:.0f} --udid MOCK_UDID")

if __name__ == "__main__":
    asyncio.run(demo())
