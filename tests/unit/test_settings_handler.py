"""SettingsHandler — L1 + L1.5 tests.

Settings is the first handler that DOESN'T go through the XCUITest
socket — it shells out to `xcrun simctl spawn defaults`. So the
L1.5 mocking surface is `_simctl_defaults_write` /
`_simctl_defaults_read` rather than FakeXCUITestReader.

Covers:
- Handler-protocol attribute lints
- Registry + canonicalization
- DefaultsEntry typed spec round-trip + every value_type
- apply() calls the right subprocess args for each value_type
- value_type validation (rejects unknown types)
- reset() is intentionally a no-op (documented for future
  refactors that might mistake the no-op for an unfinished impl)
- Resource fetcher routes through _simctl_defaults_read with
  proper domain/key pushdown
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

import sibb_state
from sibb_spec import DefaultsEntry, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    SettingsHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_settings_handler_registered_by_bundle_id():
    assert SettingsHandler.bundle_id == "com.apple.Preferences"
    assert HANDLERS[SettingsHandler.bundle_id] is SettingsHandler


def test_settings_handler_no_tcc_services():
    """`simctl spawn defaults` runs at the simulator user level — no
    TCC permission required. A nonzero tcc_services here would surface
    a no-op grant call."""
    assert SettingsHandler.tcc_services == []


def test_settings_handler_is_not_a_pre_runner():
    assert SettingsHandler.pre_runner is False
    assert SettingsHandler.pre_runner_kinds == []


def test_settings_does_not_contribute_to_collect_tcc_services():
    services = collect_tcc_services()
    for s in services:
        assert "setting" not in s.lower(), (
            f"unexpected settings-related TCC service: {s}")


def test_canonicalize_settings_friendly_name():
    assert canonicalize_app("Settings") == "com.apple.Preferences"
    assert canonicalize_app("settings") == "com.apple.Preferences"


# ─────────────────────────── DefaultsEntry spec ──────────────────────

def test_defaults_entry_spec_registered():
    assert ("Settings", "default") in SPEC_TYPES
    assert SPEC_TYPES[("Settings", "default")] is DefaultsEntry


def test_defaults_entry_string_round_trip():
    e = DefaultsEntry(domain="com.example.app",
                       key="MyKey", value="hello")
    assert e.value_type == "string"
    assert DefaultsEntry.from_dict(e.to_dict()) == e


def test_defaults_entry_bool_round_trip():
    e = DefaultsEntry(domain="com.example.app",
                       key="EnableThing", value=True,
                       value_type="bool")
    d = e.to_dict()
    assert d["value"] is True
    assert d["value_type"] == "bool"
    assert DefaultsEntry.from_dict(d) == e


def test_defaults_entry_int_and_float_round_trip():
    e_int = DefaultsEntry(domain="com.example.app",
                           key="Count", value=42, value_type="int")
    e_float = DefaultsEntry(domain="com.example.app",
                             key="Ratio", value=1.5,
                             value_type="float")
    assert DefaultsEntry.from_dict(e_int.to_dict()) == e_int
    assert DefaultsEntry.from_dict(e_float.to_dict()) == e_float


def test_validate_entry_accepts_defaults_entry():
    typed, err = validate_entry({
        "app": "Settings", "type": "default",
        "domain": "com.example.app", "key": "K",
        "value": "v", "value_type": "string",
    })
    assert err is None
    assert isinstance(typed, DefaultsEntry)


# ─────────────────────────── apply() subprocess args ─────────────────

class _RecordingReader:
    """Stand-in for XCUITestReader — exposes .udid only."""
    def __init__(self, udid: str = "FAKE-UDID"):
        self.udid = udid


async def _record_defaults_writes(monkeypatch):
    """Patch `_simctl_defaults_write` to capture calls; return the
    captures list. Tests use this to assert the handler dispatches
    the correct (udid, domain, key, value, value_type) tuple per
    apply()."""
    calls: List[Tuple[Any, ...]] = []

    async def fake_write(udid, domain, key, value, value_type):
        calls.append((udid, domain, key, value, value_type))

    monkeypatch.setattr(sibb_state, "_simctl_defaults_write", fake_write)
    return calls


async def test_apply_string_entry_shells_to_simctl(monkeypatch):
    calls = await _record_defaults_writes(monkeypatch)
    h = SettingsHandler(reader=_RecordingReader("UDID-1"))
    await h.apply({"type": "default",
                    "domain": "com.example.app",
                    "key": "Greeting", "value": "hello"})
    assert calls == [("UDID-1", "com.example.app",
                       "Greeting", "hello", "string")]


async def test_apply_bool_entry_forwards_value_type(monkeypatch):
    calls = await _record_defaults_writes(monkeypatch)
    h = SettingsHandler(reader=_RecordingReader("UDID-2"))
    await h.apply({"type": "default",
                    "domain": "com.example.app",
                    "key": "Enabled", "value": True,
                    "value_type": "bool"})
    assert calls == [("UDID-2", "com.example.app",
                       "Enabled", True, "bool")]


async def test_apply_omits_value_type_defaults_to_string(monkeypatch):
    """When the spec doesn't specify value_type, the handler must
    use "string" — that's the safest default for arbitrary user
    input. A regression that drops the default would silently coerce
    integers to whatever Swift / `defaults` infers, which can flip
    booleans (`defaults write` parses "1"/"0" as numbers, not bools).
    """
    calls = await _record_defaults_writes(monkeypatch)
    h = SettingsHandler(reader=_RecordingReader())
    await h.apply({"type": "default",
                    "domain": "com.example.app",
                    "key": "K", "value": "v"})
    assert calls[0][-1] == "string"


async def test_apply_raises_when_reader_has_no_udid():
    """The handler shells out using reader.udid. If the reader is
    None or lacks .udid, the call must raise with a clear message
    rather than crashing inside subprocess."""
    h = SettingsHandler(reader=None)
    with pytest.raises(RuntimeError, match="requires a reader"):
        await h.apply({"type": "default",
                        "domain": "com.example.app",
                        "key": "K", "value": "v"})

    class _NoUdid:
        pass
    h2 = SettingsHandler(reader=_NoUdid())
    with pytest.raises(RuntimeError, match="requires a reader"):
        await h2.apply({"type": "default",
                         "domain": "com.example.app",
                         "key": "K", "value": "v"})


async def test_apply_rejects_unknown_entry_kind(monkeypatch):
    await _record_defaults_writes(monkeypatch)
    h = SettingsHandler(reader=_RecordingReader())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "wallpaper"})


# ─────────────────────────── _simctl_defaults_write args ────────────

async def test_defaults_write_rejects_unknown_value_type():
    """The flag-mapping table is the type-safety boundary. An unknown
    value_type must fail loudly before subprocess; otherwise we'd
    spawn `defaults write ... -<None> ...` which is impossible to
    diagnose from the simctl error."""
    with pytest.raises(ValueError, match="unsupported value_type"):
        await sibb_state._simctl_defaults_write(
            "UDID", "com.example.app", "K", "v",
            value_type="quaternion")


async def test_defaults_write_assembles_correct_subprocess_args(monkeypatch):
    """Lock the exact `xcrun simctl spawn` argv shape. This is the
    single point where flag mapping ("bool" → "-bool", etc.) and
    bool-value canonicalization (True → "YES") happen — a regression
    here would flow straight through to the sim's preferences file."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                         fake_create_subprocess_exec)

    await sibb_state._simctl_defaults_write(
        "UDID-X", "com.example.app", "Flag",
        True, "bool")
    args = captured["args"]
    assert args[:5] == ("xcrun", "simctl", "spawn",
                         "UDID-X", "defaults")
    assert args[5] == "write"
    assert args[6] == "com.example.app"
    assert args[7] == "Flag"
    assert args[8] == "-bool"
    assert args[9] == "YES"


