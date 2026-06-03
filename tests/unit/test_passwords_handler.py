"""PasswordsHandler — L1 tests.

iOS 18+ Passwords app investigation (2026-05-17) revealed the
keychain access-group architecture: writes from the test runner
bundle can't land in `com.apple.password-manager`, so v1 has no
apply primitive. Verification is row-count-by-access-group from
the keychain SQLite + AX-tree observation of the UI list.

Tests cover the v1 contract (no apply, registry presence, fetcher
shape) using an in-memory keychain stand-in.
"""

from __future__ import annotations

import sqlite3

import pytest

import sibb_state
from sibb_state import (
    HANDLERS,
    PasswordsHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_passwords_handler_registered_by_bundle_id():
    assert PasswordsHandler.bundle_id == "com.apple.Passwords"
    assert HANDLERS[PasswordsHandler.bundle_id] is PasswordsHandler


def test_passwords_handler_no_tcc_services():
    """Security.framework doesn't require a SIBB TCC grant. iOS 18
    shows an AutoFill provider prompt but that's a separate
    consent flow, not a TCC service."""
    assert PasswordsHandler.tcc_services == []


def test_passwords_handler_is_not_a_pre_runner():
    assert PasswordsHandler.pre_runner is False
    assert PasswordsHandler.pre_runner_kinds == []


def test_canonicalize_passwords_friendly_name():
    assert canonicalize_app("Passwords") == "com.apple.Passwords"
    assert canonicalize_app("passwords") == "com.apple.Passwords"


def test_passwords_does_not_contribute_to_collect_tcc_services():
    services = collect_tcc_services()
    for s in services:
        assert "password" not in s.lower()


# ─────────────────────────── apply/reset stubs ───────────────────────

async def test_reset_is_noop():
    h = PasswordsHandler(reader=None)
    await h.reset()


async def test_apply_raises_clear_error_in_v1():
    """v1 has no apply primitive. Error message should mention the
    `com.apple.password-manager` access-group entitlement gap so
    future engineers understand the limit (vs thinking it's
    unimplemented)."""
    h = PasswordsHandler(reader=None)
    with pytest.raises(ValueError,
                         match="password-manager.*access group"):
        await h.apply({"type": "password",
                        "service": "example.com",
                        "account": "alice",
                        "password": "hunter2"})


# ─────────────────────────── _passwords_entry_count ──────────────────

def _make_test_keychain(path):
    """Create a fake keychain-2-debug.db with just the `inet`
    table SIBB cares about. Schema mirrors the real iOS 18+ one
    enough for our queries: acct/srvr as BLOB (real DB stores
    SHA-1 hashes there), data BLOB, agrp, and tomb (soft-delete
    flag the entry_exists query filters on)."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE inet (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            acct BLOB,
            srvr BLOB,
            data BLOB,
            agrp TEXT NOT NULL,
            tomb INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()


def test_entry_count_returns_zero_when_db_missing(monkeypatch, tmp_path):
    """If keychain-2-debug.db doesn't exist (e.g. the runner
    hasn't been launched yet), entry count is 0 — not an error.
    Same shape as `_safari_list_bookmarks` no-db fallback."""
    missing = tmp_path / "nope.db"
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(missing))
    assert sibb_state._passwords_entry_count("FAKE") == 0


def test_entry_count_counts_by_access_group(monkeypatch, tmp_path):
    """The fetcher must filter by `agrp` to differentiate
    `com.apple.password-manager` (Passwords-app-visible) entries
    from `com.apple.cfnetwork` (Safari AutoFill) and other groups
    that the same keychain holds."""
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    conn = sqlite3.connect(str(db))
    for agrp in ["com.apple.password-manager",
                  "com.apple.password-manager",
                  "com.apple.cfnetwork",
                  "com.apple.cfnetwork",
                  "com.apple.cfnetwork",
                  "apple"]:
        conn.execute(
            "INSERT INTO inet (acct, srvr, data, agrp) "
            "VALUES (?, ?, ?, ?);",
            (b"acct", b"srvr", b"encrypted-blob", agrp))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    assert sibb_state._passwords_entry_count(
        "FAKE", "com.apple.password-manager") == 2
    assert sibb_state._passwords_entry_count(
        "FAKE", "com.apple.cfnetwork") == 3
    assert sibb_state._passwords_entry_count(
        "FAKE", "apple") == 1
    assert sibb_state._passwords_entry_count(
        "FAKE", "nonexistent.group") == 0


# ─────────────────────────── resource fetcher ────────────────────────

class _UdidStub:
    def __init__(self, udid="FAKE"):
        self.udid = udid


def test_passwords_entry_count_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "passwords.entry_count" in RESOURCE_FETCHERS


async def test_fetcher_returns_default_groups_when_no_selector(
        monkeypatch, tmp_path):
    """Default selector returns counts for the three known groups
    (com.apple.password-manager, password-evaluations, cfnetwork).
    Useful for "did any password get added anywhere" verification."""
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO inet (acct, srvr, data, agrp) VALUES "
        "(?, ?, ?, 'com.apple.password-manager');",
        (b"a", b"b", b"c"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    rows = await RESOURCE_FETCHERS["passwords.entry_count"](
        _UdidStub(), {})
    groups = {r["access_group"]: r["count"] for r in rows}
    assert groups["com.apple.password-manager"] == 1
    assert "com.apple.password-manager.password-evaluations" in groups
    assert "com.apple.cfnetwork" in groups


async def test_fetcher_narrows_by_access_group_selector(
        monkeypatch, tmp_path):
    """Passing `access_group=<X>` narrows the result to that one
    group. Used for tight verification like "exactly one entry was
    added in the Passwords-app group, not Safari's group"."""
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO inet (acct, srvr, data, agrp) "
                  "VALUES (?, ?, ?, 'com.apple.password-manager');",
                  (b"a", b"b", b"c"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    rows = await RESOURCE_FETCHERS["passwords.entry_count"](
        _UdidStub(),
        {"access_group": "com.apple.password-manager"})
    assert len(rows) == 1
    assert rows[0]["count"] == 1


async def test_fetcher_requires_udid():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError

    class _NoUdid:
        pass
    fetcher = RESOURCE_FETCHERS["passwords.entry_count"]
    with pytest.raises(ResourceFetchError, match=".udid"):
        await fetcher(_NoUdid(), {})


# ───────────── _passwords_entry_exists (SHA-1 hash equality) ─────────
#
# acct/srvr columns store SHA-1 of the plaintext as a lookup index;
# the value blob stays encrypted. See IOS_SIM_QUIRKS.md §13
# "Hash-equality verification" for the empirical proof.

import hashlib  # noqa: E402  (test-local imports)


def _insert_keychain_row(db_path, *, service, account,
                          agrp="com.apple.password-manager", tomb=0):
    """Insert a row with realistic SHA-1 hash columns."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO inet (acct, srvr, data, agrp, tomb) "
        "VALUES (?, ?, ?, ?, ?);",
        (hashlib.sha1(account.encode()).digest(),
         hashlib.sha1(service.encode()).digest(),
         b"encrypted-blob", agrp, tomb))
    conn.commit()
    conn.close()


def test_entry_exists_returns_false_when_db_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(tmp_path / "nope.db"))
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "alice@example.com") is False


def test_entry_exists_matches_via_sha1_hash_equality(monkeypatch, tmp_path):
    """The hashed-column lookup must succeed when we provide the
    exact plaintext that was written."""
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    _insert_keychain_row(db, service="example.com",
                          account="alice@example.com")
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "alice@example.com") is True


def test_entry_exists_is_case_sensitive_for_inputs(monkeypatch, tmp_path):
    """SHA-1 is byte-exact: casing variants are different hashes.
    The Passwords UI preserves casing in `acct`, so this matches
    real behavior — don't normalize."""
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    _insert_keychain_row(db, service="example.com",
                          account="Alice@example.com")
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "alice@example.com") is False
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "Alice@example.com") is True


