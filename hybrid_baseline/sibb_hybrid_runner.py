"""SIBB Hybrid baseline batch runner — mirror of sibb_api_runner and
sibb_ui_runner.

Drives the SAME 26-task slate (from sibb/api_baseline/classification.yaml)
through `sibb_hybrid_assistant.run_hybrid_episode` so a third-baseline
PASS rate + per-step action_split_ratio can be compared head-to-head
with API and UI numbers.

Lifecycle parallels the UI runner (which is the canonical AXReader
owner): boot sim, start one AXReader, inject across episodes,
healthcheck + recycle on BrokenPipe (CLEAR-style server-side
timeouts, etc.). The hybrid assistant uses BOTH the AXReader (for
UI verbs + AX-tree observations) and the API tool dispatcher (which
shares the underlying XCUITest socket via `reader._xcuitest`).

Run:
    python3 -m sibb.hybrid_baseline.sibb_hybrid_runner \\
        --udid <UDID> --provider gemini --model gemini-2.5-flash \\
        --seeds 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
import types
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "benchmark"))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "simulator"))

# Reuse the API runner's classification slate, TaskResult, and
# Table 4 aggregation so all three baselines share one schema.
from sibb.api_baseline.sibb_api_runner import (  # noqa: E402
    parse_classification_slate,
    select_tasks,
    TaskResult,
    SlateEntry,
    aggregate_table4,
    write_table4_csv,
)

# Reuse the UI runner's AXReader healthcheck + recycle — the hybrid
# uses the SAME reader+socket the UI side does, so the same
# Maestro/WDA flakiness recovery applies.
from sibb.benchmark.sibb_ui_runner import (  # noqa: E402
    _is_ax_reader_alive,
    _recycle_ax_reader,
    _PREVENTIVE_RECYCLE_EVERY_N,
    _HEALTHCHECK_TIMEOUT_S,
)

from sibb_episode import simctl_boot, simctl_wait_booted  # noqa: E402
from sibb_scaffold import AXReader  # noqa: E402

import sibb.hybrid_baseline.sibb_hybrid_assistant as HA  # noqa: E402


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_MAX_TURNS = 30      # UI scaffold canonical; hybrid can use either
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_LLM_MAX_RETRIES = 5
DEFAULT_BUNDLE = "com.apple.springboard"


# ---------------------------------------------------------------------------
# Hybrid-specific TaskResult extension (action split)
# ---------------------------------------------------------------------------


@dataclass
class HybridTaskResult(TaskResult):
    """Adds the per-episode action-split fields. Shape is otherwise
    identical to TaskResult so downstream stitching / aggregation
    works."""
    n_steps_ui: int = 0
    n_steps_api: int = 0
    action_split_ratio: float = 0.0


# ---------------------------------------------------------------------------
# Episode-args builder
# ---------------------------------------------------------------------------


def _build_episode_args(runner_args, entry: SlateEntry, seed: int,
                          log_dir: str,
                          inject_reader: Optional[Any] = None
                          ) -> types.SimpleNamespace:
    """Build the SimpleNamespace `run_hybrid_episode` expects."""
    return types.SimpleNamespace(
        udid=runner_args.udid,
        generator=entry.runner_key,
        seed=seed,
        bundle=runner_args.bundle,
        provider=runner_args.provider,
        model=runner_args.model,
        max_turns=runner_args.max_turns,
        max_tokens=runner_args.max_tokens,
        temperature=runner_args.temperature,
        llm_timeout=runner_args.llm_timeout,
        log_dir=log_dir,
        inject_reader=inject_reader,
    )


# ---------------------------------------------------------------------------
# Results writer
# ---------------------------------------------------------------------------


def _write_results_json(path: str, results: List[HybridTaskResult],
                          args: argparse.Namespace, ts: str) -> None:
    doc = {
        "started_at": ts,
        "scaffold": "hybrid_baseline",
        "provider": args.provider,
        "model": args.model,
        "task_filter": args.task_filter,
        "seeds": args.seeds,
        "n_results": len(results),
        "n_pass": sum(1 for r in results if r.passed),
        "results": [asdict(r) for r in results],
    }
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2)


# ---------------------------------------------------------------------------
# Main batch loop
# ---------------------------------------------------------------------------


async def run_corpus(args: argparse.Namespace) -> int:
    slate = parse_classification_slate()
    slate = select_tasks(slate, args.task_filter)
    seeds = [int(s) for s in args.seeds.split(",")]
    total = len(slate) * len(seeds)

    if args.list:
        print(f"Slate ({len(slate)} tasks × {len(seeds)} seeds "
                f"= {total} episodes):")
        for e in slate:
            for s in seeds:
                print(f"  {e.cls:>8}  {e.subset:>12}  "
                        f"{e.generator}  seed={s}")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = args.results_dir or os.path.join(
        _REPO_ROOT, "sibb", "api_baseline", "results",
        f"hybrid_run_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    results_path = os.path.join(run_dir, "results.json")
    table4_path = os.path.join(run_dir, "table4.csv")

    print(f"\n=== sibb_hybrid_runner ===")
    print(f"Slate:     {len(slate)} tasks  × {len(seeds)} seeds "
            f"= {total} episodes")
    print(f"Provider:  {args.provider} ({args.model})")
    print(f"Prompt:    {HA.SYSTEM_PROMPT_VERSION}")
    print(f"Run dir:   {run_dir}\n")

    # Boot sim + start the shared AXReader once.
    print(f"  → booting sim + AXReader (one-time, ~25s)...")
    import subprocess as _sp
    _sp.run(["open", "-a", "Simulator",
             "--args", "-CurrentDeviceUDID", args.udid],
            capture_output=True)
    await asyncio.sleep(2)
    reader = AXReader(args.udid)
    try:
        await reader.start(bundle_id=args.bundle)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ AXReader.start failed: {type(e).__name__}: {e}")
        return 2
    print(f"  → AXReader ready (bundle={args.bundle})\n")

    results: List[HybridTaskResult] = []
    hard_errors = 0
    recycles = 0
    completed = 0

    try:
        for entry in slate:
            for seed in seeds:
                completed += 1

                # --- Preventive recycle every N episodes -----------
                if (completed > 1
                        and completed % _PREVENTIVE_RECYCLE_EVERY_N == 1):
                    recycles += 1
                    print(f"  ↻ preventive recycle (every "
                            f"{_PREVENTIVE_RECYCLE_EVERY_N} episodes; "
                            f"recycle #{recycles})...")
                    try:
                        reader = await _recycle_ax_reader(
                            reader, args.udid, args.bundle)
                        print(f"  → recovered")
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ RECYCLE FAILED: "
                                f"{type(e).__name__}: {e}")
                        hard_errors += 1
                        return 2 if args.fail_on_error else 0

                # --- Healthcheck before episode --------------------
                if not await _is_ax_reader_alive(reader):
                    recycles += 1
                    print(f"  ⚠ XCUITest socket unreachable; "
                            f"recycling (recycle #{recycles})...")
                    try:
                        reader = await _recycle_ax_reader(
                            reader, args.udid, args.bundle)
                        print(f"  → recovered")
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ RECYCLE FAILED: "
                                f"{type(e).__name__}: {e}")
                        results.append(HybridTaskResult(
                            generator=entry.generator,
                            runner_key=entry.runner_key,
                            cls=entry.cls, subset=entry.subset,
                            seed=seed, passed=False,
                            turns_used=0, tool_calls_made=0,
                            cost_usd=0.0, truncated=False,
                            truncation_reason=None,
                            error=f"recycle_failed: {type(e).__name__}",
                            duration_s=0.0,
                            n_steps_ui=0, n_steps_api=0,
                            action_split_ratio=0.0,
                        ))
                        _write_results_json(results_path, results,
                                              args, ts)
                        write_table4_csv(
                            aggregate_table4(results),
                            pathlib.Path(table4_path))
                        hard_errors += 1
                        return 2 if args.fail_on_error else 0

                print(f"  [{completed:>3}/{total:<3}] {entry.cls:>8} "
                        f"{entry.subset:>10}  {entry.runner_key}  "
                        f"seed={seed}")
                ep_args = _build_episode_args(
                    args, entry, seed, run_dir, inject_reader=reader)
                t0 = time.monotonic()
                episode_error: Optional[str] = None
                exit_code = 1
                outcome = None
                try:
                    exit_code, outcome = await HA.run_hybrid_episode(
                        ep_args)
                except (Exception, SystemExit) as e:  # noqa: BLE001
                    print(f"        ✗ episode raised: "
                            f"{type(e).__name__}: {e}")
                    hard_errors += 1
                    episode_error = f"{type(e).__name__}: {e}"

                dur = time.monotonic() - t0
                # Pull hybrid-specific metrics off the outcome (stuffed
                # into __dict__ by run_hybrid_episode to avoid forking
                # EpisodeOutcome's dataclass).
                if outcome is not None:
                    n_ui = int(outcome.__dict__.get("n_steps_ui", 0))
                    n_api = int(outcome.__dict__.get("n_steps_api", 0))
                    asr = float(outcome.__dict__.get(
                        "action_split_ratio", 0.0))
                    results.append(HybridTaskResult(
                        generator=entry.generator,
                        runner_key=entry.runner_key,
                        cls=entry.cls, subset=entry.subset, seed=seed,
                        passed=outcome.passed,
                        turns_used=outcome.turns_used,
                        tool_calls_made=outcome.tool_calls_made,
                        cost_usd=outcome.cost_usd,
                        truncated=outcome.truncated,
                        truncation_reason=outcome.truncation_reason,
                        error=outcome.error,
                        duration_s=dur,
                        n_steps_ui=n_ui,
                        n_steps_api=n_api,
                        action_split_ratio=asr,
                    ))
                else:
                    results.append(HybridTaskResult(
                        generator=entry.generator,
                        runner_key=entry.runner_key,
                        cls=entry.cls, subset=entry.subset, seed=seed,
                        passed=False, turns_used=0, tool_calls_made=0,
                        cost_usd=0.0, truncated=False,
                        truncation_reason=None,
                        error=episode_error,
                        duration_s=dur,
                        n_steps_ui=0, n_steps_api=0,
                        action_split_ratio=0.0,
                    ))

                _write_results_json(results_path, results, args, ts)
                write_table4_csv(aggregate_table4(results),
                                  pathlib.Path(table4_path))

                # Post-episode recycle if it raised.
                if episode_error is not None:
                    recycles += 1
                    print(f"  ⚠ episode raised; recycling "
                            f"(recycle #{recycles})...")
                    try:
                        reader = await _recycle_ax_reader(
                            reader, args.udid, args.bundle)
                        print(f"  → recovered")
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ RECYCLE FAILED: "
                                f"{type(e).__name__}: {e}")
                        hard_errors += 1
                        return 2 if args.fail_on_error else 0
    finally:
        try:
            await reader.stop()
        except Exception as e:  # noqa: BLE001
            print(f"  (reader.stop suppressed: {e})")

    n_pass = sum(1 for r in results if r.passed)
    # Aggregate action_split across non-failed episodes for the
    # iOS GUI/API split.
    finite = [r for r in results
              if r.n_steps_ui + r.n_steps_api > 0]
    agg_ui = sum(r.n_steps_ui for r in finite)
    agg_api = sum(r.n_steps_api for r in finite)
    agg_total = agg_ui + agg_api
    agg_api_frac = (agg_api / agg_total) if agg_total else 0.0

    print(f"\nDone — {n_pass}/{len(results)} passed; "
            f"{hard_errors} hard errors; {recycles} recycles")
    print(f"      action split: api={agg_api}/{agg_total} "
            f"({100*agg_api_frac:.1f}%), ui={agg_ui}/{agg_total} "
            f"({100*(1-agg_api_frac):.1f}%)")
    print(f"results.json  →  {results_path}")
    print(f"table4.csv    →  {table4_path}")

    if args.fail_on_error and hard_errors > 0:
        return 2
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--udid", required=True,
                    help="Booted simulator UDID")
    p.add_argument("--provider", default=DEFAULT_PROVIDER)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--seeds", default="0",
                    help="Comma-separated seed list (default: 0)")
    p.add_argument("--task-filter", default="all",
                    help="'all', 'api_only', 'ui_only', a comma-"
                          "separated list of runner_keys, or a single "
                          "runner_key")
    p.add_argument("--bundle", default=DEFAULT_BUNDLE,
                    help=f"Initial bundle for AXReader (default: "
                          f"{DEFAULT_BUNDLE})")
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--llm-timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--results-dir", default=None,
                    help="Run dir. Defaults to sibb/api_baseline/"
                          "results/hybrid_run_<ts>/")
    p.add_argument("--list", action="store_true",
                    help="Print the slate and exit without running.")
    p.add_argument("--fail-on-error", action="store_true",
                    help="Exit non-zero if any episode raised.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    return asyncio.run(run_corpus(args))


if __name__ == "__main__":
    raise SystemExit(main())