async def test_defaults_write_bool_false_emits_NO(monkeypatch):
    """`defaults write -bool false` is rejected by the CLI — values
    must be "YES" or "NO" (or "0"/"1"). We canonicalize Python False
    to "NO" specifically to avoid that footgun."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                         fake_create_subprocess_exec)

    await sibb_state._simctl_defaults_write(
        "U", "com.example.app", "X", False, "bool")
    assert captured["args"][-1] == "NO"


async def test_defaults_write_int_passes_value_as_string(monkeypatch):
    """`defaults write -int 42` accepts an int — but the subprocess
    boundary is bytes. The handler casts via str() so Python ints
    don't blow up create_subprocess_exec's type checker."""
    captured = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        class _Proc:
            returncode = 0
            async def communicate(self):
                return b"", b""
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                         fake_create_subprocess_exec)
    await sibb_state._simctl_defaults_write(
        "U", "com.example.app", "X", 42, "int")
    assert captured["args"][-1] == "42"


async def test_defaults_write_propagates_subprocess_error(monkeypatch):
    """A nonzero rc from `defaults write` (e.g. invalid domain) must
    bubble up as RuntimeError — silent failure would let an episode
    start with the agent's environment in the wrong state."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        class _Proc:
            returncode = 1
            async def communicate(self):
                return b"", b"some error from simctl"
        return _Proc()

    monkeypatch.setattr("asyncio.create_subprocess_exec",
                         fake_create_subprocess_exec)
    with pytest.raises(RuntimeError, match="some error from simctl"):
        await sibb_state._simctl_defaults_write(
            "U", "com.example.app", "X", "v", "string")


