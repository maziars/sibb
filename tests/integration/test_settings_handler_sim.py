"""SettingsHandler — L2 sim integration.

Exercises the real `xcrun simctl spawn <udid> defaults write/read`
roundtrip against a sim. No XCUITest socket involvement — Settings
goes around the runner entirely.

Covers what the L1+L1.5 mock-subprocess tests can't:
1. simctl actually accepts the argv shapes we build (catches a
   missing flag, wrong arg order, etc.).
2. The bool YES/NO canonicalization survives the iOS-side parser
   (a bool written as "YES" reads back as "1" via `defaults read`;
   we assert the read path matches).
3. Reads after writes actually see the value (catches AppGroup vs
   device-domain mistakes — see Reminders welcome key spelunking).

Light touch: we write to a sentinel test domain
(`com.sibb.tests.settings_handler`) that no real iOS app uses, so
the writes don't pollute baseline state and we don't need to clean
up the touched keys before clone teardown.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.sim

_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
for p in (_SIM_DIR, _BENCHMARK_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sibb_state import (  # noqa: E402
    SettingsHandler,
    _simctl_defaults_read,
    _simctl_defaults_write,
)


_TEST_DOMAIN = "com.sibb.tests.settings_handler"


class _UdidStub:
    """Tiny stand-in for XCUITestReader — exposes .udid only.
    SettingsHandler doesn't need anything else."""
    def __init__(self, udid: str):
        self.udid = udid


# ────────────────────── simctl roundtrip ──────────────────────────

async def test_defaults_write_string_roundtrip(sibb_udid: str):
    await _simctl_defaults_write(
        sibb_udid, _TEST_DOMAIN, "SibbString",
        "hello-l2", "string")
    val = await _simctl_defaults_read(sibb_udid, _TEST_DOMAIN, "SibbString")
    assert val == "hello-l2"


async def test_defaults_write_bool_true_reads_as_one(sibb_udid: str):
    """iOS canonicalizes bool defaults to numeric: -bool YES → 1
    on read. Lock the read shape so verifier fetchers know what
    string to compare against."""
    await _simctl_defaults_write(
        sibb_udid, _TEST_DOMAIN, "SibbBoolTrue",
        True, "bool")
    val = await _simctl_defaults_read(sibb_udid, _TEST_DOMAIN, "SibbBoolTrue")
    assert val == "1"


async def test_defaults_write_bool_false_reads_as_zero(sibb_udid: str):
    await _simctl_defaults_write(
        sibb_udid, _TEST_DOMAIN, "SibbBoolFalse",
        False, "bool")
    val = await _simctl_defaults_read(sibb_udid, _TEST_DOMAIN, "SibbBoolFalse")
    assert val == "0"


async def test_defaults_write_int_roundtrip(sibb_udid: str):
    await _simctl_defaults_write(
        sibb_udid, _TEST_DOMAIN, "SibbInt",
        42, "int")
    val = await _simctl_defaults_read(sibb_udid, _TEST_DOMAIN, "SibbInt")
    assert val == "42"


async def test_defaults_write_float_roundtrip(sibb_udid: str):
    await _simctl_defaults_write(
        sibb_udid, _TEST_DOMAIN, "SibbFloat",
        2.5, "float")
    val = await _simctl_defaults_read(sibb_udid, _TEST_DOMAIN, "SibbFloat")
    # `defaults read` of a float can print as "2.5" or "2.500000"
    # depending on iOS version; tolerate either.
    assert float(val) == 2.5


async def test_defaults_read_missing_key_returns_empty(sibb_udid: str):
    val = await _simctl_defaults_read(
        sibb_udid, _TEST_DOMAIN,
        "DefinitelyNeverWrittenKey_zxc")
    assert val == ""


async def test_defaults_read_missing_domain_returns_empty(sibb_udid: str):
    val = await _simctl_defaults_read(
        sibb_udid, "com.sibb.tests.does_not_exist",
        "AnyKey")
    assert val == ""


# ────────────────────── SettingsHandler integration ──────────────

async def test_handler_apply_writes_value(sibb_udid: str):
    """End-to-end: handler.apply → value is readable via simctl."""
    h = SettingsHandler(reader=_UdidStub(sibb_udid))
    await h.apply({"type": "default",
                    "domain": _TEST_DOMAIN,
                    "key": "ViaHandler",
                    "value": "applied",
                    "value_type": "string"})
    val = await _simctl_defaults_read(
        sibb_udid, _TEST_DOMAIN, "ViaHandler")
    assert val == "applied"


async def test_handler_apply_then_verifier_fetcher_roundtrip(sibb_udid: str):
    """Mirror the verifier-AFTER loop: handler.apply writes a value,
    settings.defaults fetcher reads it back."""
    from sibb_verify import RESOURCE_FETCHERS

    h = SettingsHandler(reader=_UdidStub(sibb_udid))
    await h.apply({"type": "default",
                    "domain": _TEST_DOMAIN,
                    "key": "VerifierKey",
                    "value": True,
                    "value_type": "bool"})

    fetcher = RESOURCE_FETCHERS["settings.defaults"]
    rows = await fetcher(_UdidStub(sibb_udid),
                          {"domain": _TEST_DOMAIN,
                           "key": "VerifierKey"})
    assert rows == [{"domain": _TEST_DOMAIN,
                      "key": "VerifierKey", "value": "1"}]


# ────────────────────── cleanup hook ─────────────────────────────

@pytest.fixture(scope="module", autouse=True)
async def _cleanup(sibb_udid: str):
    """Wipe the test domain after the module finishes so we don't
    leak sentinel keys into any subsequent test's view of the sim."""
    yield
    # `defaults delete <domain>` returns nonzero if domain doesn't
    # exist — fine, we want best-effort cleanup. _simctl_defaults_read
    # swallows nonzero rc; for delete we just run subprocess directly.
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "spawn", sibb_udid,
        "defaults", "delete", _TEST_DOMAIN,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