def test_entry_exists_rejects_wrong_service(monkeypatch, tmp_path):
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    _insert_keychain_row(db, service="example.com",
                          account="alice@example.com")
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    assert sibb_state._passwords_entry_exists(
        "FAKE", "evil.com", "alice@example.com") is False


def test_entry_exists_filters_by_access_group(monkeypatch, tmp_path):
    """Two entries with the same service+account but in different
    access groups (e.g. Safari AutoFill copy in `cfnetwork` vs
    Passwords-app copy in `password-manager`) must be
    distinguishable."""
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    _insert_keychain_row(db, service="example.com",
                          account="alice@example.com",
                          agrp="com.apple.cfnetwork")
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "alice@example.com") is False
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "alice@example.com",
        agrp="com.apple.cfnetwork") is True


def test_entry_exists_excludes_tombstoned_rows(monkeypatch, tmp_path):
    """`tomb=1` rows are soft-deleted; the Passwords app doesn't
    show them. The fetcher must skip them so a deleted entry
    doesn't appear "still saved"."""
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    _insert_keychain_row(db, service="example.com",
                          account="alice@example.com", tomb=1)
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    assert sibb_state._passwords_entry_exists(
        "FAKE", "example.com", "alice@example.com") is False


def test_passwords_entry_exists_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "passwords.entry_exists" in RESOURCE_FETCHERS


async def test_entry_exists_fetcher_returns_structured_row(
        monkeypatch, tmp_path):
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    _insert_keychain_row(db, service="example.com",
                          account="alice@example.com")
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    rows = await RESOURCE_FETCHERS["passwords.entry_exists"](
        _UdidStub(),
        {"service": "example.com", "account": "alice@example.com"})
    assert rows == [{
        "service": "example.com",
        "account": "alice@example.com",
        "access_group": "com.apple.password-manager",
        "exists": True,
    }]


async def test_entry_exists_fetcher_requires_service_and_account():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    fetcher = RESOURCE_FETCHERS["passwords.entry_exists"]
    with pytest.raises(ResourceFetchError, match="service.*account"):
        await fetcher(_UdidStub(), {"service": "example.com"})
    with pytest.raises(ResourceFetchError, match="service.*account"):
        await fetcher(_UdidStub(), {"account": "alice"})


async def test_entry_exists_fetcher_negative_result(
        monkeypatch, tmp_path):
    """No row → `exists: False`, not an error."""
    from sibb_verify import RESOURCE_FETCHERS
    db = tmp_path / "keychain-2-debug.db"
    _make_test_keychain(db)
    monkeypatch.setattr(sibb_state, "_keychain_db_path",
                         lambda udid: str(db))
    rows = await RESOURCE_FETCHERS["passwords.entry_exists"](
        _UdidStub(),
        {"service": "not-there.com", "account": "alice@nowhere.com"})
    assert len(rows) == 1
    assert rows[0]["exists"] is False