# ─────────────────────────── reset is a no-op ────────────────────────

async def test_reset_is_a_documented_noop(monkeypatch):
    """v1 doesn't track touched keys for within-episode rollback.
    The no-op IS the contract — pin it so future refactors don't
    silently `defaults delete` everything (which would wipe the
    baseline's prewarm-suppressed welcome keys for SIBB-11 apps)."""
    calls = await _record_defaults_writes(monkeypatch)
    h = SettingsHandler(reader=_RecordingReader())
    await h.reset()
    assert calls == [], (
        "reset() must not perform any defaults operations in v1 — "
        "between-episode isolation comes from clone-from-baseline"
    )


# ─────────────────────────── settings.defaults fetcher ───────────────

def test_settings_defaults_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "settings.defaults" in RESOURCE_FETCHERS


async def test_fetcher_requires_domain_and_key():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError
    fetcher = RESOURCE_FETCHERS["settings.defaults"]
    r = _RecordingReader()
    for missing in [
        {},
        {"domain": "com.example.app"},
        {"key": "K"},
        {"domain": "", "key": "K"},
    ]:
        with pytest.raises(ResourceFetchError, match="domain.*key"):
            await fetcher(r, missing)


async def test_fetcher_calls_simctl_defaults_read(monkeypatch):
    """Verify the fetcher dispatches to _simctl_defaults_read with
    the (udid, domain, key) tuple."""
    from sibb_verify import RESOURCE_FETCHERS
    captured: Dict[str, Any] = {}

    async def fake_read(udid, domain, key=None):
        captured["udid"] = udid
        captured["domain"] = domain
        captured["key"] = key
        return "some-value"

    monkeypatch.setattr(sibb_state, "_simctl_defaults_read", fake_read)
    fetcher = RESOURCE_FETCHERS["settings.defaults"]
    rows = await fetcher(_RecordingReader("UDID-Z"),
                          {"domain": "com.example.app", "key": "K"})
    assert captured == {"udid": "UDID-Z",
                          "domain": "com.example.app",
                          "key": "K"}
    assert rows == [{"domain": "com.example.app",
                      "key": "K", "value": "some-value"}]


async def test_fetcher_returns_empty_when_key_missing(monkeypatch):
    from sibb_verify import RESOURCE_FETCHERS

    async def fake_read(udid, domain, key=None):
        return ""

    monkeypatch.setattr(sibb_state, "_simctl_defaults_read", fake_read)
    fetcher = RESOURCE_FETCHERS["settings.defaults"]
    rows = await fetcher(_RecordingReader(),
                          {"domain": "com.example.app", "key": "K"})
    assert rows == []
