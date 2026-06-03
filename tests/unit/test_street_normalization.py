"""L1 tests for street-address normalization in the verifier.

Covers:
  - `_canonicalize_street` standalone — common US street suffix
    abbreviations, ordinal words (Fifth→5th), directionals (N/S/E/W),
    punctuation stripping, whitespace collapse.
  - `attribute_set_contains` with `street_norm_keys` accepts
    equivalent street spellings, rejects truly different streets.
  - `attribute_set_equals` with `street_norm_keys` (multi-set form).
  - Overlap-rejection invariant: street_norm_keys must not overlap
    with time_keys or digits_only_keys (double-canonicalization is
    silently corrupting).
"""
from __future__ import annotations
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_verify import (    # noqa: E402
    _canonicalize_street,
    _check_attribute_set_contains,
    _check_attribute_set_equals,
)


# ── canonicalizer ────────────────────────────────────────────────────────────

def test_canonicalize_suffix_avenue():
    assert _canonicalize_street("350 5th Avenue") == "350 5th ave"
    assert _canonicalize_street("350 5th Ave") == "350 5th ave"
    assert _canonicalize_street("350 5th Ave.") == "350 5th ave"
    assert _canonicalize_street("350 5th av") == "350 5th ave"


def test_canonicalize_suffix_boulevard():
    assert _canonicalize_street("1100 Sunset Boulevard") == "1100 sunset blvd"
    assert _canonicalize_street("1100 Sunset Blvd") == "1100 sunset blvd"
    assert _canonicalize_street("1100 Sunset Blvd.") == "1100 sunset blvd"


def test_canonicalize_suffix_street():
    assert _canonicalize_street("250 Howard Street") == "250 howard st"
    assert _canonicalize_street("250 Howard St") == "250 howard st"
    assert _canonicalize_street("250 howard st.") == "250 howard st"


def test_canonicalize_suffix_road():
    assert _canonicalize_street("22 Beach Road") == "22 beach rd"
    assert _canonicalize_street("22 Beach Rd") == "22 beach rd"


def test_canonicalize_ordinal_words():
    """Fifth ≡ 5th, Third ≡ 3rd, etc. — common in NYC-style addresses."""
    assert _canonicalize_street("350 Fifth Avenue") == "350 5th ave"
    assert _canonicalize_street("100 Third St") == "100 3rd st"
    assert _canonicalize_street("Eighth Avenue") == "8th ave"


def test_canonicalize_directionals():
    assert _canonicalize_street("N. Main St") == "n main st"
    assert _canonicalize_street("North Main Street") == "n main st"
    assert _canonicalize_street("Northeast 42nd Ave") == "ne 42nd ave"


def test_canonicalize_strips_punctuation_but_keeps_hyphens_slashes():
    """Commas and periods are noise; hyphens (in '42-A') and slashes
    (in '12/34') are sometimes load-bearing."""
    assert _canonicalize_street("1 Apple Park Way, Cupertino") == \
           "1 apple park way cupertino"
    assert _canonicalize_street("42-A Hollyhock") == "42-a hollyhock"


def test_canonicalize_non_string_passes_through():
    assert _canonicalize_street(None) is None
    assert _canonicalize_street(42) == 42


def test_canonicalize_collapses_whitespace():
    assert _canonicalize_street("350   5th    Avenue") == "350 5th ave"


# ── attribute_set_contains with street_norm_keys ─────────────────────────────

def _contacts_record(street: str, label: str = "home",
                      city: str = "New York"):
    return [{
        "given_name": "Sam", "family_name": "Chen",
        "postal_addresses": [
            {"label": label, "street": street, "city": city,
             "state": "NY", "postal_code": "10001"},
        ],
    }]


def test_set_contains_street_norm_matches_avenue_variants():
    """Expected typed-by-agent: '350 5th Avenue'. Saved-by-iOS:
    '350 5th Ave'. With street_norm_keys=['street'], they match."""
    records = _contacts_record("350 5th Ave")
    check = {
        "kind": "attribute_set_contains",
        "resource": "contacts.all",
        "selector": {},
        "attr": "postal_addresses",
        "expected": [{"label": "home", "street": "350 5th Avenue",
                       "city": "New York"}],
        "street_norm_keys": ["street"],
        "case_sensitive": False,
        "trim_strings": True,
        "severity": "blocking",
        "label": "test",
    }
    status, _ = _check_attribute_set_contains(records, check)
    assert status == "pass"


