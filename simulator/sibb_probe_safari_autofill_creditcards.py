"""Probe: feasibility check for Safari Credit Cards AutoFill task.

Investigation plan (matches the spec in the parent agent's request):

1. Snapshot Settings → Apps → Safari → AutoFill → Saved Credit Cards.
   Walk the AX tree, dump labels & coordinates, screenshot at each
   level. Confirm the UI surface is reachable from a pristine sim and
   what "no cards saved" looks like.

2. Drive Add Credit Card form by automated taps (cardholder name,
   number, expiry MM/YY), tap Done, then drop back into the keychain
   DB and look for what changed. We are not going to inspect
   encrypted blobs — we're looking for SHA-1-hashed plaintext markers
   in `acct`/`svce`/`labl` columns (the §13 trick).

3. Spin up a tiny HTML page with `<input autocomplete="cc-number">`
   (etc.), open in Safari, focus the cc-number input, observe whether
   iOS surfaces an "AutoFill Credit Card" suggestion bar above the
   keyboard. Screenshot + AX dump.

Outputs land in /tmp/sibb_cc_probe/ (screenshots + AX dumps).

Run as:
    python3 sibb/simulator/sibb_probe_safari_autofill_creditcards.py <UDID> [STEP]

STEP is one of:
    settings      - drive Settings → Safari → AutoFill, dump AX
    add_card      - drive Add Credit Card form, type test card, save
    inspect_kc    - dump keychain rows that changed (before vs after)
    serve_form    - serve cc form, open in Safari, dump AX with chip
    all           - run every step in order (default)
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Repo paths.
_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[2]
sys.path.insert(0, str(_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_ROOT / "sibb" / "simulator"))

OUT = Path("/tmp/sibb_cc_probe")
OUT.mkdir(parents=True, exist_ok=True)

# Test card values — we want plaintexts that won't collide with
# anything Apple ships, so the SHA-1 hash search is decisive.
CARD_HOLDER = "Sibbprobe Cardholder XYZQ"
CARD_NUMBER = "4111111111110042"  # 16-digit dummy (Luhn-valid base + tweak)
CARD_EXPIRY_MM = "12"
CARD_EXPIRY_YY = "29"
CARD_NICKNAME = "SIBBProbeCardNick"


def _keychain_db_path(udid: str) -> str:
    return os.path.expanduser(
        f"~/Library/Developer/CoreSimulator/Devices/{udid}"
        f"/data/Library/Keychains/keychain-2-debug.db"
    )


def _shot(udid: str, name: str) -> Path:
    p = OUT / f"{name}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", udid, "screenshot", str(p)],
        check=False, capture_output=True,
    )
    return p


def _dump_keychain_snapshot(udid: str, label: str) -> Path:
    """Snapshot ALL keychain rows (just metadata, no decrypted data)
    so we can diff before vs after the agent adds a card.
    """
    db = _keychain_db_path(udid)
    out = OUT / f"keychain_{label}.json"
    if not os.path.exists(db):
        out.write_text(json.dumps({"error": "no db"}))
        return out
    conn = sqlite3.connect(db, timeout=2.0)
    try:
        rows: Dict[str, List[Dict]] = {"genp": [], "inet": []}
        for table in ("genp", "inet"):
            cur = conn.execute(
                f"SELECT rowid, agrp, hex(svce), hex(acct), hex(labl), "
                f"length(data) FROM {table};"
                if table == "genp" else
                f"SELECT rowid, agrp, hex(srvr), hex(acct), hex(labl), "
                f"length(data) FROM {table};"
            )
            cols = ["rowid", "agrp", "svce_or_srvr", "acct", "labl", "datalen"]
            for r in cur.fetchall():
                rows[table].append(dict(zip(cols, r)))
        out.write_text(json.dumps(rows, indent=2))
    finally:
        conn.close()
    return out


def _scan_keychain_for_plaintext(udid: str, plaintext: str) -> List[Dict]:
    """SHA-1 hash plaintext and look for matches in acct/svce/srvr/labl
    columns across genp+inet. This is the §13 trick — Apple stores
    a SHA-1 of the plaintext as a lookup index alongside the AES blob.
    """
    h = hashlib.sha1(plaintext.encode("utf-8")).digest()
    h_hex = h.hex().upper()
    db = _keychain_db_path(udid)
    if not os.path.exists(db):
        return []
    hits: List[Dict] = []
    conn = sqlite3.connect(db, timeout=2.0)
    try:
        for table, cols in (
            ("genp", ("svce", "acct", "labl")),
            ("inet", ("srvr", "acct", "labl")),
        ):
            for col in cols:
                cur = conn.execute(
                    f"SELECT rowid, agrp, hex({col}) FROM {table} "
                    f"WHERE hex({col}) = ?;", (h_hex,)
                )
                for r in cur.fetchall():
                    hits.append({"table": table, "col": col, "rowid": r[0],
                                  "agrp": r[1], "hash_hex": r[2]})
    finally:
        conn.close()
    return hits


def _diff_keychain_snapshots(before: Path, after: Path) -> Dict:
    b = json.loads(before.read_text())
    a = json.loads(after.read_text())
    diff: Dict[str, Dict] = {}
    for table in ("genp", "inet"):
        bk = {r["rowid"]: r for r in b.get(table, [])}
        ak = {r["rowid"]: r for r in a.get(table, [])}
        added = [r for rid, r in ak.items() if rid not in bk]
        removed = [r for rid, r in bk.items() if rid not in ak]
        changed = []
        for rid, ar in ak.items():
            if rid in bk and bk[rid] != ar:
                changed.append({"before": bk[rid], "after": ar})
        diff[table] = {"added": added, "removed": removed,
                        "changed": changed,
                        "n_before": len(bk), "n_after": len(ak)}
    return diff


async def _dump_ax(reader, tag: str) -> Path:
    """Dump the full AX tree to a JSON file for later inspection."""
    snap = await reader.observe()
    out = OUT / f"ax_{tag}.json"
    elems = []
    for el in snap.elements:
        if el.frame is None:
            continue
        elems.append({
            "ref": el.ref, "role": el.role,
            "label": el.label, "value": el.value,
            "x": round(el.frame.x), "y": round(el.frame.y),
            "w": round(el.frame.width), "h": round(el.frame.height),
            "cx": round(el.frame.center_x),
            "cy": round(el.frame.center_y),
            "focused": getattr(el, "focused", None),
            "hittable": getattr(el, "hittable", None),
        })
    out.write_text(json.dumps({
        "n": len(elems),
        "keyboard_visible": snap.keyboard_visible,
        "keyboard_frame": getattr(snap, "keyboard_frame", None),
        "elements": elems,
    }, indent=2))
    return out


def _find_by_label(snap_elems: List[Dict], substr: str) -> List[Dict]:
    out = []
    s = substr.lower()
    for e in snap_elems:
        if (e.get("label") or "").lower().find(s) >= 0:
            out.append(e)
    return out


async def step_settings(reader, udid: str) -> Dict:
    """Step 1: navigate Settings → Apps → Safari → AutoFill →
    Saved Credit Cards. Dump AX + screenshot at each level."""

    findings: Dict = {"path": []}

    # Reset Settings to root.
    subprocess.run(["xcrun", "simctl", "terminate", udid,
                    "com.apple.Preferences"], capture_output=True)
    await asyncio.sleep(0.5)
    subprocess.run(["xcrun", "simctl", "launch", udid,
                    "com.apple.Preferences"], capture_output=True)
    await asyncio.sleep(2.0)

    _shot(udid, "01_settings_root")
    p = await _dump_ax(reader, "01_settings_root")
    findings["path"].append(str(p))

    # iOS 26 Settings: Apps page is a top-level entry. Scroll down,
    # find "Apps", tap, then find "Safari".
    snap = json.loads(p.read_text())
    apps_hits = _find_by_label(snap["elements"], "Apps")
    for scroll_n in range(4):
        if apps_hits:
            break
        await reader.swipe(direction="up")
        await asyncio.sleep(0.6)
        p = await _dump_ax(reader, f"01_settings_root_scroll{scroll_n}")
        snap = json.loads(p.read_text())
        apps_hits = _find_by_label(snap["elements"], "Apps")
    findings["apps_cell_found"] = bool(apps_hits)
    if apps_hits:
        findings["apps_cell"] = apps_hits[0]
        # Tap Apps cell.
        ah = apps_hits[0]
        await reader.tap(x=ah["cx"], y=ah["cy"])
        await asyncio.sleep(1.5)
        _shot(udid, "02_apps_list")
        p2 = await _dump_ax(reader, "02_apps_list")
        findings["path"].append(str(p2))

        snap2 = json.loads(p2.read_text())
        safari_hits = _find_by_label(snap2["elements"], "Safari")
        for sn in range(8):
            if safari_hits:
                break
            await reader.swipe(direction="up")
            await asyncio.sleep(0.5)
            p2 = await _dump_ax(reader, f"02_apps_list_scroll{sn}")
            snap2 = json.loads(p2.read_text())
            safari_hits = _find_by_label(snap2["elements"], "Safari")
        findings["safari_cell_found"] = bool(safari_hits)
        if safari_hits:
            sh = safari_hits[0]
            findings["safari_cell"] = sh
            await reader.tap(x=sh["cx"], y=sh["cy"])
            await asyncio.sleep(1.5)
            _shot(udid, "03_safari_settings")
            p3 = await _dump_ax(reader, "03_safari_settings")
            findings["path"].append(str(p3))

            # Find AutoFill — may need to scroll.
            snap3 = json.loads(p3.read_text())
            af_hits = _find_by_label(snap3["elements"], "AutoFill")
            findings["autofill_cell_found_first"] = bool(af_hits)
            for scroll_n in range(5):
                if af_hits:
                    break
                await reader.swipe(direction="up")
                await asyncio.sleep(0.6)
                p3 = await _dump_ax(reader, f"03_safari_settings_scroll{scroll_n}")
                snap3 = json.loads(p3.read_text())
                af_hits = _find_by_label(snap3["elements"], "AutoFill")
            findings["autofill_cell_found"] = bool(af_hits)
            if af_hits:
                fh = af_hits[0]
                findings["autofill_cell"] = fh
                await reader.tap(x=fh["cx"], y=fh["cy"])
                await asyncio.sleep(1.5)
                _shot(udid, "04_autofill_screen")
                p4 = await _dump_ax(reader, "04_autofill_screen")
                findings["path"].append(str(p4))

                snap4 = json.loads(p4.read_text())
                # Look for Saved Credit Cards / Credit Cards / Cards.
                cc_hits = (
                    _find_by_label(snap4["elements"], "Credit Card")
                    or _find_by_label(snap4["elements"], "Cards")
                )
                findings["credit_cards_cell_found"] = bool(cc_hits)
                findings["credit_cards_cell"] = cc_hits[0] if cc_hits else None
                if cc_hits:
                    ch = cc_hits[0]
                    await reader.tap(x=ch["cx"], y=ch["cy"])
                    await asyncio.sleep(1.5)
                    _shot(udid, "05_saved_credit_cards")
                    p5 = await _dump_ax(reader, "05_saved_credit_cards")
                    findings["path"].append(str(p5))
                    snap5 = json.loads(p5.read_text())
                    findings["saved_cards_screen_labels"] = [
                        e["label"] for e in snap5["elements"]
                        if e.get("label")
                    ][:40]
                    add_hits = _find_by_label(snap5["elements"], "Add")
                    findings["add_button_found"] = bool(add_hits)
                    findings["add_button"] = add_hits[0] if add_hits else None
    return findings


async def step_add_card(reader, udid: str) -> Dict:
    """Step 2: tap Add Credit Card, type values, tap Done."""
    findings: Dict = {}

    # Take a keychain snapshot BEFORE doing anything.
    before = _dump_keychain_snapshot(udid, "before_add_card")
    findings["keychain_before"] = str(before)

    # We assume we're on the Saved Credit Cards page from step 1.
    # Try to find Add Card button.
    p = await _dump_ax(reader, "06_before_add")
    snap = json.loads(p.read_text())
    add_hits = _find_by_label(snap["elements"], "Add")
    if not add_hits:
        findings["error"] = "no Add button found on saved cards screen"
        return findings
    a = add_hits[0]
    await reader.tap(x=a["cx"], y=a["cy"])
    await asyncio.sleep(2.0)
    _shot(udid, "07_add_card_form")
    p2 = await _dump_ax(reader, "07_add_card_form")
    findings["add_form_path"] = str(p2)
    snap2 = json.loads(p2.read_text())
    findings["add_form_labels"] = [e["label"] for e in snap2["elements"]
                                     if e.get("label")][:40]

    # Look for typical form fields.
    # iOS Safari AutoFill Add Card has: Cardholder Name, Card Number,
    # Expiration (combined MM/YY), Description (optional nickname).
    def find_input(needle: str) -> Optional[Dict]:
        for el in snap2["elements"]:
            lbl = (el.get("label") or "").lower()
            if needle.lower() in lbl and el.get("role") in (
                "TEXT_FIELD", "TEXTFIELD", "TextField", "textField",
                "[input]", "INPUT", "text field"
            ):
                return el
            if needle.lower() in lbl:
                return el
        return None

    name_field = find_input("Cardholder") or find_input("Name on Card") or find_input("Holder")
    number_field = find_input("Card Number") or find_input("Number")
    exp_field = find_input("Expiration") or find_input("MM/YY") or find_input("Expires")
    desc_field = find_input("Description") or find_input("Nickname")
    findings["fields"] = {
        "name": name_field,
        "number": number_field,
        "exp": exp_field,
        "desc": desc_field,
    }

    # Type into each field if found.
    async def fill(field: Optional[Dict], text: str) -> bool:
        if not field:
            return False
        await reader.tap_then_type(
            x=field["cx"], y=field["cy"], text=text)
        await asyncio.sleep(0.5)
        return True

    findings["typed_name"] = await fill(name_field, CARD_HOLDER)
    findings["typed_number"] = await fill(number_field, CARD_NUMBER)
    findings["typed_exp"] = await fill(
        exp_field, f"{CARD_EXPIRY_MM}/{CARD_EXPIRY_YY}")
    findings["typed_desc"] = await fill(desc_field, CARD_NICKNAME)

    _shot(udid, "08_form_filled")
    p3 = await _dump_ax(reader, "08_form_filled")
    findings["filled_form_path"] = str(p3)

    # Find Done / Save button.
    snap3 = json.loads(p3.read_text())
    done_hits = (
        _find_by_label(snap3["elements"], "Done")
        or _find_by_label(snap3["elements"], "Save")
    )
    findings["save_button_found"] = bool(done_hits)
    if done_hits:
        d = done_hits[0]
        await reader.tap(x=d["cx"], y=d["cy"])
        await asyncio.sleep(2.0)
    _shot(udid, "09_after_save")
    p4 = await _dump_ax(reader, "09_after_save")
    findings["after_save_path"] = str(p4)

    after = _dump_keychain_snapshot(udid, "after_add_card")
    findings["keychain_after"] = str(after)
    diff = _diff_keychain_snapshots(before, after)
    findings["keychain_diff_counts"] = {
        t: {"added": len(d["added"]),
            "removed": len(d["removed"]),
            "changed": len(d["changed"])}
        for t, d in diff.items()
    }
    (OUT / "keychain_diff.json").write_text(json.dumps(diff, indent=2))
    findings["keychain_diff_path"] = str(OUT / "keychain_diff.json")

    # Hash-search for our plaintext markers.
    for plaintext_field, plaintext in [
        ("CARD_HOLDER", CARD_HOLDER),
        ("CARD_NUMBER", CARD_NUMBER),
        ("CARD_NUMBER_LAST4", CARD_NUMBER[-4:]),
        ("CARD_NICKNAME", CARD_NICKNAME),
    ]:
        hits = _scan_keychain_for_plaintext(udid, plaintext)
        findings[f"hash_search_{plaintext_field}"] = hits

    return findings


# ─────────────────────────────────────────────────────────────────────
# Step 3: serve a tiny HTML form with autocomplete=cc-* and see if iOS
# Safari surfaces the AutoFill chip above the keyboard.
# ─────────────────────────────────────────────────────────────────────

CC_FORM_HTML = """<!doctype html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CC AutoFill Probe</title>
<style>
body { font: 18px -apple-system; padding: 16px; }
input { font-size: 18px; padding: 8px; margin: 6px 0;
        width: 100%; box-sizing: border-box; }
