"""UI baseline batch runner — mirror of sibb.api_baseline.sibb_api_runner.

Drives the SAME 26-task slate (from sibb/api_baseline/classification.yaml)
through `sibb_assistant.run_episode` so the UI-scaffold pass rate can be
compared head-to-head with the API-baseline pass rate at the same seed.

Lifecycle:
  1. Parse classification slate (shared with API baseline).
  2. Boot sim + start a single AXReader pointed at Springboard.
  3. For each (entry, seed):
       - Build a SimpleNamespace mirroring sibb_assistant.parse_args()
         output.
       - Inject the shared AXReader via args.inject_reader (added to
         sibb_assistant.run_episode in this same commit).
       - Call run_episode async; capture exit_code and the JSONL it
         wrote.
       - Parse the JSONL for turns_used / cost_usd / truncation.
  4. Aggregate into table4.csv + results.json using the API runner's
     helpers (single source of truth for the Table 4 shape).
  5. Stop the reader in finally.

The runner deliberately does NOT implement preventive XCUITest recycle
(unlike the API runner). The UI scaffold's per-episode observation
loop is more fault-tolerant — a single-episode failure rarely poisons
the next one. If sim flakiness becomes a problem here too, lift the
healthcheck + recycle helpers from sibb_api_runner.

Run:
    python3 -m sibb.benchmark.sibb_ui_runner \\
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
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO_ROOT / "sibb" / "simulator"))

# Pull the shared classification + Table 4 machinery from the API
# runner — single source of truth for the comparison.
from sibb.api_baseline.sibb_api_runner import (  # noqa: E402
    parse_classification_slate,
    select_tasks,
    TaskResult,
    SlateEntry,
    aggregate_table4,
    write_table4_csv,
)

import sibb_assistant as UA  # noqa: E402
from sibb_scaffold import AXReader  # noqa: E402
from sibb_episode import simctl_boot, simctl_wait_booted  # noqa: E402


# ---------------------------------------------------------------------------
# Recycle constants — mirror sibb_api_runner.py values
# ---------------------------------------------------------------------------

# Preventively recycle the XCUITest server every N episodes to limit the
# blast radius of slow-leak crashes (cf. WebDriverAgent #507).
# Tightened from 20 → 10 on 2026-06-11 — hybrid v1 had 1 mid-run
# TAP timeout at the every-20 cadence; halving the gap halves the
# cumulative-state buildup window. Cost: ~25s extra wall-clock per
# 26-task slate (one extra recycle); cheap relative to the cost of
# a mid-run cascade.
_PREVENTIVE_RECYCLE_EVERY_N = 10

# Socket-level healthcheck timeout: if the XCUITest server doesn't
# answer a ping within this many seconds, treat as dead and recycle.
_HEALTHCHECK_TIMEOUT_S = 3.0


# ---------------------------------------------------------------------------
# AXReader healthcheck + recycle
# ---------------------------------------------------------------------------
#
# The UI scaffold's CLEAR/SCROLL/TYPE verbs can hold the XCUITest socket
# for >30s on some Contacts-creation flows (observed
# 2026-06-11: CLEAR took 31s clearing a field, the server closed the
# socket on its end, every subsequent op got BrokenPipe). Without
# recycle, one slow command cascades into a whole-batch failure.


async def _is_ax_reader_alive(reader: AXReader,
                                timeout: float = _HEALTHCHECK_TIMEOUT_S
                                ) -> bool:
    """Returns True iff the AXReader's underlying XCUITest socket
    answers a ping within `timeout` seconds."""
    try:
        resp = await asyncio.wait_for(
            reader._xcuitest._send({"type": "ping"}),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — any failure = unhealthy
        return False
    return bool(resp.get("ok", False))


async def _recycle_ax_reader(old_reader: Optional[AXReader],
                                udid: str,
                                bundle: str) -> AXReader:
    """Tear down the old AXReader, recycle the sim, bring up a fresh
    AXReader. Mirrors `sibb_api_runner._recycle_reader` but creates
    an `AXReader` (the wrapper the UI scaffold uses) instead of a
    bare `XCUITestReader`.

    `simctl shutdown && boot` (not `erase`) drains the
    CoreSimulatorService state without paying cold-prewarm cost.
    """
    if old_reader is not None:
        try:
            await old_reader.stop()
        except Exception:  # noqa: BLE001 — best-effort
            pass
    import subprocess
    try:
        subprocess.run(["xcrun", "simctl", "shutdown", udid],
                       capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    # Quit Simulator.app between recycles to drop accumulated UI-side
    # state — CoreSimulatorService leaks across long batches and causes
    # `simctl boot` to slow-degrade across recycles. Added 2026-06-11
    # after hybrid v3b hit a fatal `simctl boot timed out` on its 3rd
    # recycle (Apple Developer Forum #713921, Maestro #3318).
    try:
        from sibb_simctl import simctl_quit_simulator_app
        await simctl_quit_simulator_app()
    except Exception:  # noqa: BLE001 — best-effort
        pass
    await asyncio.sleep(1.0)
    await simctl_boot(udid)
    await simctl_wait_booted(udid)
    await asyncio.sleep(2.0)
    new_reader = AXReader(udid)
    await new_reader.start(bundle_id=bundle)
    return new_reader


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "gemini"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_MAX_TURNS = 30      # UI baseline canonical (Reminders T4 used ~13)
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_LLM_MAX_RETRIES = 5
DEFAULT_BUNDLE = "com.apple.springboard"


# ---------------------------------------------------------------------------
# Episode-args builder
# ---------------------------------------------------------------------------


def _build_episode_args(runner_args, entry: SlateEntry, seed: int,
                          log_dir: str,
                          inject_reader: Optional[Any] = None
                          ) -> types.SimpleNamespace:
    """Build the SimpleNamespace `sibb_assistant.run_episode` expects.

    Mirrors `sibb_assistant.parse_args()` field set so we never have
    to spawn a subprocess.
    """
    return types.SimpleNamespace(
        udid=runner_args.udid,
        generator=entry.runner_key,
        seed=seed,
        bundle=runner_args.bundle,
        provider=runner_args.provider,
        model=runner_args.model,
        max_turns=runner_args.max_turns,
        max_seconds=runner_args.max_seconds,
        max_tokens=runner_args.max_tokens,
        max_elements=runner_args.max_elements,
        temperature=runner_args.temperature,
        llm_timeout=runner_args.llm_timeout,
        log_dir=log_dir,
        verbose=False,
        inject_reader=inject_reader,
    )


# ---------------------------------------------------------------------------
# JSONL post-mortem
# ---------------------------------------------------------------------------


def _parse_episode_jsonl(jsonl_path: str) -> Dict[str, Any]:
    """Extract metrics from the assistant's per-episode JSONL.

    Returns a dict with whatever's available; missing fields default
    to zero/None. Used to populate TaskResult fields the
    `run_episode` exit code doesn't carry.
    """
    if not os.path.isfile(jsonl_path):
        return {"turns_used": 0, "tool_calls_made": 0,
                "cost_usd": 0.0, "truncated": False,
                "truncation_reason": None}

    turns_used = 0
    tool_calls_made = 0
    cost_usd = 0.0
    truncated = False
    truncation_reason: Optional[str] = None
    with open(jsonl_path) as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "turn":
                turns_used += 1
                # Per-turn cost (LLMResponse.cost_usd) — additive.
                cost_usd += float(ev.get("cost_usd") or 0.0)
                # Each turn may emit ≥1 tool call; count them.
                tool_calls_made += len(ev.get("tool_calls") or []) or 1
            elif t == "truncated":
                truncated = True
                truncation_reason = ev.get("reason") or "max_turns"
    return {
        "turns_used": turns_used,
        "tool_calls_made": tool_calls_made,
        "cost_usd": cost_usd,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
    }


def _latest_jsonl_in(log_dir: str, generator: str,
                      provider: str, model: str) -> Optional[str]:
    """The UI assistant writes a timestamped JSONL per episode; find
    the most recent one matching this (generator, provider, model)."""
    pat = f"episode_{generator}_{provider}_{model}_"
    candidates = []
    if not os.path.isdir(log_dir):
        return None
    for fn in os.listdir(log_dir):
        if fn.startswith(pat) and fn.endswith(".jsonl"):
            candidates.append(os.path.join(log_dir, fn))
    if not candidates:
        return None
    candidates.sort(key=os.path.getmtime, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# Results writer
# ---------------------------------------------------------------------------


def _write_results_json(path: str, results: List[TaskResult],
                          args: argparse.Namespace, ts: str) -> None:
    """Write the per-(task, seed) results.json, kept in the same
    shape as the API runner so downstream tooling (stitch_results.py)
    can consume either side."""
    doc = {
        "started_at": ts,
        "scaffold": "ui_baseline",
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
        print(f"Slate ({len(slate)} tasks × {len(seeds)} seeds = "
                f"{total} episodes):")
        for e in slate:
            for s in seeds:
                print(f"  {e.cls:>8}  {e.subset:>12}  "
                        f"{e.generator}  seed={s}")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = args.results_dir or os.path.join(
        _REPO_ROOT, "sibb", "api_baseline", "results",
        f"ui_run_{ts}")
    os.makedirs(run_dir, exist_ok=True)
    results_path = os.path.join(run_dir, "results.json")
    table4_path = os.path.join(run_dir, "table4.csv")

    print(f"\n=== sibb_ui_runner ===")
    print(f"Slate:     {len(slate)} tasks  × {len(seeds)} seeds "
            f"= {total} episodes")
    print(f"Provider:  {args.provider} ({args.model})")
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

    results: List[TaskResult] = []
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
                        # Record remaining as hard errors and bail.
                        results.append(TaskResult(
                            generator=entry.generator,
                            runner_key=entry.runner_key,
                            cls=entry.cls, subset=entry.subset,
                            seed=seed, passed=False,
                            turns_used=0, tool_calls_made=0,
                            cost_usd=0.0, truncated=False,
                            truncation_reason=None,
                            error=f"recycle_failed: {type(e).__name__}",
                            duration_s=0.0,
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
                try:
                    exit_code = await UA.run_episode(ep_args)
                except (Exception, SystemExit) as e:  # noqa: BLE001
                    print(f"        ✗ episode raised: "
                            f"{type(e).__name__}: {e}")
                    hard_errors += 1
                    episode_error = f"{type(e).__name__}: {e}"

                dur = time.monotonic() - t0
                # Find the JSONL the assistant just wrote and pull
                # per-episode metrics out.
                jsonl = _latest_jsonl_in(
                    run_dir, entry.runner_key,
                    args.provider, args.model)
                metrics = _parse_episode_jsonl(jsonl or "")
                results.append(TaskResult(
                    generator=entry.generator,
                    runner_key=entry.runner_key,
                    cls=entry.cls,
                    subset=entry.subset,
                    seed=seed,
                    passed=(exit_code == 0),
                    turns_used=metrics["turns_used"],
                    tool_calls_made=metrics["tool_calls_made"],
                    cost_usd=metrics["cost_usd"],
                    truncated=metrics["truncated"],
                    truncation_reason=metrics["truncation_reason"],
                    error=episode_error,
                    duration_s=dur,
                ))

                # Persist incrementally so a mid-run kill doesn't
                # lose data.
                _write_results_json(results_path, results, args, ts)
                write_table4_csv(aggregate_table4(results),
                                  pathlib.Path(table4_path))

                # --- Recycle on episode exception -------------------
                # If the episode raised, the server may be in a
                # broken state (BrokenPipe is the canonical signal —
                # see CLEAR-timeout observation 2026-06-11). Recycle
                # before the next episode.
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
    print(f"\nDone — {n_pass}/{len(results)} passed; "
            f"{hard_errors} hard errors; {recycles} recycles")
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
                          "runner_key (with or without 'gen_' prefix)")
    p.add_argument("--bundle", default=DEFAULT_BUNDLE,
                    help=f"Initial bundle for AXReader (default: "
                          f"{DEFAULT_BUNDLE})")
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--max-seconds", type=float, default=float("inf"))
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--max-elements", type=int, default=150)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--llm-timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--results-dir", default=None,
                    help="Run dir. Defaults to sibb/api_baseline/"
                          "results/ui_run_<ts>/")
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
