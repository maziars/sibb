#!/usr/bin/env python3
"""Probe v2: Messages.app sent-message lifetime — careful version.

Lessons from v1:
- The JA chat cell, in iOS 26 sim Messages, is a CONTACT SUGGESTION.
  Tapping it once opens a fresh compose view (thread mode), where we
  send. After leaving and coming back, tapping the inbox row again
  goes through the same path — IF the inbox row still represents an
  ongoing conversation (it has a recent preview/date), it opens the
  thread. If the cell reverted to "12/31/00", tapping opens a
  fresh compose (no history).
- At check time we need a known navigation that lands in the THREAD
  view, with the bubble in it (if it survived). Strategy:
  (1) Always START at the springboard (press home).
  (2) Launch Messages.app via simctl (re-foregrounds; iOS restores
      its last view).
  (3) Observe. If we landed in a thread view, look for the bubble
      directly. If we landed at the inbox, look for the cell preview.
  (4) Tap the JA cell, observe again, look for bubble in thread view.

This avoids accidentally navigating to contact details.
"""
from __future__ import annotations
import asyncio, json, os, subprocess, sys, time

# Path-resolve relative to this file so the probe is portable across checkouts.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sibb_xcuitest_client import XCUITestReader  # noqa

UDID = os.environ.get("SIBB_UDID") or sys.exit(
    "SIBB_UDID env var required (your iOS simulator UDID; `xcrun simctl list devices`)")
MESSAGES = "com.apple.MobileSMS"
OUT = os.environ["OUT"]
os.makedirs(OUT, exist_ok=True)

REPORT = []


def shell(cmd, timeout=20):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def shot(name):
    p = os.path.join(OUT, name)
    shell(f"xcrun simctl io {UDID} screenshot '{p}'", timeout=10)
    return p


def sim_terminate(bundle):
    shell(f"xcrun simctl terminate {UDID} {bundle}")


def sim_launch(bundle):
    shell(f"xcrun simctl launch {UDID} {bundle}")


