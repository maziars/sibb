"""
Record JSON round-trips between Python (XCUITestReader) and the
in-simulator Swift server, for use as L1.5 / L4 fixtures.

Invocation:
    python3 sibb/tests/scripts/record_socket_fixture.py <UDID> \\
        --scenario reminders_basic \\
        --out sibb/tests/fixtures/swift_socket/

Adds one scenario function per per-app handler. Each scenario calls
the new commands in a representative sequence; the script writes one
JSON file per command type containing every recorded (request,
response) pair. Commit those fixtures alongside the handler.

Per-command files (vs one big log): when Swift changes a response
shape — iOS column rename, added field, removed flag — the diff is
scoped to the affected command, not the whole log. The L4 contract
test re-records and `diff`s against the committed fixtures so
intentional changes get reviewed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Tuple

_SCRIPT_DIR = Path(__file__).resolve()
_SIBB_ROOT = _SCRIPT_DIR.parents[2]
for _p in (_SIBB_ROOT / "benchmark", _SIBB_ROOT / "simulator"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sibb_xcuitest_client import XCUITestReader  # noqa: E402


async def _scenario_reminders_basic(
    reader: XCUITestReader,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    records: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    cmds: List[Dict[str, Any]] = [
        {"type": "wipe_reminders"},
        {"type": "list_lists"},
        {"type": "create_list", "name": "TestList"},
        {"type": "create_reminder", "title": "Item1",
         "list": "TestList", "priority": "medium"},
        {"type": "create_reminder", "title": "Item2", "list": "TestList"},
        {"type": "list_lists"},
        {"type": "list_reminders", "list": "TestList"},
        {"type": "wipe_reminders"},
    ]
    for cmd in cmds:
        resp = await reader._send(cmd)
        records.append((cmd, resp))
    return records


SCENARIOS: Dict[str,
                Callable[[XCUITestReader],
                         Awaitable[List[Tuple[Dict[str, Any],
                                              Dict[str, Any]]]]]] = {
    "reminders_basic": _scenario_reminders_basic,
}


def _group_by_type(
    records: List[Tuple[Dict[str, Any], Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for cmd, resp in records:
        t = str(cmd.get("type", "unknown"))
        groups.setdefault(t, []).append({"request": cmd, "response": resp})
    return groups


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record Swift socket fixtures")
    parser.add_argument("udid", help="Simulator UDID")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS),
                        default="reminders_basic")
    parser.add_argument("--out", type=Path,
                        default=Path("sibb/tests/fixtures/swift_socket"))
    parser.add_argument("--bundle", default="com.apple.reminders")
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace existing fixture files instead of appending")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    reader = XCUITestReader(args.udid, bundle_id=args.bundle)
    await reader.start()
    try:
        records = await SCENARIOS[args.scenario](reader)
    finally:
        await reader.stop()

    for cmd_type, items in _group_by_type(records).items():
        path = args.out / f"{cmd_type}.json"
        if path.exists() and not args.overwrite:
            existing = json.loads(path.read_text())
            items = existing + items
        path.write_text(json.dumps(items, indent=2) + "\n")
        print(f"  wrote {len(items):>3} record(s) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
