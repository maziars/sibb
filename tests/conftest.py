"""
Shared pytest config for the SIBB test suite.

Mirrors the sys.path mutation done ad-hoc in sibb_replay.py and
sibb_state.py so test modules can `import sibb_state`,
`import sibb_xcuitest_client`, etc. without packaging the project.

This is the only file every test layer (unit / handler / integration /
e2e / contract) loads. Anything broader than path setup or session-
wide fixtures should NOT live here — split it into a layer-local
conftest.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_SIBB_ROOT = _TESTS_DIR.parent
_BENCHMARK = _SIBB_ROOT / "benchmark"
_SIMULATOR = _SIBB_ROOT / "simulator"

for _p in (_TESTS_DIR, _BENCHMARK, _SIMULATOR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture(scope="session")
def sibb_udid() -> str:
    udid = os.environ.get("SIBB_UDID")
    if not udid:
        pytest.skip("SIBB_UDID env var not set; skipping simulator-backed test")
    return udid


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
