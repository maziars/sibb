#!/usr/bin/env python3
"""
SIBB Screen Inspector
======================
Navigate the iOS Simulator, press ENTER to capture what the LLM would see.
Uses the scaffold's AXReader → AXEnricher → AXTokenizer pipeline exactly
as it runs during benchmark episodes.

Usage:
    python3 sibb_inspect_screen.py <UDID> [--bundle <bundle_id>]
    python3 sibb_inspect_screen.py <UDID> --watch
    python3 sibb_inspect_screen.py <UDID> --once
    python3 sibb_inspect_screen.py <UDID> --format all

Run with:
    /Library/Developer/CommandLineTools/usr/bin/python3 sibb_inspect_screen.py

Requirements:
    sibb_scaffold.py + sibb_xcuitest_client.py in same directory
    ~/SIBBHelper built: ./sibb_xcuitest_setup.sh <UDID>
"""

import sys, os, argparse, asyncio, json, time
from datetime import datetime
from collections import defaultdict
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    from sibb_scaffold import (
        AXReader, AXEnricher, AXTokenizer, AXFocusController,
        AXElement, AXTree, ElementRole
    )
except ImportError as e:
    print(f"ERROR: Cannot import sibb_scaffold — {e}")
    sys.exit(1)

# ── Terminal colours ──────────────────────────────────────────────────────────
R  = "\033[0m";  B  = "\033[1m"
CY = "\033[36m"; GR = "\033[32m"; YE = "\033[33m"
BL = "\033[34m"; RE = "\033[31m"; GY = "\033[90m"
WH = "\033[97m"

ROLE_COLOUR = {
    ElementRole.BUTTON:         GR,
    ElementRole.TEXT_FIELD:     BL,
    ElementRole.TEXT_VIEW:      BL,
    ElementRole.SWITCH:         YE,
    ElementRole.PICKER:         YE,
    ElementRole.ADJUSTABLE:     YE,
    ElementRole.TAB:            CY,
    ElementRole.ALERT:          RE,
    ElementRole.SHEET:          RE,
    ElementRole.STATIC_TEXT:    GY,
    ElementRole.CELL:           WH,
    ElementRole.IMAGE:          GY,
    ElementRole.NAVIGATION_BAR: GY,
    ElementRole.TOOLBAR:        GY,
    ElementRole.TAB_BAR:        CY,
}
ROLE_ICON = {
    ElementRole.BUTTON:         "⬡",
    ElementRole.TEXT_FIELD:     "✎",
    ElementRole.TEXT_VIEW:      "✎",
    ElementRole.SWITCH:         "◉",
    ElementRole.PICKER:         "◎",
    ElementRole.ADJUSTABLE:     "◎",
    ElementRole.TAB:            "⊞",
    ElementRole.ALERT:          "⚠",
    ElementRole.SHEET:          "⚠",
    ElementRole.STATIC_TEXT:    "·",
    ElementRole.CELL:           "▶",
    ElementRole.NAVIGATION_BAR: "—",
    ElementRole.TOOLBAR:        "—",
    ElementRole.TAB_BAR:        "⊟",
}


# ─────────────────────────────────────────────────────────────────────────────
#  DISPLAY
# ─────────────────────────────────────────────────────────────────────────────

def fmt_el(el: AXElement) -> str:
    icon     = ROLE_ICON.get(el.effective_role, "?")
    colour   = ROLE_COLOUR.get(el.effective_role, WH)
    label    = el.effective_label or "(unlabeled)"
    value    = f"  = \"{el.value}\"" if el.value else ""
    enriched = f"  {YE}✦{el.enrichment_src}{R}" if el.enrichment_src != "ax_native" else ""
    disabled = f"  {GY}(disabled){R}" if not el.enabled else ""
    coords   = ""
    if el.frame:
        cx = round(el.frame.center_x)
        cy = round(el.frame.center_y)
        coords = f"  {GY}@({cx},{cy}){R}"
    return (f"  {colour}{icon} {B}@{el.ref}{R} "
            f"{colour}[{el.effective_role.value}]{R} "
            f"{WH}\"{label}\"{R}"
            f"{GY}{value}{R}{enriched}{disabled}{coords}")