label { display: block; margin-top: 12px; }
</style>
</head><body>
<h2>Checkout</h2>
<form id="f" method="POST" action="/submit">
<label>Cardholder Name
  <input id="ccname" name="ccname" autocomplete="cc-name" type="text">
</label>
<label>Card Number
  <input id="ccnum" name="ccnum" autocomplete="cc-number" type="text" inputmode="numeric">
</label>
<label>Expiry (MM/YY)
  <input id="ccexp" name="ccexp" autocomplete="cc-exp" type="text" inputmode="numeric">
</label>
<label>CVC
  <input id="cccsc" name="cccsc" autocomplete="cc-csc" type="text" inputmode="numeric">
</label>
<button type="submit">Pay</button>
</form>
</body></html>
"""


class _CCHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(CC_FORM_HTML.encode("utf-8"))

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h1>OK</h1>")

    def log_message(self, *a, **k):  # silence stderr
        pass


def _start_local_server() -> Tuple[int, threading.Thread, socketserver.TCPServer]:
    srv = socketserver.TCPServer(("127.0.0.1", 0), _CCHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return port, t, srv


async def step_serve_form(reader, udid: str) -> Dict:
    findings: Dict = {}
    port, _, srv = _start_local_server()
    findings["port"] = port
    url = f"http://127.0.0.1:{port}/"
    findings["url"] = url

    # Open in Safari.
    subprocess.run(["xcrun", "simctl", "openurl", udid, url],
                    capture_output=True)
    await asyncio.sleep(3.0)
    _shot(udid, "10_form_loaded")
    p = await _dump_ax(reader, "10_form_loaded")
    findings["loaded_form_path"] = str(p)

    # Focus the cc-number field. Look for "Card Number" label.
    snap = json.loads(p.read_text())
    cc_hits = (_find_by_label(snap["elements"], "Card Number")
                or _find_by_label(snap["elements"], "ccnum"))
    findings["cc_number_field_found"] = bool(cc_hits)
    if cc_hits:
        c = cc_hits[0]
        findings["cc_number_field"] = c
        await reader.tap(x=c["cx"], y=c["cy"])
        await asyncio.sleep(1.5)  # keyboard + chip render time
        _shot(udid, "11_cc_number_focused")
        p2 = await _dump_ax(reader, "11_cc_number_focused")
        findings["focused_form_path"] = str(p2)
        snap2 = json.loads(p2.read_text())
        # Dump ALL labels above the keyboard (where the AutoFill chip
        # would appear).
        kbtop = (snap2.get("keyboard_frame") or {}).get("y")
        findings["keyboard_top"] = kbtop
        if kbtop:
            chip_zone = []
            for el in snap2["elements"]:
                if el["y"] < kbtop and el["y"] > kbtop - 80:
                    chip_zone.append(el)
            findings["above_kb_strip"] = chip_zone
        # Try to find AutoFill-related labels anywhere.
        for kw in ("AutoFill", "Credit Card", "Cards", "Pay",
                    "Saved Card", "Use Card"):
            hits = _find_by_label(snap2["elements"], kw)
            if hits:
                findings.setdefault("autofill_hints", {})[kw] = hits[:5]

    srv.shutdown()
    return findings


async def main(udid: str, step: str = "all") -> int:
    from sibb_xcuitest_client import XCUITestReader

    reader = XCUITestReader(udid, bundle_id="com.apple.Preferences")
    await reader.start()

    summary: Dict = {"udid": udid, "step": step, "out": str(OUT)}
    try:
        if step in ("all", "settings"):
            print("[probe] step 1: settings → autofill")
            summary["step1_settings"] = await step_settings(reader, udid)

        if step in ("all", "add_card"):
            print("[probe] step 2: add credit card")
            summary["step2_add_card"] = await step_add_card(reader, udid)

        if step in ("all", "inspect_kc"):
            print("[probe] step 3: keychain inspect")
            # Hash-search again (idempotent).
            res = {}
            for plaintext_field, plaintext in [
                ("CARD_HOLDER", CARD_HOLDER),
                ("CARD_NUMBER", CARD_NUMBER),
                ("CARD_NUMBER_LAST4", CARD_NUMBER[-4:]),
                ("CARD_NICKNAME", CARD_NICKNAME),
            ]:
                res[plaintext_field] = _scan_keychain_for_plaintext(
                    udid, plaintext)
            summary["step3_inspect_kc"] = res

        if step in ("all", "serve_form"):
            # Switch the reader to Safari for the form step.
            await reader.stop()
            reader = XCUITestReader(udid, bundle_id="com.apple.mobilesafari")
            await reader.start()
            print("[probe] step 4: serve form + watch for chip")
            summary["step4_serve_form"] = await step_serve_form(reader, udid)
    finally:
        try:
            await reader.stop()
        except Exception:
            pass

    out = OUT / "summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"[probe] DONE — summary at {out}")
    print(f"[probe] artifacts in {OUT}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: sibb_probe_safari_autofill_creditcards.py "
                "<UDID> [step]", file=sys.stderr)
        sys.exit(2)
    step = sys.argv[2] if len(sys.argv) >= 3 else "all"
    sys.exit(asyncio.run(main(sys.argv[1], step)))