def test_set_contains_street_norm_matches_fifth_to_5th():
    """Agent typed 'Fifth Avenue' (NYC-style); iOS saved '5th Ave'."""
    records = _contacts_record("350 5th Ave")
    check = {
        "kind": "attribute_set_contains",
        "resource": "contacts.all",
        "selector": {},
        "attr": "postal_addresses",
        "expected": [{"label": "home", "street": "350 Fifth Avenue",
                       "city": "New York"}],
        "street_norm_keys": ["street"],
        "case_sensitive": False,
        "trim_strings": True,
        "severity": "blocking",
        "label": "test",
    }
    status, _ = _check_attribute_set_contains(records, check)
    assert status == "pass"


def test_set_contains_street_norm_rejects_different_street():
    """A genuinely different street still fails (norm is not a fuzzy
    string distance — it only normalizes presentation)."""
    records = _contacts_record("350 5th Ave")
    check = {
        "kind": "attribute_set_contains",
        "resource": "contacts.all",
        "selector": {},
        "attr": "postal_addresses",
        "expected": [{"label": "home", "street": "350 6th Avenue",
                       "city": "New York"}],
        "street_norm_keys": ["street"],
        "case_sensitive": False,
        "trim_strings": True,
        "severity": "blocking",
        "label": "test",
    }
    status, _ = _check_attribute_set_contains(records, check)
    assert status == "fail"


def test_set_contains_street_norm_rejects_wrong_number():
    """Different building number → no match. Catches the agent
    typo cheat (350 vs 305)."""
    records = _contacts_record("305 5th Avenue")
    check = {
        "kind": "attribute_set_contains",
        "resource": "contacts.all",
        "selector": {},
        "attr": "postal_addresses",
        "expected": [{"label": "home", "street": "350 5th Avenue",
                       "city": "New York"}],
        "street_norm_keys": ["street"],
        "severity": "blocking",
        "label": "test",
    }
    status, _ = _check_attribute_set_contains(records, check)
    assert status == "fail"


# ── attribute_set_equals with street_norm_keys ───────────────────────────────

def test_set_equals_street_norm_in_full_address_dict():
    """When using set_equals (full schema match), street_norm_keys
    must apply across the item_keys."""
    actual = [{"given_name": "Sam", "family_name": "Chen",
                "postal_addresses": [
                    {"label": "home", "street": "350 5th Ave",
                     "city": "New York", "state": "NY",
                     "postal_code": "10001", "country": "USA"},
                ]}]
    check = {
        "kind": "attribute_set_equals",
        "resource": "contacts.all",
        "selector": {},
        "attr": "postal_addresses",
        "expected": [
            {"label": "home", "street": "350 5th Avenue",
             "city": "New York", "state": "NY",
             "postal_code": "10001", "country": "USA"},
        ],
        "item_keys": ["label", "street", "city", "state",
                       "postal_code", "country"],
        "street_norm_keys": ["street"],
        "case_sensitive": False,
        "trim_strings": True,
        "severity": "blocking",
        "label": "test",
    }
    status, _ = _check_attribute_set_equals(actual, check)
    assert status == "pass"


# ── invariants ───────────────────────────────────────────────────────────────

def test_street_norm_and_digits_overlap_rejected():
    """Sanity guard: street_norm_keys and digits_only_keys must not
    overlap on the same key (double-canonicalization order would
    silently change results)."""
    records = [{"given_name": "X", "postal_addresses": []}]
    check = {
        "kind": "attribute_set_equals",
        "resource": "contacts.all",
        "selector": {},
        "attr": "postal_addresses",
        "expected": [{"label": "home", "street": "x"}],
        "item_keys": ["label", "street"],
        "street_norm_keys": ["street"],
        "digits_only_keys": ["street"],
        "case_sensitive": False,
        "trim_strings": True,
        "severity": "blocking",
        "label": "test",
    }
    try:
        _check_attribute_set_equals(records, check)
    except ValueError as e:
        assert "must not overlap" in str(e)
        return
    raise AssertionError("expected ValueError for overlap")
