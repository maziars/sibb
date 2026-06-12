"""
Programmatic episode runner — D1a.

A non-interactive variant of `sibb_episode_runner.run_episode` that
takes an agent callable instead of blocking on stdin. The runner
owns the full sim lifecycle (create → boot → runner → setup →
verify-before → agent loop → verify-after → teardown) so a parallel
orchestrator (D1b) can spawn N copies concurrently, each with its
own fresh simulator.

The agent callable signature is:

    async def agent_fn(tree: AXTree, task: Task, step_idx: int,
                       reader: AXReader) -> AgentAction

`reader` is plumbed through so test/scripted agents can call socket
commands directly when needed. LLM agents typically ignore it and
operate on `tree` alone, emitting AgentActions that `execute()` from
sibb_replay turns into XCUITest taps/types/swipes.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

_BENCHMARK_DIR = os.path.dirname(os.path.abspath(__file__))
_SIM_DIR = os.path.normpath(os.path.join(_BENCHMARK_DIR, "..", "simulator"))
for _p in (_BENCHMARK_DIR, _SIM_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sibb_baseline import (
    acquire_clone,
    ensure_baseline_sim,
    release_clone,
)
from sibb_refs import resolve_refs
from sibb_scaffold import AXReader, AXTree
from sibb_simctl import (
    ensure_runner_built,
    restart_springboard,
    simctl_boot,
    simctl_wait_booted,
    sweep_sibb_orphans,
)
from sibb_state import (
    apply_initial_state,
    apply_pre_runner_setup,
    canonicalize_app,
)
from sibb_verify import (
    BaselineSnapshot,
    CheckResult,
    RESOURCE_FETCHERS,
    blocking_pass,
    run_checks,
)


AgentFn = Callable[[AXTree, Any, int, AXReader], Awaitable[Any]]


class AbortEpisode(Exception):
    """Raised when an episode cannot continue (e.g. socket died).

    Distinguishes recoverable per-action failures (which the agent
    loop tolerates) from framework-level failures (which end the
    episode). The runner catches this and sets
    `EpisodeResult.final_status = "connection_lost"`.

    Use cases:
    - Reader socket gone (BrokenPipeError, ConnectionResetError)
    - xcodebuild test target crashed mid-episode
    - The Swift server returned `ok=false` with a fatal error class
      (TCC permission revoked, EventKit store gone, etc.)
    """


# Connection-level Python exceptions that imply the Swift server is
# unreachable. `_run_agent_loop` upgrades these to AbortEpisode.
_CONNECTION_FAILURE_EXC = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
    ConnectionRefusedError,
)


@dataclass
class EpisodeResult:
    task_id: str
    apps: List[str]
    udid: str
    passed_before: bool = False
    passed_after: bool = False
    checks_before: List[CheckResult] = field(default_factory=list)
    checks_after: List[CheckResult] = field(default_factory=list)
    agent_actions: List[Any] = field(default_factory=list)
    steps_taken: int = 0
    # "done" | "fail" | "max_steps" | "error" | "no_agent"
    final_status: str = "error"
    error: Optional[str] = None


async def run_episode_scripted(
    task,
    agent_fn: Optional[AgentFn],
    *,
    udid: Optional[str] = None,
    reader: Optional[AXReader] = None,
    device_type_substring: str = "iPhone 17",
    runtime_version: Optional[str] = None,
    max_steps: int = 50,
) -> EpisodeResult:
    """Run one episode end-to-end programmatically.

    Three resource-ownership modes, from fully-owned to fully-injected:

    1. `reader=None, udid=None` — own_sim: create + boot + prewarm a
       fresh simulator, build the runner, start the reader, run the
       episode, then tear all of that down. The path the parallel
       orchestrator (D1b) uses.

    2. `reader=None, udid=<X>` — own_reader: reuse an existing
       simulator (X is already booted + prewarmed); we spin up our
       own AXReader against it for this one episode and stop it at
       teardown. Useful for one-off scripts that already have a sim.

    3. `reader=<R>` — fully injected: caller manages BOTH sim and
       reader. We do setup → agent loop → verify against R; never
       call reader.start/stop. Use this for test suites that share
       one long-lived reader across many episodes — avoids the
       cross-process `requestAccess` rate-limit and the
       ~10-15s xcodebuild test-launch cost per episode.

    If `agent_fn` is None, the runner only does setup +
    verify-BEFORE + verify-AFTER. Useful for verifier development
    and for re-rolling tasks whose verify-BEFORE accidentally passes.
    """
    inject_reader = reader is not None
    own_sim = udid is None and reader is None
    own_reader = reader is None and udid is not None

    if inject_reader:
        # Caller fully manages reader lifecycle.
        udid = reader.udid

    if own_sim:
        # F1: ensure runner build + baseline sim both exist. Both are
        # idempotent — first call on a fresh machine pays the build
        # (~3 min) and baseline-prewarm (~3 min) costs; every call
        # after that is sub-second. Order doesn't matter — build and
        # baseline are independent artifacts.
        await ensure_runner_built(device_type_substring, runtime_version)
        baseline_udid, _ = await ensure_baseline_sim(
            device_type_substring, runtime_version)
        # Clone the baseline → boot the clone. ~15-25s vs the old
        # path's ~150-300s (create+boot+prewarm+shutdown+boot). The
        # clone inherits the baseline's TCC.db + dismissed first-run
        # dialogs + suppression keys, so no per-episode prewarm.
        udid = await acquire_clone(
            baseline_udid, label=(task.task_id or "anon"))

    result = EpisodeResult(
        task_id=task.task_id or "anon",
        apps=list(task.apps or []),
        udid=udid or "",
    )

    try:
        # Resolve SymbolicRef instances BEFORE the dispatcher sees
        # entries — handlers and verifiers consume pure strings.
        if hasattr(task, "initial_state") and task.initial_state.spec:
            task.initial_state.spec = resolve_refs(
                task.initial_state.spec)
        if getattr(task, "verify_checks", None):
            task.verify_checks = resolve_refs(task.verify_checks)

        # Pre-runner setup (Springboard layout/dock). Handles its
        # own shutdown/boot when entries are present; no-op otherwise.
        # Skipped when reader is injected (caller's responsibility).
        if not inject_reader:
            # apply_pre_runner_setup is sync (subprocess.run + time.sleep)
            # and takes ~15s when entries are present. In D1b's
            # asyncio.gather over N workers, calling it directly would
            # block the event loop and stall every other worker for
            # the whole shutdown/boot cycle. asyncio.to_thread pushes
            # the blocking call into the default executor.
            import asyncio
            pre_report = await asyncio.to_thread(
                apply_pre_runner_setup, udid, task)
            if pre_report["errors"]:
                result.error = (
                    "pre_runner errors: "
                    + "; ".join(pre_report["errors"])
                )
                result.final_status = "error"
                return result

            # If no pre_runner entries applied, sim wasn't booted.
            if not pre_report["applied"]:
                await simctl_boot(udid)
                await simctl_wait_booted(udid)
                # Settle window: SpringBoard finishes wiring AX queries
                # ~2s after Booted on warm/already-prewarmed sims.
                # (Fresh sims paid the ~30-60s prewarm cost upfront
                # right after simctl_create — no second prewarm here.)
                await asyncio.sleep(2.0)

            # SpringBoard restart: evicts per-bundle TCC cache so the
            # runner's first EKEventStore.requestAccess call sees a
            # fresh permission state. Pattern from wix/AppleSimulatorUtils
            # (production-proven via Detox). Critical under parallel
            # load — without this, SpringBoard's cached "no permission"
            # state for com.sibb.tests.xctrunner persists from initial
            # boot even after simctl privacy grant has updated TCC.db.
            # Placed here (last step before reader.start) so prewarm's
            # app launches aren't disrupted by a mid-flight SpringBoard
            # restart..
            if own_sim:
                await restart_springboard(udid)

            primary_bundle = (
                canonicalize_app((task.apps or ["Reminders"])[0])
                or "com.apple.springboard"
            )
            reader = AXReader(udid)
            await reader.start(bundle_id=primary_bundle)

        state_report = await apply_initial_state(reader._xcuitest, task)
        if state_report["errors"]:
            result.error = (
                "state setup errors: "
                + "; ".join(state_report["errors"])
            )
            result.final_status = "error"
            return result

        # Capture baseline for any `identity`-kind verify_checks.
        # The check resolves its before/after diff against this
        # snapshot — without it, identity checks return status="error"
        # at run_check time. We capture AFTER apply_initial_state so
        # the "baseline" reflects the task's INTENDED post-setup
        # state, not the raw pre-reset state.
        verify_checks = getattr(task, "verify_checks", None) or []
        baseline: Optional[BaselineSnapshot] = None
        baseline_resources = _baseline_resources_for(verify_checks)
        if baseline_resources:
            try:
                baseline = await BaselineSnapshot.capture(
                    reader._xcuitest, sorted(baseline_resources))
            except Exception as e:
                result.error = (
                    f"baseline capture failed: "
                    f"{type(e).__name__}: {e}"
                )
                result.final_status = "error"
                return result

        # Verifier-BEFORE.
        if verify_checks:
            result.checks_before = await run_checks(
                reader._xcuitest, verify_checks, baseline=baseline)
            result.passed_before = blocking_pass(result.checks_before)

        # Agent loop. agent_fn=None mode just runs verifier twice
        # with no actions in between — useful for sanity testing
        # the verifier-BEFORE vs verifier-AFTER delta.
        if agent_fn is None:
            result.final_status = "no_agent"
        else:
            await _run_agent_loop(
                reader, task, agent_fn, result, max_steps)

        # Verifier-AFTER.
        if verify_checks:
            result.checks_after = await run_checks(
                reader._xcuitest, verify_checks, baseline=baseline)
            result.passed_after = blocking_pass(result.checks_after)

    except AbortEpisode as e:
        # Connection-level abort: socket dead, runner crashed. The
        # episode is over but the failure is framework, not agent.
        # Verifier-AFTER would just re-fail on the same dead socket;
        # leave checks_after empty rather than chasing a phantom.
        result.error = str(e)
        result.final_status = "connection_lost"
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.final_status = "error"
    finally:
        # Only tear down resources WE created. An injected reader
        # belongs to the caller; we never stop it.
        if not inject_reader and reader is not None:
            try:
                await reader.stop()
            except Exception:
                pass
        if own_sim and udid:
            # Best-effort clone teardown; failures don't mask the
            # underlying result. The baseline itself is NOT deleted
            # — it's reused across episodes.
            await release_clone(udid)

    return result


def _baseline_resources_for(verify_checks) -> set:
    """Resource keys that need a baseline captured before the agent runs.

    Two reasons a check needs a baseline:
      (a) `kind == "identity"` — diff current vs baseline records.
      (b) The check's selector references the `$baseline_iso` sentinel
          (resolved to the baseline.captured_at ISO at check time).
          Today this is used by maps.history's `min_create_iso` to
          scope to "rows the agent wrote this episode."
    """
    out: set = set()
    for c in verify_checks or []:
        res = c.get("resource")
        if not res or res not in RESOURCE_FETCHERS:
            continue
        if c.get("kind") == "identity":
            out.add(res)
            continue
        selector = c.get("selector") or {}
        if any(v == "$baseline_iso" for v in selector.values()
               if isinstance(v, str)):
            out.add(res)
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  D1b — parallel orchestrator
# ─────────────────────────────────────────────────────────────────────────────


AgentFactory = Callable[[Any], Optional[AgentFn]]


async def run_episodes_parallel(
    tasks,
    agent_factory: Optional[AgentFactory] = None,
    *,
    concurrency: int = 4,
    device_type_substring: str = "iPhone 17",
    runtime_version: Optional[str] = None,
    max_steps: int = 50,
    sweep_at_start: bool = True,
) -> List[EpisodeResult]:
    """Run a batch of tasks in parallel, each on its own fresh simulator.

    Architecture:
      sweep_sibb_orphans()   sweeps leftover sims/tmp from prior crashes
      ensure_runner_built()  blocks until xcodebuild build-for-testing
                             has produced ~/SIBBHelper/build/ (runs ONCE
                             before workers spawn, no race)
      N workers              each loops on a shared asyncio.Queue;
                             per task, calls run_episode_scripted(udid=None)
                             which owns a fresh sim end-to-end

    `agent_factory(task) -> agent_fn` returns the agent for each task.
    Pass `None` to run all tasks in verifier-only mode (no agent loop;
    each result has `final_status="no_agent"`).

    Returns: List[EpisodeResult] in COMPLETION order (not task order).
    Each result carries its `task_id` so callers can re-key.

    Concurrency vs cost (F1, post baseline+clone):
    - Each worker holds one booted clone (~1-2 GB RAM each)
    - Practical cap: 4-8 on a 32 GB Mac before swapping
    - Per-episode prelude: ~50-100s (clone + boot + xcodebuild test
      launch). No more prewarm-per-worker, no more serialization.
    - First call on a fresh machine pays a one-time ~3 min baseline
      build cost in `ensure_baseline_sim()` — subsequent calls reuse
      that baseline.
    """
    task_list = list(tasks)
    if not task_list:
        return []

    if sweep_at_start:
        await sweep_sibb_orphans()

    # Build the runner + baseline once, BEFORE workers spawn. Both
    # are idempotent and have their own locks, but spawning workers
    # first would race them all at the build/baseline-prewarm with
    # extra overhead. Single up-front call is cleaner.
    await ensure_runner_built(device_type_substring, runtime_version)
    await ensure_baseline_sim(device_type_substring, runtime_version)

    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    for t in task_list:
        queue.put_nowait(t)

    results: List[EpisodeResult] = []
    results_lock = asyncio.Lock()

    async def worker(worker_id: int) -> None:
        while True:
            try:
                task = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                agent_fn = (agent_factory(task)
                             if agent_factory is not None else None)
                result = await run_episode_scripted(
                    task, agent_fn,
                    udid=None,    # own_sim per episode
                    device_type_substring=device_type_substring,
                    runtime_version=runtime_version,
                    max_steps=max_steps,
                )
            except Exception as e:
                # Worker-level exception (rare — run_episode_scripted
                # catches AbortEpisode + generic Exception internally).
                # Synthesize an error result so the task isn't silently
                # dropped from the report.
                result = EpisodeResult(
                    task_id=getattr(task, "task_id", None) or "anon",
                    apps=list(getattr(task, "apps", None) or []),
                    udid="",
                    final_status="error",
                    error=(
                        f"worker {worker_id}: "
                        f"{type(e).__name__}: {e}"
                    ),
                )
            async with results_lock:
                results.append(result)
            queue.task_done()

    # Don't spawn more workers than tasks — they'd just immediately
    # see an empty queue and return.
    n_workers = max(1, min(concurrency, len(task_list)))
    workers = [
        asyncio.create_task(worker(i)) for i in range(n_workers)
    ]
    await asyncio.gather(*workers, return_exceptions=True)
    return results


async def _run_agent_loop(
    reader: AXReader,
    task,
    agent_fn: AgentFn,
    result: EpisodeResult,
    max_steps: int,
) -> None:
    """Inner loop. Mutates `result` in place. Lazy-imports `execute`
    from sibb_replay to avoid the module-load cost on import.

    Raises `AbortEpisode` if a connection-level failure makes the
    episode unrecoverable (socket dead, xcodebuild crashed). The
    outer `run_episode_scripted` catches this and reports
    `final_status="connection_lost"`.
    """
    from sibb_replay import execute   # noqa: E402

    for step_idx in range(max_steps):
        try:
            tree = await reader.read()
        except _CONNECTION_FAILURE_EXC as e:
            raise AbortEpisode(
                f"reader.read() at step {step_idx}: "
                f"{type(e).__name__}: {e}"
            )

        try:
            action = await agent_fn(tree, task, step_idx, reader)
        except Exception as e:
            result.error = (
                f"agent_fn raised at step {step_idx}: "
                f"{type(e).__name__}: {e}"
            )
            result.final_status = "error"
            result.steps_taken = step_idx
            return

        result.agent_actions.append(action)

        if action.action_type in ("done", "fail"):
            result.final_status = action.action_type
            result.steps_taken = step_idx + 1
            return

        # Action execution may fail per-step without ending the
        # episode (agents re-observe and adapt). BUT connection-level
        # failures mean every subsequent step will also fail — abort
        # rather than grind through max_steps doomed iterations.
        try:
            await execute(reader, action, tree)
        except _CONNECTION_FAILURE_EXC as e:
            raise AbortEpisode(
                f"execute at step {step_idx}: "
                f"{type(e).__name__}: {e}"
            )
        except Exception:
            # Recoverable per-step failure (element disabled, target
            # off-screen, etc.) — agent re-observes next step.
            pass

    result.final_status = "max_steps"
    result.steps_taken = max_steps
