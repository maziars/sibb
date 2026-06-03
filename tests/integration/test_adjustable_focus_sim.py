"""L2 sim test: focused text fields do NOT get `[adj]` role tag.

Regression coverage for the 2026-05-27 variant B failure mode where
the iOS Contacts new-contact sheet's First/Last Name input fields
flipped between `[input]` and `[adj]` based purely on which one held
keyboard focus. Root cause was Swift-side `snapshotAdjustable()`
returning true for any element whose AX trait bitmask had the
adjustable bit set — and iOS includes that bit on the focused
element for VoiceOver-rotor text-cursor positioning, even on plain
text inputs.

The Swift fix excludes focused plain text inputs from the KVC trait
probe. This test confirms the post-fix behavior in a real simulator:
open Contacts → New Contact, focus First name (which becomes
hasFocus=true), and assert the AX dump shows it as `[input]`, not
`[adj]`.

Run:
    SIBB_UDID=<udid> python3 -m pytest -m sim \
        sibb/tests/integration/test_adjustable_focus_sim.py -v

Skips if SIBB_UDID isn't set. First run takes ~30-60s for runner
build; subsequent module-scoped tests share one reader.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

from sibb_xcuitest_client import XCUITestReader  # noqa: E402


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[XCUITestReader]:
    r = XCUITestReader(sibb_udid)
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


async def _open_contacts_new_contact_sheet(reader: XCUITestReader) -> None:
    """Reset Contacts, navigate to the New Contact sheet via the
    Contacts.app + button. Tap the First name field to focus it."""
    import subprocess as _sp
    udid = reader.udid
    _sp.run(["xcrun", "simctl", "terminate", udid,
             "com.apple.MobileAddressBook"], capture_output=True)
    await asyncio.sleep(1.0)
    await reader.launch(bundle_id="com.apple.MobileAddressBook")
    await asyncio.sleep(2.5)
    # Dismiss any onboarding popup.
    for _ in range(3):
        raw = await reader._send({"type": "observe",
                                    "bundleId": "com.apple.MobileAddressBook"})
        els = raw.get("elements") or []
        dismissed = False
        for e in els:
            lbl = (e.get("label") or "").strip().lower()
            if e.get("role") == "btn" and lbl in (
                    "continue", "not now", "ok", "skip", "done"):
                fr = e.get("frame") or {}
                await reader.tap(x=fr.get("x", 0) + fr.get("width", 0) / 2,
                                  y=fr.get("y", 0) + fr.get("height", 0) / 2)
                await asyncio.sleep(0.7)
                dismissed = True
                break
        if not dismissed:
            break
    # Tap the "+" button to open New Contact sheet.
    raw = await reader._send({"type": "observe",
                                "bundleId": "com.apple.MobileAddressBook"})
    els = raw.get("elements") or []
    add_btn = next((e for e in els if e.get("role") == "btn"
                    and (e.get("label") or "").lower() in ("add", "+")),
                   None)
    if add_btn is None:
        pytest.skip("Could not locate Contacts '+' button — UI variant")
    fr = add_btn["frame"]
    await reader.tap(x=fr["x"] + fr["width"] / 2,
                     y=fr["y"] + fr["height"] / 2)
    await asyncio.sleep(1.5)


def _find_field_by_label(els, label_substr, accept_adj=True):
    """Find an input element whose label contains label_substr. When
    `accept_adj=True`, also accepts the (buggy pre-fix) `adj` role —
    needed for navigation/setup paths so the test still works against
    an UNFIXED build. When `accept_adj=False`, requires the post-fix
    `input` role — used by assertions that pin the post-fix contract."""
    label_substr_lower = label_substr.lower()
    allowed = ("input", "adj") if accept_adj else ("input",)
    for e in els:
        lbl = (e.get("label") or "").lower()
        role = e.get("role", "")
        if label_substr_lower in lbl and role in allowed:
            return e
    return None


async def _poll_for_focused_at(reader, tap_x, tap_y,
                                  bundle, timeout_s=1.5,
                                  poll_interval_s=0.12):
    """Mirror the production `_wait_for_focus_at` — poll until a
    focused element's frame contains the tapped point, OR timeout."""
    import time as _t
    deadline = _t.monotonic() + timeout_s
    while _t.monotonic() < deadline:
        await asyncio.sleep(poll_interval_s)
        raw = await reader._send({"type": "observe",
                                    "bundleId": bundle})
        for e in raw.get("elements") or []:
            if not e.get("focused"):
                continue
            f = e.get("frame") or {}
            if (f.get("x", 0) <= tap_x <= f.get("x", 0) + f.get("width", 0)
                    and f.get("y", 0) <= tap_y <= f.get("y", 0) + f.get("height", 0)):
                return True
    return False


def _typed_text_in_field(field):
    """iOS renders typed text in either the AX `value` slot or
    appended to the `label` slot, depending on the iOS version + the
    specific control. Return the typed-text candidate from either
    source as a single string so the test can substring-check it."""
    return (field.get("value") or "") + " " + (field.get("label") or "")


