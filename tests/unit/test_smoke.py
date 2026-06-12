"""Pytest discovery smoke + import sanity + __main__-blocks intact.

If this file fails, the test skeleton itself is broken — fix it
before anything else.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.fast


def test_pytest_discovery_works():
    assert True


def test_can_import_sibb_modules():
    import sibb_state  # noqa: F401
    import sibb_xcuitest_client  # noqa: F401
    import sibb_task_generator_v3  # noqa: F401
    import sibb_verify_reminders  # noqa: F401


def test_can_import_fake_reader():
    from fakes.fake_reader import FakeXCUITestReader  # noqa: F401


# # the per-module __main__ smoke blocks; they're load-bearing for
# fast iteration. If a module drops the guard, this test catches it.
_MODULES_WITH_MAIN = [
    "sibb/benchmark/sibb_state.py",
    "sibb/benchmark/sibb_replay.py",
    "sibb/benchmark/sibb_inspect_screen.py",
    "sibb/benchmark/sibb_verify_reminders.py",
    "sibb/simulator/sibb_xcuitest_client.py",
    "sibb/simulator/sibb_randomize_layout.py",
]


def _project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("relpath", _MODULES_WITH_MAIN)
def test_main_smoke_block_intact(relpath: str):
    path = _project_root() / relpath
    assert path.exists(), f"module missing: {relpath}"
    content = path.read_text()
    assert 'if __name__ == "__main__":' in content, (
        f"{relpath} lost its __main__ smoke block — "
        "see CLAUDE.md memory on preserved iteration tooling"
    )
