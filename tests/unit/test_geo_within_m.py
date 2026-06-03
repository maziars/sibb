"""L1 tests for the `geo_within_m` check kind + haversine helper
(landed 2026-05-31 in sibb_verify.py for variant D / E destination
verification).

The check asserts the agent's actual Maps active-route destination
coord is within `radius_m` of the expected coord (typically 50 m).
Expected coords come from MKLocalSearch run on the same simulator
the agent uses, so SDK-build drift is zero and the residual
variance is just the agent's query-string fuzz.

Haversine is the canonical great-circle distance formula on a
sphere — accurate to ~0.5 % globally, which is well under our
50 m verifier threshold.
"""
from __future__ import annotations
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_verify import _haversine_m, _check_geo_within_m  # noqa: E402


# ── Haversine math ───────────────────────────────────────────────────

def test_haversine_zero_distance():
    """Identical coords → 0 m."""
    d = _haversine_m(37.794199, -122.394911, 37.794199, -122.394911)
    assert d == 0.0


def test_haversine_short_distance_sf_blocks():
    """100 Market St (37.794199,-122.394911) ↔ 250 Howard St
    (37.790217,-122.394261) — a few SoMa blocks south. Should be
    ~440 m on the ground."""
    d = _haversine_m(37.794199, -122.394911,
                      37.790217, -122.394261)
    assert 400 < d < 500, f"expected ~440 m, got {d:.1f} m"


def test_haversine_cross_continent():
    """SF (37.79,-122.39) ↔ NYC (40.75,-73.99) — should be ~4130 km."""
    d = _haversine_m(37.794199, -122.394911,
                      40.748238,  -73.985058)
    km = d / 1000.0
    assert 4100 < km < 4150, f"expected ~4130 km, got {km:.0f} km"


def test_haversine_symmetric():
    """Distance is symmetric — d(A,B) == d(B,A) within float tolerance."""
    a = _haversine_m(40.748238, -73.985058, 47.610438, -122.343100)
    b = _haversine_m(47.610438, -122.343100, 40.748238, -73.985058)
    assert abs(a - b) < 1e-6


# ── _check_geo_within_m handler ──────────────────────────────────────

def _record(lat: float, lon: float):
    """Build a maps.active_route-shaped record with `destination` nested."""
    return {
        "is_activated": True,
        "destination": {"name": "x", "address_lines": [],
                         "lat": lat, "lon": lon},
    }


def _check(lat: float, lon: float, radius_m: float = 50.0):
    return {"kind": "geo_within_m",
            "resource": "maps.active_route",
            "lat": lat, "lon": lon, "radius_m": radius_m}


def test_pass_exact_match():
    status, evidence = _check_geo_within_m(
        [_record(37.794199, -122.394911)],
        _check(37.794199, -122.394911))
    assert status == "pass"
    assert evidence["distance_m"] == 0.0


def test_pass_within_radius():
    """Two coords ~10 m apart, 50 m threshold → pass."""
    # 0.0001 degree ≈ 11 m at this latitude
    status, evidence = _check_geo_within_m(
        [_record(37.794299, -122.394911)],
        _check(37.794199, -122.394911, radius_m=50.0))
    assert status == "pass", evidence
    assert 5 < evidence["distance_m"] < 25


def test_fail_outside_radius():
    """Two coords ~440 m apart, 50 m threshold → fail."""
    status, evidence = _check_geo_within_m(
        [_record(37.790217, -122.394261)],  # 250 Howard St
        _check(37.794199, -122.394911, radius_m=50.0))  # 100 Market St
    assert status == "fail"
    assert evidence["distance_m"] > 400


def test_fail_no_records():
    """Empty record list → fail with a clear evidence trail."""
    status, evidence = _check_geo_within_m([], _check(37.7, -122.4))
    assert status == "fail"
    assert "no record matched" in evidence["error"]


def test_fail_multiple_records():
    """More than one matching record means the selector wasn't
    tight enough — refuse rather than picking arbitrarily."""
    status, evidence = _check_geo_within_m(
        [_record(37.79, -122.39), _record(40.74, -73.98)],
        _check(37.7, -122.4))
    assert status == "fail"
    assert "exactly one" in evidence["error"]


def test_fail_missing_destination_coords():
    """Record's destination dict has no lat/lon — graceful fail."""
    bad = {"is_activated": True, "destination": {"name": "x"}}
    status, evidence = _check_geo_within_m([bad], _check(37.7, -122.4))
    assert status == "fail"
    assert "no usable destination" in evidence["error"]


def test_fail_destination_is_none():
    """Record's `destination` is None (no plist active-nav)."""
    bad = {"is_activated": True, "destination": None}
    status, evidence = _check_geo_within_m([bad], _check(37.7, -122.4))
    assert status == "fail"


def test_raises_when_missing_required_keys():
    """`lat`, `lon`, `radius_m` are required — raise ValueError if any
    is missing so the harness reports a clear `error` rather than a
    silent false-pass."""
    import pytest
    for missing in ("lat", "lon", "radius_m"):
        check = _check(37.7, -122.4)
        del check[missing]
        with pytest.raises(ValueError):
            _check_geo_within_m([_record(0, 0)], check)


def test_raises_when_check_values_non_numeric():
    """Type-check the check values — strings or None should raise."""
    import pytest
    bad = _check(37.7, -122.4)
    bad["radius_m"] = "fifty"  # type: ignore
    with pytest.raises(ValueError):
        _check_geo_within_m([_record(0, 0)], bad)


# ── Registration: kind appears in CHECK_KINDS dispatch table ─────────

def test_kind_registered():
    from sibb_verify import CHECK_KINDS
    assert "geo_within_m" in CHECK_KINDS
    assert CHECK_KINDS["geo_within_m"] is _check_geo_within_m


# ── Real-corpus coords sanity ─────────────────────────────────────────

def test_corpus_addresses_distinct_enough():
    """Every (lat,lon) in the _MESSAGE_ADDRESSES corpus must be at
    least 100 m from every other entry. If two entries collided
    (e.g. someone edited the corpus and accidentally pointed two
    cities at the same coord), 50 m threshold would let the agent
    pass by navigating to the wrong address."""
    sys.path.insert(0, os.path.abspath(
        os.path.join(THIS_DIR, "..", "..", "benchmark")))
    from sibb_task_generator_v3 import _MESSAGE_ADDRESSES
    n = len(_MESSAGE_ADDRESSES)
    for i in range(n):
        for j in range(i + 1, n):
            lat_i, lon_i = _MESSAGE_ADDRESSES[i][4], _MESSAGE_ADDRESSES[i][5]
            lat_j, lon_j = _MESSAGE_ADDRESSES[j][4], _MESSAGE_ADDRESSES[j][5]
            d = _haversine_m(lat_i, lon_i, lat_j, lon_j)
            assert d > 100, (
                f"entries {i} and {j} are only {d:.0f} m apart — "
                f"too close for the 50 m verifier threshold to "
                f"distinguish them; pick distinct addresses")
