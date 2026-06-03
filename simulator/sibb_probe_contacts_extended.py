#!/usr/bin/env python3
"""Smoke test for the extended Contacts handler (Phase 1 infra).

Validates:
  - Fresh-store cross-process refresh (no stale cache between calls)
  - create_contact accepts new fields (birthday, multi-phone, postal,
    nickname, jobTitle, urls, dates, ...)
  - list_contacts returns the new fields with correct label mapping
  - update_contact mutates a target without breaking others
  - parse round-trip on "YYYY-MM-DD" birthdays

Run:
  /Library/Developer/CommandLineTools/usr/bin/python3 \
    sibb/simulator/sibb_probe_contacts_extended.py
"""
from __future__ import annotations
import asyncio, os, sys, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sibb_xcuitest_client import XCUITestReader

UDID = "19B95A95-614A-4ECA-B943-44FDADFD7A9F"


def check(label: str, cond: bool, evidence: str = "") -> bool:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}" + (f" — {evidence}" if evidence else ""))
    return cond


async def main():
    reader = XCUITestReader(UDID, bundle_id="com.apple.springboard")
    await reader.start()
    fails = 0
    try:
        async with reader._lock:
            # 1) Wipe
            print("\n=== wipe_contacts ===")
            r = await reader._send({"type": "wipe_contacts"})
            assert r.get("ok"), f"wipe failed: {r}"
            print(f"  wiped {r.get('removed_contacts')} contacts")

            # 2) Create with new fields
            print("\n=== create_contact (full payload) ===")
            payload = {
                "type": "create_contact",
                "given_name": "Alice",
                "family_name": "Tester",
                "middle_name": "Q",
                "nickname": "Al",
                "phonetic_given_name": "AL-iss",
                "organization": "Acme Inc",
                "job_title": "Engineer",
                "department": "Platform",
                "birthday": "1990-04-15",
                "phones": [
                    {"label": "mobile", "value": "+1-650-555-0001"},
                    {"label": "work",   "value": "+1-650-555-0002"},
                ],
                "emails": [
                    {"label": "home", "value": "alice@home.example"},
                    {"label": "work", "value": "alice@acme.example"},
                ],
                "postal_addresses": [
                    {"label": "home",
                     "street": "1 Apple Park Way",
                     "city": "Cupertino", "state": "CA",
                     "postal_code": "95014", "country": "USA"},
                ],
                "urls": [{"label": "homepage", "value": "https://acme.example"}],
                "dates": [{"label": "anniversary", "iso": "2015-06-20"}],
            }
            r = await reader._send(payload)
            ok1 = check("create returned ok", r.get("ok") is True, json.dumps(r))
            alice_id = r.get("identifier") or ""

            # 3) Create a second contact (minimal) to verify multi-row
            r = await reader._send({
                "type": "create_contact",
                "given_name": "Bob",
                "family_name": "Distractor",
                "phone": "+1-650-555-9999",  # legacy single-phone path
            })
            check("legacy single-phone create still works",
                  r.get("ok") is True, json.dumps(r))

            # 4) List and inspect Alice's row
            print("\n=== list_contacts (verify round-trip) ===")
            r = await reader._send({"type": "list_contacts"})
            if not r.get("ok"):
                print(f"  list failed: {r}"); return
            alice = next((c for c in r.get("contacts", [])
                           if c.get("given_name") == "Alice"), None)
            if alice is None:
                print("  ✗ Alice not in list result"); return

            print(f"  alice = {json.dumps(alice, indent=2, default=str)}")

            ok2 = check("middle_name round-trips",
                        alice.get("middle_name") == "Q")
            ok3 = check("nickname round-trips",
                        alice.get("nickname") == "Al")
            ok4 = check("phonetic_given_name round-trips",
                        alice.get("phonetic_given_name") == "AL-iss")
            ok5 = check("job_title round-trips",
                        alice.get("job_title") == "Engineer")
            ok6 = check("department round-trips",
                        alice.get("department") == "Platform")
            ok7 = check("birthday round-trips as YYYY-MM-DD",
                        alice.get("birthday") == "1990-04-15",
                        alice.get("birthday", ""))

            phones = alice.get("phones") or []
            phone_set = {(p.get("label"), p.get("value")) for p in phones}
            ok8 = check("phones — mobile entry present",
                        ("mobile", "+1-650-555-0001") in phone_set)
            ok9 = check("phones — work entry present",
                        ("work", "+1-650-555-0002") in phone_set)
            ok10 = check("phones — exactly 2 entries", len(phones) == 2)

            emails = alice.get("emails") or []
            email_labels = {e.get("label") for e in emails}
            ok11 = check("emails — both labels present",
                         email_labels == {"home", "work"},
                         str(sorted(email_labels)))

            addrs = alice.get("postal_addresses") or []
            ok12 = check("postal_addresses — exactly 1", len(addrs) == 1)
            if addrs:
                a = addrs[0]
                ok13 = check("postal — street round-trip",
                             a.get("street") == "1 Apple Park Way")
                ok14 = check("postal — postal_code round-trip",
                             a.get("postal_code") == "95014")
                ok15 = check("postal — label is 'home'",
                             a.get("label") == "home")
            else:
                ok13 = ok14 = ok15 = False

            urls = alice.get("urls") or []
            ok16 = check("urls — homepage round-trip",
                         len(urls) == 1
                         and urls[0].get("label") == "homepage"
                         and urls[0].get("value") == "https://acme.example")

            dates = alice.get("dates") or []
            ok17 = check("dates — anniversary round-trip",
                         len(dates) == 1
                         and dates[0].get("label") == "anniversary"
                         and dates[0].get("iso") == "2015-06-20",
                         json.dumps(dates))

            # 5) Update Alice's phone array (add a third phone, keep others)
            print("\n=== update_contact (multi-value replace) ===")
            r = await reader._send({
                "type": "update_contact",
                "identifier": alice_id,
                "phones": [
                    {"label": "mobile", "value": "+1-650-555-0001"},
                    {"label": "work",   "value": "+1-650-555-0002"},
                    {"label": "home",   "value": "+1-650-555-0003"},
                ],
            })
            ok18 = check("update returned ok",
                         r.get("ok") is True, json.dumps(r))

            r = await reader._send({"type": "list_contacts"})
            alice2 = next((c for c in r.get("contacts", [])
                            if c.get("given_name") == "Alice"), {})
            phones2 = alice2.get("phones") or []
            ok19 = check("update — phones now has 3", len(phones2) == 3,
                         str(len(phones2)))

            # 6) Confirm Bob's row was untouched by the update
            bob = next((c for c in r.get("contacts", [])
                         if c.get("given_name") == "Bob"), None)
            ok20 = check("Bob preserved through update",
                         bob is not None and bob.get("phone")
                          == "+1-650-555-9999")

            results = [ok1, ok2, ok3, ok4, ok5, ok6, ok7, ok8, ok9, ok10,
                       ok11, ok12, ok13, ok14, ok15, ok16, ok17, ok18,
                       ok19, ok20]
            fails = sum(1 for x in results if not x)
            print(f"\n{'PASS' if fails == 0 else f'FAIL ({fails}/{len(results)})'}: "
                  f"{len(results) - fails}/{len(results)} checks")
    finally:
        await reader.stop()
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
