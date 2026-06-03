"""D1c — L2 sim test: real create / boot / shutdown / delete cycle.

Slow (~30-45s) because we genuinely boot a sim. Gated on the
existing `sim` marker; runs only when SIBB_UDID env var is set
(matches the convention from the other L2 tests, signalling
"sim-side test ops are allowed in this environment").
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.sim


_SIM_DIR = Path(__file__).resolve().parents[2] / "simulator"
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

import sibb_simctl  # noqa: E402


async def test_create_boot_shutdown_delete_lifecycle(sibb_udid: str):
    # sibb_udid is unused for the cycle itself (we create our own
    # sim) — it just acts as the env-permission gate.
    _ = sibb_udid

    runtime_id = sibb_simctl.find_ios_runtime_id()
    device_type_id = sibb_simctl.find_device_type_id("iPhone")
    assert runtime_id is not None, "no iOS runtime available"
    assert device_type_id is not None, "no iPhone device type available"

    udid = await sibb_simctl.simctl_create(
        "SIBB-D1c-Lifecycle-Test", device_type_id, runtime_id,
    )
    assert udid and len(udid) >= 36, f"unexpected UDID format: {udid!r}"
    try:
        # Boot + wait — actually exercises the simctl side.
        await sibb_simctl.simctl_boot(udid)
        await sibb_simctl.simctl_wait_booted(udid, timeout=60.0)

        # Boot again — must be idempotent.
        await sibb_simctl.simctl_boot(udid)

        # Shut down + verify.
        await sibb_simctl.simctl_shutdown(udid)
    finally:
        # Defensive cleanup — never leak a UDID.
        await sibb_simctl.simctl_shutdown(udid)
        await sibb_simctl.simctl_delete(udid)


async def test_ensure_runner_built_idempotent_when_already_built(
    sibb_udid: str,
):
    _ = sibb_udid
    # The existing test runner from prior episode work should already
    # be present. ensure_runner_built must detect this and return
    # immediately — no setup.sh invocation, no temp sim created.
    if sibb_simctl.find_xctestrun_path() is None:
        pytest.skip("test runner not built on this host; "
                    "this test asserts the no-op path")
    import time
    t0 = time.time()
    await sibb_simctl.ensure_runner_built()
    elapsed = time.time() - t0
    # Idempotent path should be near-instant (<1s); 5s is generous.
    assert elapsed < 5.0, (
        f"ensure_runner_built took {elapsed:.1f}s when build was already "
        "present — expected ~immediate return"
    )