def section_lines(title, items, colour=WH):
    if not items: return []
    lines = [f"\n  {colour}{B}── {title} ──{R}"]
    for el in items: lines.append(fmt_el(el))
    return lines


def render_visual(tree: AXTree) -> str:
    els   = tree.elements
    lines = []

    def grp(role):
        return [e for e in els if e.effective_role == role
                and e.visible and e.effective_label]
    def grp_multi(roles):
        return [e for e in els if e.effective_role in roles
                and e.visible and e.effective_label]

    alerts  = grp_multi({ElementRole.ALERT, ElementRole.SHEET})
    nav     = grp(ElementRole.NAVIGATION_BAR)
    tabs    = grp(ElementRole.TAB)
    texts   = grp(ElementRole.STATIC_TEXT)
    cells   = grp(ElementRole.CELL)
    buttons = grp(ElementRole.BUTTON)
    inputs  = grp_multi({ElementRole.TEXT_FIELD, ElementRole.TEXT_VIEW})
    switches= grp(ElementRole.SWITCH)
    pickers = grp_multi({ElementRole.PICKER, ElementRole.ADJUSTABLE})
    images  = grp(ElementRole.IMAGE)

    if alerts:
        lines.append(f"\n  {RE}{B}╔══ DIALOG / ALERT ══╗{R}")
        for a in alerts: lines.append(fmt_el(a))
        lines.append(f"  {RE}{B}╚════════════════════╝{R}")

    for n in nav:
        lines.append(f"\n  {GY}{B}── Navigation: \"{n.effective_label}\" ──{R}")

    lines += section_lines("Tabs",          tabs,    CY)
    lines += section_lines("Text",          texts,   GY)
    lines += section_lines("Cells / Rows",  cells,   WH)
    lines += section_lines("Buttons",       buttons, GR)
    lines += section_lines("Input Fields",  inputs,  BL)
    lines += section_lines("Switches",      switches,YE)
    lines += section_lines("Pickers",       pickers, YE)
    lines += section_lines("Images",        images,  GY)

    return "\n".join(lines)


def print_llm_section(flat_text: str):
    print(f"\n{B}{'─'*68}{R}")
    print(f"{B}  LLM OBSERVATION — exact text sent to model{R}")
    print(f"{GY}  @ref [role] \"label\" = value  ✦=enriched{R}")
    print(f"{B}{'─'*68}{R}")
    for line in flat_text.split("\n"):
        if not line.strip(): continue
        if "[Button]" in line or "[btn]" in line: c = GR
        elif "[TextField]" in line or "[TextV" in line: c = BL
        elif "[Switch]" in line or "[Picker]" in line: c = YE
        elif "[Tab]" in line: c = CY
        elif "[Alert]" in line or "[Sheet]" in line: c = RE
        elif "[StaticText]" in line or "[text]" in line: c = GY
        else: c = WH
        line_out = line.replace("✦", f"{YE}✦{R}{c}")
        print(f"  {c}{line_out}{R}")


# ─────────────────────────────────────────────────────────────────────────────
#  CAPTURE — same pipeline as scaffold.observe()
# ─────────────────────────────────────────────────────────────────────────────