async def test_focused_first_name_field_is_input_not_adj(reader):
    """After tapping First name, the AX dump should show it as
    `[input]` (or, equivalently, role='input' with adjustable=False).
    Pre-fix it would have appeared as `[adj]` / adjustable=True.

    This test is forgiving on the wider UI (multiple ways iOS might
    render the new-contact sheet); the load-bearing assertion is on
    the specific element's adjustable flag."""
    bundle = "com.apple.MobileAddressBook"
    await _open_contacts_new_contact_sheet(reader)
    # Setup phase: locate First name. Setup paths accept the pre-fix
    # `adj` role too so this test can run against an unfixed build —
    # it'll still catch the regression at the final assertion.
    raw = await reader._send({"type": "observe", "bundleId": bundle})
    els = raw.get("elements") or []
    fn_field = _find_field_by_label(els, "first name", accept_adj=True)
    if fn_field is None:
        pytest.skip("New-contact sheet didn't expose 'First name' "
                    "(UI variant) — re-evaluate test scenario")
    fr = fn_field["frame"]
    tap_x = fr["x"] + fr["width"] / 2
    tap_y = fr["y"] + fr["height"] / 2
    await reader.tap(x=tap_x, y=tap_y)
    # Wait for focus to actually transfer to First name before
    # observing — without this poll, a fast observe() can read the
    # AX state BEFORE iOS' responder chain finishes the focus
    # handoff, masking the bug we're trying to catch.
    focused_ok = await _poll_for_focused_at(reader, tap_x, tap_y, bundle)
    if not focused_ok:
        pytest.skip(
            "Focus didn't transfer to First name within 1.5s — "
            "test can't validate the bug without a confirmed focused "
            "state. Either iOS is slow or the AX surface doesn't "
            "expose `focused` here.")
    # Re-observe; find First name by its post-tap state.
    raw = await reader._send({"type": "observe", "bundleId": bundle})
    els = raw.get("elements") or []
    fn_focused = _find_field_by_label(els, "first name", accept_adj=True)
    assert fn_focused is not None, (
        "first name field disappeared after focus")
    assert fn_focused.get("focused") is True, (
        f"poll claimed focus settled but field reports focused="
        f"{fn_focused.get('focused')!r}")
    # Load-bearing assertion: a focused plain text input must NOT
    # have adjustable=True. Pre-fix: True (false positive from the
    # KVC trait probe). Post-fix: False (excluded for focused plain
    # text inputs).
    assert fn_focused.get("adjustable") is not True, (
        f"FOCUSED First name field reported adjustable=True — Swift "
        f"`snapshotAdjustable()` is over-flagging again. "
        f"Field state: {fn_focused!r}")
    # And — equivalently — its role should NOT be `adj` post-fix.
    assert fn_focused.get("role") != "adj", (
        f"FOCUSED First name role='adj' — Swift fix not effective. "
        f"Field state: {fn_focused!r}")


async def test_type_after_tap_lands_in_target_field(reader):
    """Bug B regression: TYPE after TAP focus race. Tap First name,
    type 'SibbA'; tap Last name (with wait-for-focus), type 'SibbB'.
    The AX dump should show 'SibbA' under First name and 'SibbB'
    under Last name, with NO leakage either direction."""
    bundle = "com.apple.MobileAddressBook"
    await _open_contacts_new_contact_sheet(reader)
    raw = await reader._send({"type": "observe", "bundleId": bundle})
    els = raw.get("elements") or []
    fn_field = _find_field_by_label(els, "first name", accept_adj=True)
    ln_field = _find_field_by_label(els, "last name", accept_adj=True)
    if fn_field is None or ln_field is None:
        pytest.skip("New-contact sheet missing First/Last name "
                    "fields (UI variant)")
    # Focus First name and type — production code uses
    # _wait_for_focus_at, so the test exercises the same path.
    fr = fn_field["frame"]
    fn_tap_x = fr["x"] + fr["width"] / 2
    fn_tap_y = fr["y"] + fr["height"] / 2
    await reader.tap(x=fn_tap_x, y=fn_tap_y)
    await _poll_for_focused_at(reader, fn_tap_x, fn_tap_y, bundle)
    await reader.type_text("SibbA")
    await asyncio.sleep(0.5)
    # Now tap Last name with the same focus-wait + type.
    raw = await reader._send({"type": "observe", "bundleId": bundle})
    els = raw.get("elements") or []
    ln_field = _find_field_by_label(els, "last name", accept_adj=True)
    if ln_field is None:
        pytest.skip("Last name field disappeared mid-test")
    fr = ln_field["frame"]
    ln_tap_x = fr["x"] + fr["width"] / 2
    ln_tap_y = fr["y"] + fr["height"] / 2
    await reader.tap(x=ln_tap_x, y=ln_tap_y)
    await _poll_for_focused_at(reader, ln_tap_x, ln_tap_y, bundle)
    await reader.type_text("SibbB")
    await asyncio.sleep(0.6)
    # Verify final state. iOS renders typed text in different AX
    # slots depending on version — check value+label as one bag.
    raw = await reader._send({"type": "observe", "bundleId": bundle})
    els = raw.get("elements") or []
    fn = _find_field_by_label(els, "first name", accept_adj=True)
    ln = _find_field_by_label(els, "last name", accept_adj=True)
    assert fn is not None and ln is not None, (
        "Lost First/Last name fields after typing")
    fn_text = _typed_text_in_field(fn)
    ln_text = _typed_text_in_field(ln)
    assert "SibbA" in fn_text, (
        f"First name should contain 'SibbA' (the first type call); "
        f"got value+label={fn_text!r}")
    assert "SibbB" in ln_text, (
        f"Last name should contain 'SibbB' (the second type call); "
        f"got value+label={ln_text!r}")
    # Load-bearing: SibbB must NOT be in First name's text. That's
    # the leak bug variant B trial exposed.
    assert "SibbB" not in fn_text, (
        f"TYPE-AFTER-TAP LEAK: text intended for Last name landed in "
        f"First name. First name text bag: {fn_text!r}. This is the "
        f"bug that broke variant B trials on 2026-05-27.")
    # And the reverse, for symmetry — SibbA shouldn't leak forward
    # into Last name either (would indicate a different focus bug).
    assert "SibbA" not in ln_text, (
        f"Reverse-direction TYPE leak: SibbA landed in Last name. "
        f"Last name text bag: {ln_text!r}")
