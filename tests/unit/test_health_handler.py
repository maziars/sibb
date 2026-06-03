"""HealthHandler — L1 + L1.5 tests.

HealthKit handler. Mirrors the Contacts/Reminders pattern but with
the per-sample-type complexity HealthKit adds.

Covers:
- Handler-protocol lints + registry + canonicalization
- TCC service declaration (both share + update)
- HealthSample typed spec round-trip + validation
- apply() socket dispatch with sample_type validation
- reset() routes to wipe_health_samples
- FakeXCUITestReader CRUD + filtering (type, time window)
- health.samples resource fetcher pushdown semantics
"""

from __future__ import annotations

import pytest

from sibb_spec import HealthSample, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    HEALTH_VALID_TYPES,
    HealthHandler,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────

def test_health_handler_registered_by_bundle_id():
    assert HealthHandler.bundle_id == "com.apple.Health"
    assert HANDLERS[HealthHandler.bundle_id] is HealthHandler


def test_health_handler_declares_both_health_tcc_services():
    """HealthKit splits permissions into share (read) and update
    (write). We need both because list_health_samples needs share
    and wipe needs update. Asserting both prevents a regression
    that drops one of them and bricks delete."""
    assert HealthHandler.tcc_services == ["health-share", "health-update"]


def test_health_handler_is_not_a_pre_runner():
    assert HealthHandler.pre_runner is False
    assert HealthHandler.pre_runner_kinds == []


def test_health_services_in_collect_tcc_services_union():
    services = collect_tcc_services()
    assert "health-share" in services
    assert "health-update" in services


def test_canonicalize_health_friendly_name():
    assert canonicalize_app("Health") == "com.apple.Health"
    assert canonicalize_app("health") == "com.apple.Health"


# ─────────────────────────── valid-types contract ────────────────────

def test_health_valid_types_match_v1_scope():
    """HEALTH_VALID_TYPES must equal the v1 scope declared in
    sibb_xcuitest_setup.sh's HEALTH_QUANTITY_TYPES dict. Drift would
    let the Python side dispatch a sample_type that Swift doesn't
    recognize, surfacing as 'unknown sample_type' at runtime.

    Source-lint: grep the Swift table from setup.sh and assert
    the Python list matches.
    """
    import pathlib
    import re
    swift = pathlib.Path(
        "sibb/simulator/sibb_xcuitest_setup.sh").read_text()
    # The Swift declaration includes a type annotation that contains
    # its own [String: ...] brackets — anchor on `= [` to skip past
    # the annotation and capture the actual dict body.
    block = re.search(
        r"let HEALTH_QUANTITY_TYPES.*?=\s*\[(.+?)\]",
        swift, re.DOTALL)
    assert block, "couldn't find HEALTH_QUANTITY_TYPES in setup.sh"
    swift_types = set(re.findall(r'"(\w+)":\s*\(', block.group(1)))
    assert swift_types == set(HEALTH_VALID_TYPES), (
        f"drift: setup.sh declares {swift_types}, "
        f"sibb_state declares {set(HEALTH_VALID_TYPES)}"
    )


# ─────────────────────────── HealthSample spec ───────────────────────

def test_health_sample_spec_registered():
    assert ("Health", "sample") in SPEC_TYPES
    assert SPEC_TYPES[("Health", "sample")] is HealthSample


def test_health_sample_minimal_construction():
    s = HealthSample(sample_type="step_count", value=5000,
                      start_iso="2026-05-16T08:00:00Z")
    assert s.sample_type == "step_count"
    assert s.value == 5000
    assert s.end_iso is None


def test_health_sample_to_dict_canonical_shape():
    s = HealthSample(sample_type="body_mass", value=70.5,
                      start_iso="2026-05-16T07:00:00Z",
                      end_iso="2026-05-16T07:00:00Z")
    assert s.to_dict() == {
        "app": "Health", "type": "sample",
        "sample_type": "body_mass",
        "value": 70.5,
        "start_iso": "2026-05-16T07:00:00Z",
        "end_iso": "2026-05-16T07:00:00Z",
    }


def test_health_sample_round_trip():
    original = HealthSample(sample_type="heart_rate", value=72,
                              start_iso="2026-05-16T08:00:00Z")
    back = HealthSample.from_dict(original.to_dict())
    assert back == original


def test_validate_entry_accepts_health_sample():
    typed, err = validate_entry({
        "app": "Health", "type": "sample",
        "sample_type": "step_count", "value": 1000,
        "start_iso": "2026-05-16T08:00:00Z",
    })
    assert err is None
    assert isinstance(typed, HealthSample)


# ─────────────────────────── apply() dispatch ────────────────────────