def log(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()
    with open(os.path.join(OUT, "v2.log"), "a") as f:
        f.write(msg + "\n")


async def observe_raw(reader, bundle=None):
    cmd = {"type": "observe"}
    if bundle:
        cmd["bundleId"] = bundle
    async with reader._lock:
        raw = await reader._send(cmd)
    return raw


def dump_ax(els, path, header=""):
    with open(path, "w") as f:
        if header:
            f.write(f"# {header}\n")
        for e in els:
            f.write(f"{e.get('role','?'):10s} label={(e.get('label') or '')!r:50s} value={(e.get('value') or '')!r:30s} frame={e.get('frame')}\n")


def find_label_substr(els, substr):
    """Return list of elements whose label contains substr."""
    return [e for e in els if substr in (e.get("label") or "")]


def find_role(els, role):
    return [e for e in els if e.get("role") == role]


async def tap_el(reader, el):
    fr = el.get("frame") or {}
    cx = fr.get("x", 0) + fr.get("width", 0) / 2
    cy = fr.get("y", 0) + fr.get("height", 0) / 2
    await reader.tap(x=cx, y=cy)


# ─────────────────────────────────────────────────────────────────────────


async def view_kind(els):
    """Identify which Messages view we landed on.
    Returns: 'inbox' | 'thread' | 'contact_details' | 'springboard' | 'other'
    """
    has_compose_input = any(e.get("role") == "input" and (e.get("label") or "") == "Message"
                            for e in els)
    has_imessage_text = any((e.get("label") or "") == "iMessage" and e.get("role") == "text"
                            for e in els)
    has_conversations = any((e.get("label") or "") == "Conversations"
                            and e.get("role") == "collection" for e in els)
    has_create_contact = any((e.get("label") or "") == "Create New Contact" for e in els)
    has_back_btn_messages = any((e.get("label") or "") == "Messages" and e.get("role") == "btn"
                                 for e in els)
    if has_create_contact and not has_compose_input:
        return "contact_details"
    if has_compose_input or has_imessage_text:
        return "thread"
    if has_conversations:
        return "inbox"
    if any((e.get("label") or "").lower() in ("home screen") for e in els):
        return "springboard"
    return "other"


async def navigate_back_to_inbox(reader, max_steps=3):
    """Tap top-left back chevron repeatedly until we're at the inbox."""
    for _ in range(max_steps):
        raw = await observe_raw(reader)
        els = raw.get("elements") or []
        vk = await view_kind(els)
        if vk == "inbox":
            return True
        # Find back button (label 'Messages' or 'Back')
        back = None
        for e in els:
            if e.get("role") == "btn" and (e.get("label") or "") in ("Messages", "Back"):
                back = e
                break
        if not back:
            return False
        await tap_el(reader, back)
        await asyncio.sleep(0.6)
    return False


async def open_ja_thread(reader):
    """From the inbox, tap the JA cell (the row, not the title bar btn).
    A 'cell' role element with label containing '888' is the inbox row.
    """
    raw = await observe_raw(reader)
    els = raw.get("elements") or []
    # The inbox cell is role=='cell' with label like '+1 (888) 555-1212, 12/31/00 '
    # or after our send, '+1 (888) 555-1212, ..., 8:48 PM, OurMarker'.
    for e in els:
        if e.get("role") == "cell" and "888" in (e.get("label") or ""):
            await tap_el(reader, e)
            await asyncio.sleep(1.0)
            return True
    return False


async def send_in_thread(reader, marker):
    """We are at a thread/compose view. Tap the Message input, type marker, send."""
    raw = await observe_raw(reader)
    els = raw.get("elements") or []
    compose = None
    for e in els:
        if e.get("role") == "input" and (e.get("label") or "") == "Message":
            compose = e
            break
    if not compose:
        return False, "no compose input"
    await tap_el(reader, compose)
    await asyncio.sleep(0.7)
    await reader.type_text(marker)
    await asyncio.sleep(0.5)
    # Find SendButton (it appears after typing). It has role='btn' label='SendButton'
    # on some iOS versions; otherwise an unlabeled btn near right of compose.
    raw = await observe_raw(reader)
    els = raw.get("elements") or []
    send_btn = None
    for e in els:
        if e.get("role") == "btn" and (e.get("label") or "") in ("SendButton", "Send"):
            send_btn = e
            break
    if not send_btn:
        # find unlabeled btn near (367, 499) — typical send-arrow position
        best = None
        best_d = 1e9
        for e in els:
            if e.get("role") == "btn" and not (e.get("label") or ""):
                fr = e.get("frame") or {}
                cx = fr.get("x", 0) + fr.get("width", 0) / 2
                cy = fr.get("y", 0) + fr.get("height", 0) / 2
                if cx < 300 or cy < 400 or cy > 600:
                    continue
                d = (cx - 367) ** 2 + (cy - 499) ** 2
                if d < best_d:
                    best_d = d
                    best = e
        send_btn = best
    if not send_btn:
        return False, "no send button"
    await tap_el(reader, send_btn)
    await asyncio.sleep(0.8)
    return True, "ok"


async def reset_messages(reader):
    """Terminate Messages, relaunch, dismiss popups, go to inbox."""
    sim_terminate(MESSAGES)
    await asyncio.sleep(1.5)
    sim_launch(MESSAGES)
    await asyncio.sleep(2.5)
    # Dismiss any TCC / welcome — try Continue / Not Now / OK.
    for _ in range(3):
        raw = await observe_raw(reader)
        els = raw.get("elements") or []
        dismissed = False
        for e in els:
            lbl = (e.get("label") or "").strip().lower()
            if e.get("role") == "btn" and lbl in ("continue", "not now", "ok", "skip", "done"):
                await tap_el(reader, e)
                await asyncio.sleep(0.7)
                dismissed = True
                break
        if not dismissed:
            break
    await navigate_back_to_inbox(reader)


async def send_marker(reader, marker):
    """From inbox, open JA, send marker. Returns ok+err."""
    if not await open_ja_thread(reader):
        return False, "couldn't open JA cell"
    ok, err = await send_in_thread(reader, marker)
    return ok, err


async def check_for_marker(reader, marker, tag, scenario_left_us_at):
    """At check time, return whether marker is visible.
    scenario_left_us_at: 'foreground_messages' | 'backgrounded_home' |
                        'backgrounded_other_app' | 'terminated' |
                        'app_switcher' | 'unknown'

    Strategy: launch Messages (re-foreground if needed; for terminated,
    this is a true launch — but we already relaunched in the scenario).
    Then check:
    A) The JA inbox cell label — if it contains our marker, message
       survived (preview text).
    B) Navigate into thread, check for bubble.

    Returns dict.
    """
    # Re-foreground Messages (no-op if already foreground)
    sim_launch(MESSAGES)
    await asyncio.sleep(1.5)
    # Save screenshot of where we land
    shot(f"{tag}_landed.png")
    raw = await observe_raw(reader)
    els = raw.get("elements") or []
    dump_ax(els, os.path.join(OUT, f"ax_{tag}_landed.txt"), header=f"landed bundle={raw.get('bundleId')}")
    vk = await view_kind(els)

    # If we're in a thread (typical when iOS restores last view), check bubble.
    bubble_in_thread = False
    inbox_cell_label = None
    inbox_has_marker = False
    if vk == "thread":
        # Look for cell with label containing marker
        bubble_in_thread = bool(find_label_substr(els, marker))
        # Navigate back to inbox to check inbox cell
        await navigate_back_to_inbox(reader)
        await asyncio.sleep(0.6)
        shot(f"{tag}_inbox.png")
        raw = await observe_raw(reader)
        els2 = raw.get("elements") or []
        dump_ax(els2, os.path.join(OUT, f"ax_{tag}_inbox.txt"), header="inbox")
        # Find JA inbox cell
        for e in els2:
            if e.get("role") == "cell" and "888" in (e.get("label") or ""):
                inbox_cell_label = e.get("label")
                break
        inbox_has_marker = bool(inbox_cell_label and marker in inbox_cell_label)
    elif vk == "inbox":
        for e in els:
            if e.get("role") == "cell" and "888" in (e.get("label") or ""):
                inbox_cell_label = e.get("label")
                break
        inbox_has_marker = bool(inbox_cell_label and marker in inbox_cell_label)
        # Try to enter the thread and check for bubble
        if await open_ja_thread(reader):
            await asyncio.sleep(0.6)
            shot(f"{tag}_thread.png")
            raw = await observe_raw(reader)
            els3 = raw.get("elements") or []
            dump_ax(els3, os.path.join(OUT, f"ax_{tag}_thread.txt"), header="thread")
            vk3 = await view_kind(els3)
            if vk3 == "thread":
                bubble_in_thread = bool(find_label_substr(els3, marker))
    else:
        log(f"  [warn] view_kind={vk}, can't check reliably")

    return {
        "tag": tag,
        "view_on_landing": vk,
        "bubble_in_thread": bubble_in_thread,
        "inbox_cell_label": inbox_cell_label,
        "inbox_has_marker": inbox_has_marker,
    }


# ─── Scenarios ─────────────────────────────────────────────────────────


async def do_scenario(reader, name, prepare_fn, after_send_fn, checks):
    """checks: list of (tag, t_offset_seconds_after_send, scenario_left_us_at)"""
    log(f"\n=== {name} ===")
    await reset_messages(reader)
    await prepare_fn(reader) if prepare_fn else None
    marker = f"{name}_T{int(time.time())}"
    sent_t = time.time()
    ok, err = await send_marker(reader, marker)
    log(f"  send: ok={ok} err={err} marker={marker}")
    if not ok:
        REPORT.append({"scenario": name, "marker": marker, "send_ok": False, "err": err})
        return
    shot(f"{name}_after_send.png")
    # Snapshot the immediate thread state to confirm bubble was visible.
    raw = await observe_raw(reader)
    els = raw.get("elements") or []
    dump_ax(els, os.path.join(OUT, f"ax_{name}_immediate.txt"), header="immediate after send")
    immediate_visible = bool(find_label_substr(els, marker))
    log(f"  immediate post-send: marker_visible_in_AX={immediate_visible}")

    # Perform scenario.
    after_left = "foreground_messages"
    if after_send_fn:
        after_left = await after_send_fn(reader) or after_left

    results = [{"tag": "immediate", "bubble_in_thread": immediate_visible,
                "view_on_landing": "thread", "inbox_has_marker": None,
                "inbox_cell_label": None}]
    for tag, t_off, scn_left in checks:
        target = sent_t + t_off
        while time.time() < target:
            await asyncio.sleep(min(5.0, target - time.time()))
        r = await check_for_marker(reader, marker, f"{name}_{tag}", scn_left)
        log(f"  [{tag} @T+{t_off}s] view={r['view_on_landing']} "
            f"thread_bubble={r['bubble_in_thread']} "
            f"inbox_has_marker={r['inbox_has_marker']} "
            f"inbox_cell={r['inbox_cell_label']!r}")
        results.append(r)
    REPORT.append({"scenario": name, "marker": marker, "send_ok": True, "results": results})


# Scenario after_send_fns
async def asn_none(reader):
    return "foreground_messages"


async def asn_press_home(reader):
    await reader.press("home")
    await asyncio.sleep(1.0)
    return "backgrounded_home"


async def asn_launch_contacts(reader):
    sim_launch("com.apple.MobileAddressBook")
    await asyncio.sleep(2.0)
    return "backgrounded_other_app"


async def asn_multi_app(reader):
    for b in ["com.apple.MobileAddressBook", "com.apple.mobilecal",
              "com.apple.Preferences", "com.apple.reminders"]:
        sim_launch(b)
        await asyncio.sleep(10)
    return "backgrounded_other_app"


async def asn_terminate(reader):
    sim_terminate(MESSAGES)
    await asyncio.sleep(5)
    return "terminated"


async def asn_app_switcher_close(reader):
    try:
        await reader.press("app_switcher")
        await asyncio.sleep(1.5)
        # Try to swipe up on the centered Messages card
        await reader.swipe_at(200, 437, 200, 100, duration_s=0.15)
        await asyncio.sleep(1.5)
        # Tap empty area at top to exit app switcher
        await reader.tap(x=200, y=50)
        await asyncio.sleep(1.0)
        return "app_switcher"
    except Exception as ex:
        log(f"  app-switcher gesture exception: {ex}")
        return "app_switcher"


async def main():
    reader = XCUITestReader(UDID, bundle_id=MESSAGES)
    log(f"Starting XCUITest reader against {UDID}")
    await reader.start()
    log("Reader started.")

    try:
        # S0: baseline
        await do_scenario(reader, "S0", None, asn_none,
                          [("immediate", 2, "foreground_messages")])
        # S1: foreground idle
        await do_scenario(reader, "S1", None, asn_none, [
            ("T10", 10, "foreground_messages"),
            ("T30", 30, "foreground_messages"),
            ("T60", 60, "foreground_messages"),
            ("T120", 120, "foreground_messages"),
        ])
        # S2: background via home
        await do_scenario(reader, "S2", None, asn_press_home, [
            ("T10", 10, "backgrounded_home"),
            ("T30", 30, "backgrounded_home"),
            ("T60", 60, "backgrounded_home"),
            ("T120", 120, "backgrounded_home"),
        ])
        # S3: background to Contacts
        await do_scenario(reader, "S3", None, asn_launch_contacts, [
            ("T10", 10, "backgrounded_other_app"),
            ("T30", 30, "backgrounded_other_app"),
            ("T60", 60, "backgrounded_other_app"),
            ("T120", 120, "backgrounded_other_app"),
        ])
        # S4: multi-app cycle 4x10s
        await do_scenario(reader, "S4", None, asn_multi_app, [
            ("T45", 45, "backgrounded_other_app"),
        ])
        # S5: terminate Messages
        await do_scenario(reader, "S5", None, asn_terminate, [
            ("T8", 8, "terminated"),
        ])
        # S6: app-switcher swipe close
        await do_scenario(reader, "S6", None, asn_app_switcher_close, [
            ("T15", 15, "app_switcher"),
        ])
        # S7: long foreground idle 300s
        await do_scenario(reader, "S7", None, asn_none, [
            ("T180", 180, "foreground_messages"),
            ("T300", 300, "foreground_messages"),
        ])
    finally:
        with open(os.path.join(OUT, "report_v2.json"), "w") as f:
            json.dump(REPORT, f, indent=2)
        log(f"\nReport saved to {os.path.join(OUT, 'report_v2.json')}")


if __name__ == "__main__":
    asyncio.run(main())
