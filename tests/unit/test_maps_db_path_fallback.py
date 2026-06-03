"""L1 test for `_mapsdb_path` filesystem-walk fallback.

Discovered 2026-05-28 during variant D validation: the verifier
runs after the agent declares DONE; by then the sim has been shut
down (depending on the harness shutdown policy). `simctl
get_app_container` fails with "Unable to lookup in current state:
Shutdown", `_mapsdb_path` returned None, `_maps_history` returned []
silently, and the verifier false-failed even though the agent's
committed-route row was on disk.

Fix: when simctl is unavailable, walk the device's
`Containers/Data/Application/*/Library/Maps/MapsSync_0.0.1`
directory tree and return the first match. Each app installs into
its own UUID-named subdir; Maps' container is the one with the
MapsSync file.

These tests use a temp directory mimicking the simulator's layout
to validate the resolver without needing a real sim.
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_state import _mapsdb_path  # noqa: E402


def _build_sim_layout(root: Path, udid: str,
                       maps_uuid: str = "94A1BC86-MAPS-CONTAINER",
                       other_uuid: str = "11111111-SOME-OTHER-APP",
                       write_maps_db: bool = True) -> str:
    """Construct a directory tree mimicking the iOS simulator's
    Containers/Data/Application layout. Returns the simulator root
    for use with the mocked HOME."""
    device_data = (root / "Library" / "Developer" / "CoreSimulator"
                   / "Devices" / udid / "data")
    apps_root = device_data / "Containers" / "Data" / "Application"
    apps_root.mkdir(parents=True)
    # Maps' container with the DB
    maps_dir = apps_root / maps_uuid / "Library" / "Maps"
    maps_dir.mkdir(parents=True)
    if write_maps_db:
        (maps_dir / "MapsSync_0.0.1").write_bytes(b"\x00" * 32)
    # Another app's container WITHOUT a Maps DB (must be ignored)
    other = apps_root / other_uuid / "Library" / "Reminders"
    other.mkdir(parents=True)
    (other / "Reminders.sqlite").write_bytes(b"\x00" * 16)
    return str(device_data)


def test_fallback_returns_maps_db_when_simctl_unavailable():
    """simctl fails (mocked to return non-zero) → resolver walks
    the filesystem and finds MapsSync_0.0.1 under the Maps container."""
    udid = "FAKE-UDID-AAAA-BBBB-CCCC-000000000001"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_sim_layout(root, udid)
        # Mock os.path.expanduser to point at our temp root
        with patch.dict(os.environ, {"HOME": str(root)}):
            # Mock subprocess.run so simctl looks "unavailable"
            with patch("sibb_state.subprocess.run") as fake_run:
                class _R:
                    returncode = 1
                    stdout = ""
                fake_run.return_value = _R()
                path = _mapsdb_path(udid)
        assert path is not None
        assert path.endswith("Library/Maps/MapsSync_0.0.1")
        assert "94A1BC86-MAPS-CONTAINER" in path


def test_fallback_returns_none_when_maps_never_installed():
    """No Maps container directory at all → None (not crash)."""
    udid = "FAKE-UDID-AAAA-BBBB-CCCC-000000000002"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_sim_layout(root, udid, write_maps_db=False)
        # Remove the Maps dir entirely
        maps_dir = (root / "Library" / "Developer" / "CoreSimulator"
                    / "Devices" / udid / "data" / "Containers" / "Data"
                    / "Application" / "94A1BC86-MAPS-CONTAINER" / "Library"
                    / "Maps")
        if maps_dir.exists():
            for f in maps_dir.iterdir():
                f.unlink()
            maps_dir.rmdir()
        with patch.dict(os.environ, {"HOME": str(root)}):
            with patch("sibb_state.subprocess.run") as fake_run:
                class _R:
                    returncode = 1
                    stdout = ""
                fake_run.return_value = _R()
                path = _mapsdb_path(udid)
        assert path is None


def test_simctl_path_wins_when_available():
    """When simctl returns a valid container path AND the file
    exists there, the resolver returns it without falling back to
    the walk. (Faster, and accurate even if multiple Maps versions
    coexist for some reason.)"""
    udid = "FAKE-UDID-AAAA-BBBB-CCCC-000000000003"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sim_data = _build_sim_layout(root, udid)
        container = os.path.join(
            sim_data, "Containers", "Data", "Application",
            "94A1BC86-MAPS-CONTAINER")
        with patch.dict(os.environ, {"HOME": str(root)}):
            with patch("sibb_state.subprocess.run") as fake_run:
                class _R:
                    returncode = 0
                    stdout = container + "\n"
                fake_run.return_value = _R()
                path = _mapsdb_path(udid)
        # Should match the container-prefixed path (could be either
        # simctl-resolved or fallback-resolved; both point at the
        # same file)
        assert path is not None
        assert path.endswith("Library/Maps/MapsSync_0.0.1")


def test_simctl_returns_path_but_file_missing_falls_back_to_walk():
    """Edge case: simctl returns a container path but the
    MapsSync_0.0.1 file isn't there (stale simctl cache, or Maps
    not yet exercised). Walk should still find it elsewhere."""
    udid = "FAKE-UDID-AAAA-BBBB-CCCC-000000000004"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_sim_layout(root, udid,
                            maps_uuid="DDDDDDDD-WALK-FOUND")
        with patch.dict(os.environ, {"HOME": str(root)}):
            with patch("sibb_state.subprocess.run") as fake_run:
                # simctl points at a NON-existent container
                class _R:
                    returncode = 0
                    stdout = "/nonexistent/path\n"
                fake_run.return_value = _R()
                path = _mapsdb_path(udid)
        assert path is not None
        assert "DDDDDDDD-WALK-FOUND" in path


def test_no_simulator_directory_returns_none():
    """If the sim directory doesn't exist at all (wrong UDID, or
    simctl never created the device), return None."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Don't build any sim layout
        with patch.dict(os.environ, {"HOME": str(root)}):
            with patch("sibb_state.subprocess.run") as fake_run:
                class _R:
                    returncode = 1
                    stdout = ""
                fake_run.return_value = _R()
                path = _mapsdb_path("BOGUS-UDID-DOESNT-EXIST")
        assert path is None
