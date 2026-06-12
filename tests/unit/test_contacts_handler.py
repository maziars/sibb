"""ContactsHandler — L1 + L1.5 tests.

Mirrors the structure of Reminders/Calendar handler tests:
- Handler-protocol attribute lints (bundle_id, tcc_services, etc.)
- Registry membership
- Reset + apply against the in-memory FakeXCUITestReader
- Typed-spec dataclass round-trip + validation

Goal: every Contacts code path Python touches has a fast unit test
before we pay the ~30 s/test L2 sim integration cost.
"""

from __future__ import annotations

import pytest

from sibb_spec import Contact, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    ContactsHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_contacts_handler_registered_by_bundle_id():
    assert ContactsHandler.bundle_id == "com.apple.MobileAddressBook"
    assert HANDLERS[ContactsHandler.bundle_id] is ContactsHandler


def test_contacts_handler_declares_contacts_tcc_service():
    assert ContactsHandler.tcc_services == ["contacts"]


def test_contacts_handler_is_not_a_pre_runner():
    """Contacts state lives in the CN store, not the home-screen plist
    — no shut-down-required apply step."""
    assert ContactsHandler.pre_runner is False
    assert ContactsHandler.pre_runner_kinds == []


def test_contacts_in_collect_tcc_services_union():
    """Adding ContactsHandler must surface `contacts` in the
    union that ensure_runner_permissions iterates."""
    services = collect_tcc_services()
    assert "contacts" in services


def test_canonicalize_contacts_friendly_name():
    """`canonicalize_app('Contacts')` must resolve to the bundle id —
    generators use the friendly name, the dispatcher needs the bundle id.
    """
    assert canonicalize_app("Contacts") == "com.apple.MobileAddressBook"
    assert canonicalize_app("contacts") == "com.apple.MobileAddressBook"


# ─────────────────────────── Contact spec dataclass ──────────────────

def test_contact_spec_registered():
    assert ("Contacts", "contact") in SPEC_TYPES
    assert SPEC_TYPES[("Contacts", "contact")] is Contact


def test_contact_minimal_construction_with_given_name():
    c = Contact(given_name="Ada")
    assert c.given_name == "Ada"
    assert c.family_name == ""
    assert c.phone is None
    assert c.email is None
    assert c.organization is None


def test_contact_minimal_construction_with_family_name():
    c = Contact(family_name="Lovelace")
    assert c.family_name == "Lovelace"
    assert c.given_name == ""


def test_contact_to_dict_canonical_shape():
    """The Contact dataclass has grown over time (added v1 fields
    like nickname, postal_addresses, etc.). This test pins:
      * The 5 explicitly-set fields appear with their values.
      * Every None-default field is preserved as None (so handlers
        can round-trip through dict-form without losing the slot).
      * The app + type discriminators are stable.
    """
    c = Contact(given_name="Ada", family_name="Lovelace",
                 phone="+1-555-0100", email="ada@example.com",
                 organization="Analytical Engine Co.")
    d = c.to_dict()
    # Stable discriminators + explicitly-set values.
    assert d["app"] == "Contacts"
    assert d["type"] == "contact"
    assert d["given_name"] == "Ada"
    assert d["family_name"] == "Lovelace"
    assert d["phone"] == "+1-555-0100"
    assert d["email"] == "ada@example.com"
    assert d["organization"] == "Analytical Engine Co."
    # Every other dataclass field must appear (as None) so round-trip
    # via `from_dict` is loss-less. Sample a few representative ones.
    for none_field in ("middle_name", "nickname", "job_title",
                        "department", "birthday", "phones", "emails",
                        "postal_addresses", "urls", "dates"):
        assert none_field in d, f"missing field {none_field}"
        assert d[none_field] is None, (
            f"{none_field} should be None when unset; got {d[none_field]!r}")


def test_contact_round_trip():
    original = Contact(given_name="Grace", family_name="Hopper",
                        phone="+1-555-0200")
    back = Contact.from_dict(original.to_dict())
    assert back == original


def test_validate_entry_accepts_contact():
    typed, err = validate_entry({
        "app": "Contacts", "type": "contact",
        "given_name": "Alan", "family_name": "Turing",
    })
    assert err is None
    assert isinstance(typed, Contact)
    assert typed.given_name == "Alan"


# ─────────────────────────── handler reset + apply ───────────────────

async def test_handler_apply_creates_contact_via_socket():
    """apply with a contact entry sends create_contact to the reader.
    """
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = ContactsHandler(reader=r)
    await h.apply({"type": "contact",
                    "given_name": "Ada", "family_name": "Lovelace",
                    "phone": "+1-555-0100"})
    # Fake records the request shape — verify Swift command name
    # and forwarded fields.
    last = r.history[-1]
    assert last["request"]["type"] == "create_contact"
    assert last["request"]["given_name"] == "Ada"
    assert last["request"]["family_name"] == "Lovelace"
    assert last["request"]["phone"] == "+1-555-0100"
    assert last["response"]["ok"] is True


