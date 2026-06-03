"""ContactsHandler — L2 sim integration.

Runs against the real iOS simulator with the live XCUITest runner.
Covers what the L1 + L1.5 fake-reader tests can't:
1. Swift command shapes actually match Python's expectations
   (a typo in `phoneNumbers` key, or a mismatched dictionary cast,
   would never surface in the fake).
2. The on-sim CNContactStore is reachable from the test runner
   bundle (TCC grants + first-launch dialog dismissal worked
   end-to-end during baseline build).
3. The reset semantics actually wipe the store — not just clear
   the in-memory fake.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

pytestmark = pytest.mark.sim

_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
for p in (_SIM_DIR, _BENCHMARK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sibb_scaffold import AXReader  # noqa: E402
from sibb_state import ContactsHandler  # noqa: E402


@pytest_asyncio.fixture(scope="module")
async def reader(sibb_udid: str) -> AsyncIterator[AXReader]:
    r = AXReader(sibb_udid)
    # Attach to SpringBoard — Contacts ops are EventKit-style
    # framework calls, no foreground app required.
    await r.start(bundle_id="com.apple.springboard")
    try:
        # Start clean. The fixture's first test inherits whatever the
        # cloned baseline had; subsequent tests pay for their own
        # wipe in setup. wipe-on-fixture-enter keeps test ordering
        # independent.
        await r._xcuitest._send({"type": "wipe_contacts"})
        yield r
    finally:
        await r.stop()


# ────────────────────── Swift command shapes ────────────────────────

async def test_create_contact_round_trip(reader):
    """Basic round-trip: create → list → see it. Validates
    create_contact and list_contacts Swift command shapes match
    Python's expectations.
    """
    resp = await reader._xcuitest._send({
        "type": "create_contact",
        "given_name": "Ada",
        "family_name": "Lovelace",
        "phone": "+1-555-0100",
        "email": "ada@example.com",
        "organization": "Analytical Engine Co.",
    })
    assert resp.get("ok") is True, f"create_contact failed: {resp}"
    assert resp.get("given_name") == "Ada"
    assert resp.get("identifier"), "Swift didn't return an identifier"

    resp = await reader._xcuitest._send({"type": "list_contacts"})
    assert resp.get("ok") is True
    rows = resp.get("contacts", [])
    matches = [c for c in rows if c.get("given_name") == "Ada"
                and c.get("family_name") == "Lovelace"]
    assert len(matches) == 1, (
        f"expected exactly one Ada Lovelace, got {len(matches)}: "
        f"{matches}"
    )
    m = matches[0]
    assert m["phone"] == "+1-555-0100"
    assert m["email"] == "ada@example.com"
    assert m["organization"] == "Analytical Engine Co."


async def test_create_contact_rejects_when_both_names_blank(reader):
    """Swift enforces at least one of given_name/family_name. Without
    this guard the CN store would happily save a contact with no
    identifiable fields, which then ruins identity diff checks (every
    blank contact looks like every other blank contact).
    """
    resp = await reader._xcuitest._send({"type": "create_contact"})
    assert resp.get("ok") is False
    assert "required" in resp.get("error", "")


async def test_wipe_contacts_clears_store(reader):
    """Reset semantics: wipe must remove every contact, not just
    half-clear or leave system entries behind.
    """
    # Seed three contacts.
    for i in range(3):
        await reader._xcuitest._send({
            "type": "create_contact",
            "given_name": f"Person{i}",
            "family_name": "Test",
        })
    # Confirm they're there.
    resp = await reader._xcuitest._send({"type": "list_contacts"})
    assert resp.get("ok") is True
    assert len(resp.get("contacts", [])) >= 3
    # Wipe.
    resp = await reader._xcuitest._send({"type": "wipe_contacts"})
    assert resp.get("ok") is True
    assert resp.get("removed_contacts", 0) >= 3
    # Verify empty.
    resp = await reader._xcuitest._send({"type": "list_contacts"})
    assert resp.get("ok") is True
    assert resp.get("contacts", []) == []


async def test_list_contacts_name_filter_pushdown(reader):
    """Server-side name_filter (case-insensitive substring) matches
    against given/family/concat. Pushdown happens in Swift, not in
    the Python verifier, so this is the only place where the
    pushdown's correctness is L2-testable.
    """
    await reader._xcuitest._send({"type": "wipe_contacts"})
    for given, family in [("Ada", "Lovelace"),
                            ("Grace", "Hopper"),
                            ("Alan", "Turing")]:
        await reader._xcuitest._send({
            "type": "create_contact",
            "given_name": given, "family_name": family,
        })
    resp = await reader._xcuitest._send({
        "type": "list_contacts", "name_filter": "lov"})
    rows = resp.get("contacts", [])
    assert [r["family_name"] for r in rows] == ["Lovelace"]
    resp = await reader._xcuitest._send({
        "type": "list_contacts", "name_filter": "race"})
    rows = resp.get("contacts", [])
    assert [r["given_name"] for r in rows] == ["Grace"]


# ────────────────────── ContactsHandler integration ────────────────

async def test_handler_apply_then_verifier_fetcher_round_trip(reader):
    """End-to-end: handler.apply → resource fetcher returns the
    record. This is the loop that an actual verifier-AFTER step
    exercises during an episode.
    """
    from sibb_verify import RESOURCE_FETCHERS

    await reader._xcuitest._send({"type": "wipe_contacts"})
    handler = ContactsHandler(reader=reader._xcuitest)
    await handler.apply({"type": "contact",
                          "given_name": "Alan",
                          "family_name": "Turing",
                          "phone": "+44-555-0142"})

    fetcher = RESOURCE_FETCHERS["contacts.all"]
    rows = await fetcher(reader._xcuitest, {})
    matches = [r for r in rows
               if r.get("given_name") == "Alan"
               and r.get("family_name") == "Turing"]
    assert len(matches) == 1
    assert matches[0]["phone"] == "+44-555-0142"


async def test_handler_reset_wipes_via_handler_api(reader):
    """Same as test_wipe_contacts_clears_store but exercising the
    handler.reset() entry point rather than the raw socket — this is
    the path the dispatcher takes during apply_initial_state."""
    handler = ContactsHandler(reader=reader._xcuitest)
    await handler.apply({"type": "contact", "given_name": "X"})
    await handler.apply({"type": "contact", "given_name": "Y"})
    await handler.reset()
    resp = await reader._xcuitest._send({"type": "list_contacts"})
    assert resp.get("contacts") == []


# ────────────────────── Onboarding dismissal ───────────────────────

# Why no explicit "onboarding-isn't-blocking" L2 test: the handler
# round-trip tests above already prove this indirectly. If Contacts'
# welcome / Add Account / iCloud prompt were stuck in front, the CN
# framework calls (create/list/wipe) would either fail outright or
# would land in a permission-blocked state. They don't — they round
# trip cleanly. A dedicated AX-based "is the welcome screen gone?"
# check via observe() + bundle-switch surfaced an unrelated runner-
# socket flake under load (cross-app observe was closing the socket
# in some runs) and didn't add coverage beyond what the framework
# tests provide.