async def test_apply_sample_sends_create_health_sample():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = HealthHandler(reader=r)
    await h.apply({"type": "sample",
                    "sample_type": "step_count",
                    "value": 5000,
                    "start_iso": "2026-05-16T08:00:00Z"})
    last = r.history[-1]
    assert last["request"]["type"] == "create_health_sample"
    assert last["request"]["sample_type"] == "step_count"
    assert last["request"]["value"] == 5000
    assert last["request"]["start_iso"] == "2026-05-16T08:00:00Z"
    assert "end_iso" not in last["request"]
    assert last["response"]["ok"] is True


async def test_apply_sample_forwards_end_iso_when_specified():
    """Step-count and heart-rate samples have a non-trivial duration
    (e.g. steps over a 10-min window). end_iso must reach Swift."""
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = HealthHandler(reader=r)
    await h.apply({"type": "sample",
                    "sample_type": "step_count",
                    "value": 5000,
                    "start_iso": "2026-05-16T08:00:00Z",
                    "end_iso":   "2026-05-16T08:10:00Z"})
    req = r.history[-1]["request"]
    assert req["end_iso"] == "2026-05-16T08:10:00Z"


async def test_apply_sample_rejects_unknown_sample_type():
    class _Noop:
        async def _send(self, cmd):
            return {"ok": True}
    h = HealthHandler(reader=_Noop())
    with pytest.raises(ValueError, match="unknown sample_type"):
        await h.apply({"type": "sample",
                        "sample_type": "blood_pressure",
                        "value": 120,
                        "start_iso": "2026-05-16T08:00:00Z"})


async def test_apply_sample_requires_value():
    class _Noop:
        async def _send(self, cmd):
            return {"ok": True}
    h = HealthHandler(reader=_Noop())
    with pytest.raises(ValueError, match="value is required"):
        await h.apply({"type": "sample",
                        "sample_type": "step_count",
                        "start_iso": "2026-05-16T08:00:00Z"})


async def test_apply_sample_requires_start_iso():
    class _Noop:
        async def _send(self, cmd):
            return {"ok": True}
    h = HealthHandler(reader=_Noop())
    with pytest.raises(ValueError, match="start_iso is required"):
        await h.apply({"type": "sample",
                        "sample_type": "step_count",
                        "value": 1000})


async def test_apply_rejects_unknown_entry_kind():
    class _Noop:
        async def _send(self, cmd):
            return {"ok": True}
    h = HealthHandler(reader=_Noop())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "workout"})


async def test_apply_raises_on_socket_error():
    class FailingReader:
        async def _send(self, cmd):
            return {"ok": False, "error": "no health permission"}
    h = HealthHandler(reader=FailingReader())
    with pytest.raises(RuntimeError, match="no health permission"):
        await h.apply({"type": "sample",
                        "sample_type": "step_count",
                        "value": 1000,
                        "start_iso": "2026-05-16T08:00:00Z"})


# ─────────────────────────── reset() ─────────────────────────────────

async def test_reset_calls_wipe_health_samples():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    h = HealthHandler(reader=r)
    await h.apply({"type": "sample", "sample_type": "step_count",
                    "value": 1000,
                    "start_iso": "2026-05-16T08:00:00Z"})
    await h.apply({"type": "sample", "sample_type": "heart_rate",
                    "value": 72,
                    "start_iso": "2026-05-16T08:00:00Z"})
    await h.reset()
    resp = await r._send({"type": "list_health_samples"})
    assert resp["samples"] == []


async def test_reset_raises_on_socket_error():
    class FailingReader:
        async def _send(self, cmd):
            return {"ok": False, "error": "no health permission"}
    h = HealthHandler(reader=FailingReader())
    with pytest.raises(RuntimeError, match="no health permission"):
        await h.reset()


# ─────────────────────────── fake reader CRUD ────────────────────────

async def test_fake_reader_create_round_trip():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_health_sample",
                            "sample_type": "step_count",
                            "value": 5000,
                            "start_iso": "2026-05-16T08:00:00Z"})
    assert resp["ok"] is True
    assert resp["unit"] == "count"
    assert resp["identifier"].startswith("fake-health-")
    resp = await r._send({"type": "list_health_samples"})
    assert len(resp["samples"]) == 1
    s = resp["samples"][0]
    assert s["sample_type"] == "step_count"
    assert s["value"] == 5000.0
    assert s["unit"] == "count"
    assert s["source"] == "com.sibb.tests.xctrunner"