async def capture(reader: AXReader, args):
    """One observation cycle using the exact scaffold pipeline."""
    enricher  = AXEnricher(vlm_client=None)
    tokenizer = AXTokenizer()

    t0   = time.time()
    tree = await reader.read()
    ms   = round((time.time() - t0) * 1000)
    method = getattr(tree, "method", "snapshot")  # snapshot path vs slow fallback

    if not tree.elements:
        print(f"\n{RE}  No elements — is the simulator booted with an app open?{R}")
        return

    tree = await enricher.enrich(tree, screenshot=None)

    n_total    = len(tree.elements)
    n_enriched = sum(1 for e in tree.elements if e.enrichment_src != "ax_native")
    n_unlabeled = len(tree.unlabeled())
    est_tokens  = tokenizer.estimate_tokens(tree)
    backend     = "XCUITest" if reader._using_xctest else "idb"

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"\n{B}{'═'*68}{R}")
    print(f"{B}  SIBB Screen Inspector{R}  {GY}{ts}{R}  [{backend}]")
    method_color = GR if method == "snapshot" else YE
    print(f"  Elements: {WH}{n_total}{R}  "
          f"{YE}{n_enriched} enriched ✦{R}  "
          f"{RE}{n_unlabeled} unlabeled{R}  "
          f"{GY}~{est_tokens} tokens{R}  "
          f"{GY}read: {ms}ms{R}  "
          f"{method_color}[{method}]{R}")
    print(f"{B}{'═'*68}{R}")

    if args.format in ("visual", "all"):
        print(f"\n{B}  VISUAL VIEW{R}")
        vis = render_visual(tree)
        print(vis if vis.strip() else f"  {GY}(no visible elements){R}")

    flat = tokenizer.tokenize(tree, fmt="flat", max_elements=150)
    print_llm_section(flat)

    if args.format in ("nested", "all"):
        print(f"\n{B}{'─'*68}{R}")
        print(f"{B}  NESTED HIERARCHY{R}")
        print(f"{B}{'─'*68}{R}")
        nested = tokenizer.tokenize(tree, fmt="nested", max_elements=150)
        print(nested or f"  {GY}(empty){R}")

    unlabeled = tree.unlabeled()
    if unlabeled:
        print(f"\n{B}{'─'*68}{R}")
        print(f"{B}  UNLABELED{R} {GY}(would go to VLM){R}")
        print(f"{B}{'─'*68}{R}")
        for el in unlabeled[:8]:
            coord = (f"@({el.frame.center_x:.0f},{el.frame.center_y:.0f})"
                     if el.frame else "no frame")
            print(f"  {RE}? @{el.ref}{R} [{el.role.value}] "
                  f"raw='{el.raw_label or '(none)'}'  {GY}{coord}{R}")
        if len(unlabeled) > 8:
            print(f"  {GY}...{len(unlabeled)-8} more{R}")

    print(f"\n{B}{'═'*68}{R}")


# ─────────────────────────────────────────────────────────────────────────────
#  MODES
# ─────────────────────────────────────────────────────────────────────────────

async def run_interactive(reader: AXReader, args):
    print(f"\n{B}SIBB Screen Inspector — Interactive Mode{R}")
    print(f"Backend: {'XCUITest (full tree)' if reader._using_xctest else 'idb (partial)'}")
    print(f"\n{GY}Navigate the simulator to any screen.")
    print(f"Press  ENTER  to capture.  q + ENTER  to quit.{R}")
    while True:
        try:
            cmd = input(f"\n{B}capture (or q) > {R}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("q","quit","exit"): break
        await capture(reader, args)


async def run_watch(reader: AXReader, args, interval: float = 2.0):
    import os as _os
    print(f"\n{B}Watch Mode{R} — refreshing every {interval}s  Ctrl+C to stop")
    last_sig = None
    while True:
        try:
            tree = await reader.read()
            sig  = str(len(tree.elements)) + str([e.label for e in tree.elements[:5]])
            if sig != last_sig:
                _os.system("clear")
                await capture(reader, args)
                last_sig = sig
            await asyncio.sleep(interval)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"{RE}Error: {e}{R}")
            await asyncio.sleep(interval)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY
# ─────────────────────────────────────────────────────────────────────────────

def find_booted_udid() -> Optional[str]:
    import subprocess
    r = subprocess.run(["xcrun","simctl","list","devices","--json"],
                       capture_output=True, text=True)
    for devs in json.loads(r.stdout).get("devices",{}).values():
        for d in devs:
            if d.get("state") == "Booted":
                return d["udid"]
    return None


async def main_async(args):
    reader = AXReader(args.udid)
    await reader.start(bundle_id=args.bundle)

    try:
        if args.once:
            await capture(reader, args)
        elif args.watch:
            await run_watch(reader, args)
        else:
            await run_interactive(reader, args)
    finally:
        await reader.stop()


def main():
    parser = argparse.ArgumentParser(
        description="Inspect what the LLM sees from the current simulator screen."
    )
    parser.add_argument("udid", nargs="?", default=None)
    parser.add_argument("--bundle", default="com.apple.reminders",
                        help="Bundle ID of app to attach to")
    parser.add_argument("--format",
                        choices=["visual","flat","nested","all"],
                        default="visual")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--once",  action="store_true")
    args = parser.parse_args()

    args.udid = args.udid or find_booted_udid()
    if not args.udid:
        print(f"{RE}No booted simulator found.{R}")
        sys.exit(1)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