async def test_handler_apply_omits_optional_fields_when_none():
    """The Swift schema expects optional fields to be absent when
    unset, not None — verify the handler drops Nones rather than
    forwarding them. Otherwise Swift's `as? String` casts succeed
    on NSNull (which decodes back to nil) but a buggy fake or future
    Swift change could regress on this contract."""
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = ContactsHandler(reader=r)
    await h.apply({"type": "contact", "given_name": "Bare"})
    req = r.history[-1]["request"]
    for opt in ("phone", "email", "organization"):
        assert opt not in req, (
            f"optional field {opt!r} should not be sent when omitted "
            f"from entry, but request was {req!r}"
        )


async def test_handler_reset_calls_wipe_contacts():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = ContactsHandler(reader=r)
    # Seed two contacts then reset.
    await h.apply({"type": "contact", "given_name": "A"})
    await h.apply({"type": "contact", "given_name": "B"})
    await h.reset()
    # The fake should now be empty; verify via list_contacts.
    resp = await r._send({"type": "list_contacts"})
    assert resp["ok"] is True
    assert resp["contacts"] == []


async def test_handler_apply_raises_on_socket_error():
    """If the socket reports failure, apply must raise — silent
    failure on entry creation would let an empty episode start with
    a passing verifier (state wasn't actually applied).
    """
    class FailingReader:
        async def _send(self, cmd):
            return {"ok": False, "error": "no contacts permission"}
    h = ContactsHandler(reader=FailingReader())
    with pytest.raises(RuntimeError, match="no contacts permission"):
        await h.apply({"type": "contact", "given_name": "X"})


async def test_handler_apply_rejects_unknown_entry_kind():
    class _NoopReader:
        async def _send(self, cmd):
            return {"ok": True}
    h = ContactsHandler(reader=_NoopReader())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "definitely_not_a_contact"})


# ─────────────────────────── fake-reader lookups ─────────────────────

async def test_fake_reader_list_contacts_returns_seeded_records():
    """Sanity: the fake's create→list round trip preserves field shape.
    This is the harness for L1.5 generator tests that build a contact
    spec, dispatch through the handler, and then verifier-fetch.
    """
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    await r._send({"type": "create_contact",
                    "given_name": "Ada", "family_name": "Lovelace",
                    "phone": "+1-555-0100",
                    "email": "ada@example.com",
                    "organization": "Analytical Engine Co."})
    resp = await r._send({"type": "list_contacts"})
    assert resp["ok"] is True
    assert len(resp["contacts"]) == 1
    c = resp["contacts"][0]
    assert c["given_name"] == "Ada"
    assert c["family_name"] == "Lovelace"
    assert c["phone"] == "+1-555-0100"
    assert c["email"] == "ada@example.com"
    assert c["organization"] == "Analytical Engine Co."


async def test_fake_reader_list_contacts_filters_by_name():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    for given, family in [("Ada", "Lovelace"),
                            ("Grace", "Hopper"),
                            ("Alan", "Turing")]:
        await r._send({"type": "create_contact",
                        "given_name": given, "family_name": family})

    resp = await r._send({"type": "list_contacts",
                            "name_filter": "lov"})
    assert [c["family_name"] for c in resp["contacts"]] == ["Lovelace"]
    resp = await r._send({"type": "list_contacts",
                            "name_filter": "lan"})  # matches Alan only
    names = sorted(c["given_name"] for c in resp["contacts"])
    assert names == ["Alan"]


async def test_fake_reader_create_contact_requires_name():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_contact"})
    assert resp["ok"] is False
    assert "required" in resp["error"]


# ─────────────────────────── resource fetcher wiring ─────────────────

def test_contacts_all_in_resource_fetchers():
    """The verifier framework's `contacts.all` resource must be
    registered so identity / exists / absent checks can target it
    without a kind=error.
    """
    from sibb_verify import RESOURCE_FETCHERS
    assert "contacts.all" in RESOURCE_FETCHERS


async def test_contacts_all_fetcher_returns_socket_rows():
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "create_contact",
                    "given_name": "Ada", "family_name": "Lovelace"})
    fetcher = RESOURCE_FETCHERS["contacts.all"]
    rows = await fetcher(r, {})
    assert len(rows) == 1
    assert rows[0]["given_name"] == "Ada"


async def test_contacts_all_fetcher_pushes_name_filter_to_socket():
    """`name_filter` is a SOCKET pushdown, not a client-side selector.
    The fetcher must forward it as `cmd["name_filter"]` AND strip it
    from the client-side selector before `_filter_records` runs (else
    every record gets rejected for not having `name_filter` as a key).
    """
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    for given in ("Ada", "Grace", "Alan"):
        await r._send({"type": "create_contact", "given_name": given})
    fetcher = RESOURCE_FETCHERS["contacts.all"]
    rows = await fetcher(r, {"name_filter": "lan"})
    names = sorted(row["given_name"] for row in rows)
    assert names == ["Alan"]