async def test_fake_reader_filters_by_sample_type():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    await r._send({"type": "create_health_sample",
                    "sample_type": "step_count", "value": 1000,
                    "start_iso": "2026-05-16T08:00:00Z"})
    await r._send({"type": "create_health_sample",
                    "sample_type": "heart_rate", "value": 72,
                    "start_iso": "2026-05-16T08:00:00Z"})
    await r._send({"type": "create_health_sample",
                    "sample_type": "body_mass", "value": 70.5,
                    "start_iso": "2026-05-16T07:00:00Z"})
    resp = await r._send({"type": "list_health_samples",
                            "sample_type": "heart_rate"})
    assert len(resp["samples"]) == 1
    assert resp["samples"][0]["sample_type"] == "heart_rate"


async def test_fake_reader_filters_by_time_window():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    for hour in (8, 10, 12):
        await r._send({"type": "create_health_sample",
                        "sample_type": "step_count", "value": 1000,
                        "start_iso": f"2026-05-16T{hour:02d}:00:00Z",
                        "end_iso":   f"2026-05-16T{hour:02d}:10:00Z"})
    resp = await r._send({"type": "list_health_samples",
                            "start_iso": "2026-05-16T09:00:00Z",
                            "end_iso":   "2026-05-16T11:00:00Z"})
    # Only the 10:00 sample falls inside the window.
    assert len(resp["samples"]) == 1
    assert resp["samples"][0]["start_iso"] == "2026-05-16T10:00:00Z"


async def test_fake_reader_rejects_unknown_sample_type():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_health_sample",
                            "sample_type": "blood_pressure",
                            "value": 120,
                            "start_iso": "2026-05-16T08:00:00Z"})
    assert resp["ok"] is False
    assert "unknown sample_type" in resp["error"]


async def test_fake_reader_rejects_non_numeric_value():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    resp = await r._send({"type": "create_health_sample",
                            "sample_type": "step_count",
                            "value": "lots",
                            "start_iso": "2026-05-16T08:00:00Z"})
    assert resp["ok"] is False
    assert "required" in resp["error"]


async def test_fake_reader_wipe_clears_samples():
    from fakes.fake_reader import FakeXCUITestReader
    r = FakeXCUITestReader()
    for i in range(3):
        await r._send({"type": "create_health_sample",
                        "sample_type": "step_count",
                        "value": 1000 + i,
                        "start_iso": f"2026-05-16T08:0{i}:00Z"})
    resp = await r._send({"type": "wipe_health_samples"})
    assert resp["ok"] is True
    assert resp["removed_samples"] == 3
    resp = await r._send({"type": "list_health_samples"})
    assert resp["samples"] == []


# ─────────────────────────── resource fetcher ────────────────────────

def test_health_samples_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "health.samples" in RESOURCE_FETCHERS


async def test_health_samples_fetcher_returns_rows():
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "create_health_sample",
                    "sample_type": "body_mass", "value": 70.5,
                    "start_iso": "2026-05-16T07:00:00Z"})
    fetcher = RESOURCE_FETCHERS["health.samples"]
    rows = await fetcher(r, {})
    assert len(rows) == 1
    assert rows[0]["sample_type"] == "body_mass"
    assert rows[0]["value"] == 70.5


async def test_fetcher_pushes_sample_type_filter_to_socket():
    """`sample_type` is a socket pushdown — narrower query for
    large stores. Fake mirrors the behavior so client-side tests
    catch the same shape Swift handles."""
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    await r._send({"type": "create_health_sample",
                    "sample_type": "step_count", "value": 1000,
                    "start_iso": "2026-05-16T08:00:00Z"})
    await r._send({"type": "create_health_sample",
                    "sample_type": "heart_rate", "value": 72,
                    "start_iso": "2026-05-16T08:00:00Z"})
    fetcher = RESOURCE_FETCHERS["health.samples"]
    rows = await fetcher(r, {"sample_type": "step_count"})
    assert len(rows) == 1
    assert rows[0]["sample_type"] == "step_count"


async def test_fetcher_pushes_time_window_to_socket():
    from fakes.fake_reader import FakeXCUITestReader
    from sibb_verify import RESOURCE_FETCHERS
    r = FakeXCUITestReader()
    for hour in (8, 10, 12):
        await r._send({"type": "create_health_sample",
                        "sample_type": "step_count", "value": 1000,
                        "start_iso": f"2026-05-16T{hour:02d}:00:00Z",
                        "end_iso":   f"2026-05-16T{hour:02d}:10:00Z"})
    fetcher = RESOURCE_FETCHERS["health.samples"]
    rows = await fetcher(r, {"start_iso": "2026-05-16T09:00:00Z",
                              "end_iso":   "2026-05-16T11:00:00Z"})
    assert len(rows) == 1
    assert rows[0]["start_iso"] == "2026-05-16T10:00:00Z"
