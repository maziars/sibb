#!/usr/bin/env python3
"""
SIBB Native AX Reader
======================
Reads the iOS Simulator accessibility tree WITHOUT idb.
Uses a small Swift helper that talks to the macOS Accessibility API directly.

The Swift helper (sibb_ax_helper.swift) must be compiled once:
    swiftc sibb_ax_helper.swift -o sibb_ax_helper

Then this module uses it in place of idb in the scaffold.

Drop-in replacement for AXReader in sibb_scaffold.py — same interface,
same output format. Just swap the import.
"""

import subprocess, json, os, asyncio, hashlib, time
from typing import Optional, List

HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "sibb_ax_helper")
SWIFT_SRC   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "sibb_ax_helper.swift")


def compile_helper() -> bool:
    """Compile the Swift helper if binary doesn't exist or source is newer."""
    if not os.path.exists(SWIFT_SRC):
        print(f"ERROR: {SWIFT_SRC} not found")
        return False
    if (os.path.exists(HELPER_PATH) and
            os.path.getmtime(HELPER_PATH) > os.path.getmtime(SWIFT_SRC)):
        return True  # already compiled and up to date
    print("Compiling sibb_ax_helper.swift...")
    result = subprocess.run(
        ["swiftc", SWIFT_SRC, "-o", HELPER_PATH],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Compile error:\n{result.stderr}")
        return False
    print("Compiled successfully.")
    return True


def check_accessibility_permission() -> bool:
    """Check if Terminal/Python has Accessibility permission."""
    result = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to return name of first process'],
        capture_output=True, text=True
    )
    return result.returncode == 0


# ── Same interface as AXReader from sibb_scaffold.py ─────────────────────────

class NativeAXReader:
    """
    Reads the iOS Simulator AX tree using the native macOS Accessibility API
    via a compiled Swift helper. No idb required.

    Limitation: reads from the macOS AX layer, not the iOS AX layer.
    This means it sees the Simulator window and its iOS content as rendered
    on macOS — element structure may differ slightly from idb's output.
    For most SIBB benchmark purposes this is equivalent.
    """

    def __init__(self, udid: str):
        self.udid = udid
        self._ref_counter = 0
        self._cache: dict = {}

        # Import scaffold classes for compatibility
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from sibb_scaffold import (AXElement, AXFrame, AXTree,
                                       ElementRole, ROLE_MAP,
                                       SYSTEM_JUNK_LABELS)
            self._AXElement = AXElement
            self._AXFrame   = AXFrame
            self._AXTree    = AXTree
            self._ElementRole = ElementRole
            self._ROLE_MAP  = ROLE_MAP
            self._JUNK      = SYSTEM_JUNK_LABELS
        except ImportError as e:
            print(f"WARNING: Could not import sibb_scaffold: {e}")
            self._AXElement = None

    def _next_ref(self) -> str:
        self._ref_counter += 1
        return f"e{self._ref_counter:04d}"

    async def read(self, use_cache_ms: float = 0):
        """Read the current AX tree from the simulator."""
        raw_json = await self._fetch_raw()
        tree_hash = hashlib.md5(raw_json.encode()).hexdigest()

        if use_cache_ms > 0 and tree_hash in self._cache:
            cached_tree, cached_time = self._cache[tree_hash]
            if (time.time() * 1000 - cached_time) < use_cache_ms:
                return cached_tree

        try:
            raw_list = json.loads(raw_json)
        except json.JSONDecodeError:
            raw_list = []

        elements_flat = []
        for raw in raw_list:
            el = self._parse_element(raw, elements_flat)

        from sibb_scaffold import AXTree
        tree = AXTree(
            elements=elements_flat,
            root=elements_flat[0] if elements_flat else None,
            udid=self.udid,
        )
        self._cache[tree_hash] = (tree, time.time() * 1000)
        return tree

    async def _fetch_raw(self) -> str:
        """Run the Swift helper and return JSON string."""
        if not os.path.exists(HELPER_PATH):
            if not compile_helper():
                return "[]"

        proc = await asyncio.create_subprocess_exec(
            HELPER_PATH, self.udid,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            # Common error: accessibility permission not granted
            if "not permitted" in err.lower() or "permission" in err.lower():
                print("\nERROR: Accessibility permission required.")
                print("Go to: System Settings → Privacy & Security → Accessibility")
                print("Add Terminal (or your Python app) and enable it.")
            return "[]"
        return stdout.decode()

    def _parse_element(self, raw: dict, flat: list):
        """Parse one element from the Swift helper's JSON output."""
        from sibb_scaffold import (AXElement, AXFrame, ElementRole,
                                   ROLE_MAP, SYSTEM_JUNK_LABELS)

        raw_label = raw.get("AXLabel") or raw.get("AXTitle") or ""
        raw_role  = raw.get("AXRole", "Unknown")
        raw_value = raw.get("AXValue")
        raw_hint  = raw.get("AXHint")
        raw_frame = raw.get("AXFrame")

        label = raw_label.strip() if raw_label else None
        if label and label.strip().lower() in SYSTEM_JUNK_LABELS:
            label = None

        frame = None
        if raw_frame:
            frame = AXFrame(
                x=raw_frame.get("x", 0),
                y=raw_frame.get("y", 0),
                width=raw_frame.get("width", 0),
                height=raw_frame.get("height", 0),
            )

        el = AXElement(
            ref=self._next_ref(),
            label=label,
            raw_label=raw_label or None,
            role=ROLE_MAP.get(raw_role, ElementRole.UNKNOWN),
            value=str(raw_value) if raw_value is not None else None,
            hint=raw_hint,
            frame=frame,
            enabled=raw.get("AXEnabled", True),
            visible=True,
        )
        flat.append(el)
        return el
