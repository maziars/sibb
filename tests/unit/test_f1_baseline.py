"""F1 — baseline+clone unit tests.

L1 layer: fingerprint determinism, name canonicalization, clone
retry semantics, and source-lint that `sibb_episode.py` has shed
its old per-episode prewarm path.
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.fast


# ────────────────────────── Fingerprint ───────────────────────────────

def test_baseline_fingerprint_is_deterministic():
    """Same prewarm.sh + runtime id → same fingerprint, every time.
    Without this, every `ensure_baseline_sim` call would rebuild —
    defeating F1's entire point.
    """
    from sibb_baseline import baseline_fingerprint
    rt = "com.apple.CoreSimulator.SimRuntime.iOS-26-3"
    assert baseline_fingerprint(rt) == baseline_fingerprint(rt)


def test_baseline_fingerprint_changes_with_runtime():
    from sibb_baseline import baseline_fingerprint
    fp_a = baseline_fingerprint(
        "com.apple.CoreSimulator.SimRuntime.iOS-26-3")
    fp_b = baseline_fingerprint(
        "com.apple.CoreSimulator.SimRuntime.iOS-26-4")
    assert fp_a != fp_b, (
        "fingerprint must differ between iOS runtimes — otherwise "
        "26.3 and 26.4 baselines would collide"
    )


def test_baseline_fingerprint_changes_when_prewarm_sh_changes(tmp_path,
                                                              monkeypatch):
    """If `sibb_prewarm.sh` content changes, the fingerprint must flip
    so the old baseline gets swept and a fresh one built.
    """
    import sibb_baseline
    fake = tmp_path / "fake_prewarm.sh"
    fake.write_text("#!/bin/bash\necho v1\n")
    monkeypatch.setattr(sibb_baseline, "PREWARM_SCRIPT", fake)
    rt = "com.apple.CoreSimulator.SimRuntime.iOS-26-3"
    fp_v1 = sibb_baseline.baseline_fingerprint(rt)
    fake.write_text("#!/bin/bash\necho v2\n")
    fp_v2 = sibb_baseline.baseline_fingerprint(rt)
    assert fp_v1 != fp_v2


def test_baseline_fingerprint_changes_when_setup_sh_changes(tmp_path,
                                                            monkeypatch):
    """`sibb_xcuitest_setup.sh` contains the Swift dismiss_app_onboarding
    logic invoked at baseline build time. When that changes (new
    dismiss labels, dispatch tweaks), the existing baseline's
    dismissal state may not match the current runner — fingerprint
    must flip so a fresh baseline gets built.
    """
    import sibb_baseline
    fake_prewarm = tmp_path / "fake_prewarm.sh"
    fake_prewarm.write_text("#!/bin/bash\necho prewarm\n")
    fake_setup = tmp_path / "fake_setup.sh"
    fake_setup.write_text("#!/bin/bash\necho v1\n")
    monkeypatch.setattr(sibb_baseline, "PREWARM_SCRIPT", fake_prewarm)
    monkeypatch.setattr(sibb_baseline, "SETUP_SCRIPT", fake_setup)
    rt = "com.apple.CoreSimulator.SimRuntime.iOS-26-3"
    fp_v1 = sibb_baseline.baseline_fingerprint(rt)
    fake_setup.write_text("#!/bin/bash\necho v2\n")
    fp_v2 = sibb_baseline.baseline_fingerprint(rt)
    assert fp_v1 != fp_v2


def test_baseline_fingerprint_handles_missing_prewarm(tmp_path,
                                                      monkeypatch):
    """Unreadable prewarm.sh shouldn't crash — fall back to a stable
    placeholder string and let the actual build raise a useful error.
    """
    import sibb_baseline
    missing = tmp_path / "does-not-exist.sh"
    monkeypatch.setattr(sibb_baseline, "PREWARM_SCRIPT", missing)
    monkeypatch.setattr(sibb_baseline, "SETUP_SCRIPT", missing)
    rt = "com.apple.CoreSimulator.SimRuntime.iOS-26-3"
    fp1 = sibb_baseline.baseline_fingerprint(rt)
    fp2 = sibb_baseline.baseline_fingerprint(rt)
    assert fp1 == fp2  # still deterministic


# ────────────────────────── baseline_name ─────────────────────────────

def test_baseline_name_uses_short_runtime_label():
    from sibb_baseline import baseline_name
    name = baseline_name("com.apple.CoreSimulator.SimRuntime.iOS-26-3")
    assert name.startswith("SIBB-Baseline-26.3-")


def test_baseline_name_length_includes_fingerprint():
    """Name format: SIBB-Baseline-<rt>-<8char-fingerprint>. Lock in
    the 8-char suffix so any sim with this prefix is unambiguously
    a baseline (not e.g. SIBB-Baseline-26.3-old)."""
    from sibb_baseline import baseline_name
    name = baseline_name("com.apple.CoreSimulator.SimRuntime.iOS-26-3")
    # SIBB-Baseline-26.3-<8 chars>
    assert len(name.rsplit("-", 1)[-1]) == 8


# ───────────────────── simctl_clone retry semantics ───────────────────

async def test_simctl_clone_retries_on_failure(monkeypatch):
    """Clone is flaky on the same machine (Apple Forum #713921). The
    wrapper retries up to N times before raising.
    """
    import sibb_simctl
    attempts = []

    async def fake_run_simctl(*args, **kwargs):
        attempts.append(args)
        if len(attempts) < 3:
            return 1, "", "Failed to clone device"
        return 0, "NEW-UDID", ""

    monkeypatch.setattr(sibb_simctl, "_run_simctl", fake_run_simctl)
    udid = await sibb_simctl.simctl_clone(
        "BASE-UDID", "clone-name", retries=3)
    assert udid == "NEW-UDID"
    assert len(attempts) == 3


async def test_simctl_clone_raises_after_retries_exhausted(monkeypatch):
    import sibb_simctl
    calls = []

    async def fake_run_simctl(*args, **kwargs):
        calls.append(args)
        return 1, "", "Failed to clone device"

    monkeypatch.setattr(sibb_simctl, "_run_simctl", fake_run_simctl)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await sibb_simctl.simctl_clone(
            "BASE-UDID", "clone-name", retries=3)
    assert len(calls) == 3


async def test_simctl_clone_returns_immediately_on_success(monkeypatch):
    import sibb_simctl
    calls = []

    async def fake_run_simctl(*args, **kwargs):
        calls.append(args)
        return 0, "CLONE-UDID-123", ""

    monkeypatch.setattr(sibb_simctl, "_run_simctl", fake_run_simctl)
    udid = await sibb_simctl.simctl_clone("BASE", "name")
    assert udid == "CLONE-UDID-123"
    assert len(calls) == 1


# ───────────────────── episode runner source-lint ─────────────────────

def test_episode_runner_no_longer_calls_run_prewarm():
    """The old per-episode prewarm path (`_run_prewarm(udid)` inside
    `run_episode_scripted`) MUST be gone post-F1 — its only legitimate
    home is `sibb_baseline._run_prewarm`, used during baseline build.

    A regression that puts prewarm back into the episode hot path
    would undo all F1 wins (the 3-5× speedup and the parallel-scale
    bottleneck removal).
    """
    src = pathlib.Path("sibb/benchmark/sibb_episode.py").read_text()
    func_idx = src.find("async def run_episode_scripted(")
    assert func_idx > 0
    func_end = src.find("\nasync def ", func_idx + 1)
    if func_end < 0:
        func_end = src.find("\ndef _", func_idx + 1)
    func_body = src[func_idx:func_end if func_end > 0 else len(src)]
    assert "_run_prewarm" not in func_body, (
        "run_episode_scripted must NOT call _run_prewarm — F1 moved "
        "prewarm into the baseline build (sibb_baseline.py)"
    )


def test_episode_runner_no_longer_calls_ensure_runner_permissions():
    """Same rationale as the prewarm lint — TCC grants now happen
    once in `ensure_baseline_sim`, not per-episode.
    """
    src = pathlib.Path("sibb/benchmark/sibb_episode.py").read_text()
    func_idx = src.find("async def run_episode_scripted(")
    func_end = src.find("\nasync def ", func_idx + 1)
    if func_end < 0:
        func_end = src.find("\ndef _", func_idx + 1)
    func_body = src[func_idx:func_end if func_end > 0 else len(src)]
    assert "ensure_runner_permissions" not in func_body, (
        "run_episode_scripted must NOT call ensure_runner_permissions "
        "directly — F1 baked TCC grants into the baseline"
    )


def test_baseline_module_imports_in_sibb_episode():
    """acquire_clone + ensure_baseline_sim + release_clone must be
    imported at module top — lazy imports would defeat the source-lint
    above (they'd silently re-add a prewarm path that pytest never sees).
    """
    src = pathlib.Path("sibb/benchmark/sibb_episode.py").read_text()
    # Bound by the first top-level function/class (any line starting
    # with `def ` or `async def ` or `class ` at column 0). Splitting
    # on a bare `def ` would land inside the module docstring (which
    # contains `async def agent_fn(...)` as an example).
    import re
    m = re.search(r"^(async def |def |class )", src, re.MULTILINE)
    module_top = src[:m.start()] if m else src
    assert "from sibb_baseline import" in module_top
    for name in ("acquire_clone", "ensure_baseline_sim", "release_clone"):
        assert name in module_top, (
            f"{name} must be top-level imported in sibb_episode"
        )


# ─────────────────── release_clone is best-effort ─────────────────────

async def test_release_clone_swallows_shutdown_failure(monkeypatch):
    """Teardown must never propagate — leaking a clone is preferable
    to masking the episode's actual outcome with a cleanup error.
    """
    import sibb_baseline

    async def boom(udid, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(sibb_baseline, "simctl_shutdown", boom)
    monkeypatch.setattr(sibb_baseline, "simctl_delete", boom)
    # Must not raise.
    await sibb_baseline.release_clone("FAKE-UDID")
