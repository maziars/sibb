"""Batch runner for the Option A+ API baseline experiment.

Drives `sibb_api_assistant.run_api_episode` over the scored slate in
`classification.yaml`. Writes per-task trajectory JSONLs (the assistant
does this itself), plus aggregate `results.json` and `table4.csv` files
under a single run directory.

The runner is deliberately simple and sequential — one sim per UDID
means parallelism would require multiple simulators. We don't need v2
hot-reload to land headline numbers.

CLI usage:

    python -m sibb.api_baseline.sibb_api_runner \\
        --udid <YOUR-SIM-UDID> \\
        --provider gemini --model gemini-2.5-flash \\
        --seed 0 --task-filter all

Filters:
    --task-filter all       → 26 scored tasks (22 api_only + 7 ui_only;
                              wait, 19 + 7 = 26 after the Bucket-1
                              re-classification — see classification.yaml
                              summary)
    --task-filter api_only  → 19 tasks
    --task-filter ui_only   → 7 tasks
    --task-filter smoke     → 3 hand-picked tasks for validation runs
    --task-filter <name>    → a single task by short name

CLAUDE.md compliance:
  - Python 3.9 typing — Optional / List / Dict / Tuple.
  - No PyYAML (stock 3.9 from CommandLineTools doesn't have it); the
    classification.yaml is parsed with stdlib regex same way the L1
    invariant test does.
  - No edits to sibb/benchmark/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import os
import pathlib
import re
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
for _sub in ("sibb/simulator", "sibb/benchmark"):
    _p = os.path.join(_REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sibb_llm import default_model  # noqa: E402
from sibb_xcuitest_client import XCUITestReader  # noqa: E402
from sibb_episode import simctl_boot, simctl_wait_booted  # noqa: E402

from sibb.api_baseline.sibb_api_assistant import (  # noqa: E402
    run_api_episode,
    DEFAULT_MAX_TURNS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TIMEOUT_S,
    DEFAULT_LLM_MAX_RETRIES,
    EpisodeOutcome,
    resolve_generator_key,
)


# Preventive recycle cadence — after this many episodes, we proactively
# shut the sim down and rebuild the XCUITest server. Apple Forums
# #118920 + Maestro #3254/#3318 + WDA #507 all document
# CoreSimulatorService / testmanagerd resource exhaustion that
# eventually crashes a long-running test runner. Recycling preemptively
# is ~15s of cost (no `simctl erase`) and turns a guaranteed crash
# every 3-20 episodes into a controlled hygiene cycle.
# Tightened from 20 → 10 on 2026-06-11 after the hybrid v1 run
# (~3.8% TAP-timeout rate at every-20 cadence). The ~25s per
# preventive recycle is cheap insurance compared with the cost of a
# mid-run timeout cascade; halving the gap halves the chance a
# cumulative-state issue builds up between recycles.
_PREVENTIVE_RECYCLE_EVERY_N = 10

# Healthcheck timeout — how long we wait for a `ping` response before
# declaring the XCUITest server zombie. The reader's own _send timeout
# is 30s (was 60s pre-2026-06-11); 3s is enough to catch the "alive
# but unreachable" state without flagging real-but-slow responses.
_HEALTHCHECK_TIMEOUT_S = 3.0


# ---------------------------------------------------------------------------
# classification.yaml parser
# ---------------------------------------------------------------------------


CLASSIFICATION_YAML = (
    pathlib.Path(_REPO_ROOT) / "sibb" / "api_baseline"
    / "classification.yaml")


@dataclass
class SlateEntry:
    """One task in the classification.yaml scored slate."""
    generator: str            # Python def name, e.g. "gen_add_reminder..."
    runner_key: str           # GENERATORS dict key (no "gen_" prefix)
    cls: str                  # "api_only" | "ui_only" | "hybrid"
    subset: str               # "Reminders" | "Calendar" | ...

    def __post_init__(self):
        if not self.runner_key:
            self.runner_key = resolve_generator_key(self.generator)


# Map each generator to its paper-table subset. The headline Table 4
# uses 5 rows; entries not in this map default to "Other".
_SUBSET_BY_GENERATOR: Dict[str, str] = {
    # ---- Reminders (api_only) -----------
    "gen_add_reminder_to_existing_list":   "Reminders",
    "gen_create_recurring_with_due":       "Reminders",
    "gen_list_due_today":                  "Reminders",
    "gen_list_due_tomorrow":               "Reminders",
    "gen_lookup_reminder_notes":           "Reminders",
    # ---- Calendar (api_only) ------------
    "gen_create_event_with_title_time":    "Calendar",
    "gen_create_recurring_event":          "Calendar",
    "gen_lookup_event_location":           "Calendar",
    "gen_list_events_today":               "Calendar",
    "gen_list_conflicting_events":         "Calendar",
    "gen_next_event_lookup":               "Calendar",
    # ---- Contacts (api_only) ------------
    "gen_create_contact_with_address":     "Contacts",
    "gen_full_business_card":              "Contacts",
    "gen_lookup_phone_by_name":            "Contacts",
    "gen_set_contact_birthday":            "Contacts",
    "gen_set_contact_birthday_no_year":    "Contacts",
    "gen_add_second_phone_label":          "Contacts",
    # ---- Cross-app (api_only) -----------
    "gen_maps_search_to_contact":          "Cross-app",
    "gen_reminder_with_calendar_event":    "Cross-app",
    # ---- UI-required --------------------
    "gen_message_save_sender":             "UI-required",
    "gen_message_save_body":               "UI-required",
    "gen_message_save_address":            "UI-required",
    "gen_message_save_sender_with_address": "UI-required",
    "gen_message_to_contact_to_maps":      "UI-required",
    "gen_message_to_new_contact_to_maps":  "UI-required",
    "gen_safari_bookmark_specific_url":    "UI-required",
}


def parse_classification_slate(path: pathlib.Path = CLASSIFICATION_YAML
                                 ) -> List[SlateEntry]:
    """Parse the scored (`tasks:`) section of classification.yaml into
    SlateEntry records. Hybrid extras are deliberately excluded — they
    are not in the headline run."""
    text = path.read_text()
    try:
        start = text.index("\ntasks:\n")
    except ValueError as exc:
        raise RuntimeError(
            f"classification.yaml missing `tasks:` section ({path})") from exc
    end = text.index("\nhybrid_tasks_for_kappa:\n")
    section = text[start:end]

    out: List[SlateEntry] = []
    cur_gen: Optional[str] = None
    cur_cls: Optional[str] = None
    for line in section.splitlines():
        m_gen = re.match(r"^  - generator: (\S+)", line)
        m_cls = re.match(r"^    class: (\S+)", line)
        if m_gen:
            if cur_gen and cur_cls:
                out.append(SlateEntry(
                    generator=cur_gen, runner_key="",
                    cls=cur_cls,
                    subset=_SUBSET_BY_GENERATOR.get(cur_gen, "Other"),
                ))
            cur_gen = m_gen.group(1)
            cur_cls = None
        elif m_cls and cur_gen is not None:
            cur_cls = m_cls.group(1)
    if cur_gen and cur_cls:
        out.append(SlateEntry(
            generator=cur_gen, runner_key="",
            cls=cur_cls,
            subset=_SUBSET_BY_GENERATOR.get(cur_gen, "Other"),
        ))
    return out


# ---------------------------------------------------------------------------
# Task filtering
# ---------------------------------------------------------------------------


# Three hand-picked tasks for `--task-filter smoke`. Chosen to exercise:
#  1. simplest api_only path (add a reminder)
#  2. read-style task with agent.answer
#  3. one ui_only to confirm 0% by construction
_SMOKE_TASKS: Tuple[str, ...] = (
    "gen_add_reminder_to_existing_list",
    "gen_list_due_today",
    "gen_message_save_sender",
)


def select_tasks(slate: List[SlateEntry], filt: str) -> List[SlateEntry]:
    """Apply a task-filter string to a parsed slate. `filt` is one of:
      - 'all'           → entire slate
      - 'api_only'      → just the API-doable rows
      - 'ui_only'       → just the by-construction-zero rows
      - 'smoke'         → 3 hand-picked validation tasks
      - <generator>     → a single entry (full name or stripped form)
      - 'a,b,c'         → comma-separated list of generators (any form)
    """
    if filt == "all":
        return list(slate)
    if filt in ("api_only", "ui_only", "hybrid"):
        return [e for e in slate if e.cls == filt]
    if filt == "smoke":
        return [e for e in slate if e.generator in _SMOKE_TASKS]
    # Comma-separated list of generator names.
    if "," in filt:
        names = [n.strip() for n in filt.split(",") if n.strip()]
        keys = {resolve_generator_key(n) for n in names}
        matches = [e for e in slate
                    if e.generator in names or e.runner_key in keys]
        if len(matches) != len(names):
            missing = set(names) - {e.generator for e in matches} - {
                e.runner_key for e in matches}
            raise SystemExit(
                f"task-filter could not match {sorted(missing)!r}.")
        return matches
    # Exact match on either form.
    key = resolve_generator_key(filt)
    matches = [e for e in slate
                if e.generator == filt or e.runner_key == key]
    if not matches:
        raise SystemExit(
            f"task-filter {filt!r} matched nothing. Use one of: "
            "'all' / 'api_only' / 'ui_only' / 'smoke' / <generator> "
            "/ comma-list of generators.")
    return matches


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    """One (task, seed) outcome — what we write to results.json."""
    generator: str
    runner_key: str
    cls: str
    subset: str
    seed: int
    passed: bool
    turns_used: int
    tool_calls_made: int
    cost_usd: float
    truncated: bool
    truncation_reason: Optional[str]
    error: Optional[str]
    duration_s: float


def aggregate_table4(results: List[TaskResult]
                      ) -> List[Dict[str, Any]]:
    """Group results into Table 4 rows: one row per (subset, class) seen
    in the slate. Each row: subset, class, n_run, n_pass, pass_rate,
    total_cost_usd, mean_turns."""
    by_key: Dict[Tuple[str, str], List[TaskResult]] = {}
    for r in results:
        key = (r.subset, r.cls)
        by_key.setdefault(key, []).append(r)
    rows: List[Dict[str, Any]] = []
    for (subset, cls), rs in sorted(by_key.items()):
        n_run = len(rs)
        n_pass = sum(1 for r in rs if r.passed)
        rows.append({
            "subset": subset,
            "class": cls,
            "n_run": n_run,
            "n_pass": n_pass,
            "pass_rate": (n_pass / n_run) if n_run else 0.0,
            "total_cost_usd": sum(r.cost_usd for r in rs),
            "mean_turns": (sum(r.turns_used for r in rs) / n_run
                            if n_run else 0.0),
        })
    return rows


def write_table4_csv(rows: List[Dict[str, Any]], path: pathlib.Path
                       ) -> None:
    """Write the per-subset aggregate as CSV."""
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "subset", "class", "n_run", "n_pass",
            "pass_rate", "total_cost_usd", "mean_turns",
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **row,
                "pass_rate": f"{row['pass_rate']:.4f}",
                "total_cost_usd": f"{row['total_cost_usd']:.4f}",
                "mean_turns": f"{row['mean_turns']:.2f}",
            })


# ---------------------------------------------------------------------------
# Episode-args builder
# ---------------------------------------------------------------------------


def _build_episode_args(runner_args, entry: SlateEntry, seed: int,
                          log_dir: str,
                          inject_reader: Optional[Any] = None
                          ) -> types.SimpleNamespace:
    """Build the namespace that `run_api_episode` expects.

    The assistant CLI uses argparse.Namespace; we replicate the same
    field set here so the runner can invoke the function without
    spawning a subprocess.

    `inject_reader`: when provided, the episode skips its own
    pre_runner / boot / reader.start() and reuses the shared reader
    the batch runner owns."""
    return types.SimpleNamespace(
        udid=runner_args.udid,
        generator=entry.runner_key,
        seed=seed,
        provider=runner_args.provider,
        model=runner_args.model,
        max_turns=runner_args.max_turns,
        max_tokens=runner_args.max_tokens,
        temperature=runner_args.temperature,
        llm_timeout=runner_args.llm_timeout,
        llm_max_retries=runner_args.llm_max_retries,
        budget_usd_max=runner_args.budget_usd_max,
        retrieval=runner_args.retrieval,
        log_dir=log_dir,
        inject_reader=inject_reader,
    )


# ---------------------------------------------------------------------------
# Shared XCUITest reader + healthcheck + recycle
# ---------------------------------------------------------------------------


async def _is_reader_alive(reader: Any,
                             timeout: float = _HEALTHCHECK_TIMEOUT_S
                             ) -> bool:
    """Returns True iff the XCUITest server responds to a ping within
    `timeout` seconds. Catches the "alive but unreachable" zombie state
    Maestro #3254 / WDA #507 describe — the runner process appears
    healthy but the socket no longer answers."""
    try:
        resp = await asyncio.wait_for(
            reader._send({"type": "ping"}),
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 — any failure = unhealthy
        return False
    return bool(resp.get("ok", False))


async def _recycle_reader(old_reader: Optional[Any], udid: str) -> Any:
    """Tear down the old XCUITest server, recycle the sim, and bring
    up a fresh server. Returns the new reader.

    Uses `simctl shutdown && boot` (NOT `erase`) so the sim's data
    directory survives — accumulated `testmanagerd` / CoreSimulatorService
    state is drained without paying the 60-120s cold-boot prewarm cost."""
    # Stop the old reader first (best-effort).
    if old_reader is not None:
        try:
            await old_reader.stop()
        except Exception:  # noqa: BLE001
            pass
    # Drain CoreSimulatorService state via a quick shutdown/boot.
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
    new_reader = XCUITestReader(udid=udid, bundle_id="com.apple.reminders")
    await new_reader.start()
    return new_reader


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_corpus(args) -> int:
    """Iterate the slate × seeds matrix; write incremental results.

    Returns 0 if every selected episode completed (pass or fail by
    verifier — that's a fine outcome). Returns 2 if any episode had a
    hard error (LLM init failure, sim hang) AND `--fail-on-error` is
    set. Otherwise returns 0 even with hard errors so a batch can
    complete and surface partial results.
    """
    slate = parse_classification_slate()
    selected = select_tasks(slate, args.task_filter)
    seeds = [int(s) for s in args.seeds.split(",")]

    if args.list:
        # Dry-run: just print the slate × seeds matrix.
        for e in selected:
            for s in seeds:
                print(f"{e.cls:>8}  {e.subset:>12}  {e.generator}  seed={s}")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = (args.results_dir
               or os.path.join(_REPO_ROOT, "sibb", "api_baseline",
                                "results", f"run_{ts}"))
    os.makedirs(run_dir, exist_ok=True)
    results_path = os.path.join(run_dir, "results.json")
    table4_path = os.path.join(run_dir, "table4.csv")

    results: List[TaskResult] = []
    total = len(selected) * len(seeds)
    print(f"\n=== sibb_api_runner ===")
    print(f"Slate:     {len(selected)} tasks  × {len(seeds)} seeds = "
            f"{total} episodes")
    print(f"Provider:  {args.provider} ({args.model})")
    print(f"Run dir:   {run_dir}")
    print()

    # ---- One shared XCUITest reader for the whole batch -------------
    # Eliminates the boot churn that crashed the prior headline runs.
    # See sibb_episode.py:183-212 for the UI baseline's identical
    # pattern (`inject_reader`). One-time ~25s startup; reused across
    # every episode below.
    print(f"  → booting sim + XCUITest server (one-time, ~25s)...")
    await simctl_boot(args.udid)
    await simctl_wait_booted(args.udid)
    await asyncio.sleep(2.0)
    reader = XCUITestReader(udid=args.udid, bundle_id="com.apple.reminders")
    await reader.start()
    print(f"  → XCUITest server ready")
    print()

    completed = 0
    hard_errors = 0
    recycles = 0
    try:
        for entry in selected:
            for seed in seeds:
                completed += 1

                # --- Pre-episode healthcheck ------------------------
                # Catches the "alive but unreachable" zombie state the
                # research report flagged from Maestro #3254 / WDA #507.
                # If the server is dead, recycle BEFORE running the
                # episode (so the episode sees a fresh server instead
                # of hanging).
                if not await _is_reader_alive(reader):
                    recycles += 1
                    print(f"  ⚠ XCUITest server unreachable; recycling "
                            f"(recycle #{recycles})...")
                    try:
                        reader = await _recycle_reader(reader, args.udid)
                        print(f"  → recovered")
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ RECYCLE FAILED: "
                                f"{type(e).__name__}: {e}")
                        # We can't proceed; record remaining tasks as
                        # hard errors and bail.
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
                        _write_results_json(results_path, results, args, ts)
                        write_table4_csv(
                            aggregate_table4(results),
                            pathlib.Path(table4_path))
                        hard_errors += 1
                        return 2 if args.fail_on_error else 0

                # --- Run the episode ------------------------------------
                print(f"  [{completed:>3}/{total:<3}] {entry.cls:>8} "
                        f"{entry.subset:>10}  {entry.runner_key}  seed={seed}")
                ep_args = _build_episode_args(
                    args, entry, seed, run_dir, inject_reader=reader)
                t0 = time.monotonic()
                episode_error: Optional[str] = None
                try:
                    exit_code, outcome = await run_api_episode(ep_args)
                except (Exception, SystemExit) as e:  # noqa: BLE001
                    # Catch SystemExit too — run_api_episode raises it
                    # for unknown-generator / config errors, which would
                    # otherwise unwind the whole batch.
                    print(f"        ✗ episode raised: "
                            f"{type(e).__name__}: {e}")
                    hard_errors += 1
                    episode_error = f"{type(e).__name__}: {e}"
                    results.append(TaskResult(
                        generator=entry.generator,
                        runner_key=entry.runner_key,
                        cls=entry.cls, subset=entry.subset, seed=seed,
                        passed=False, turns_used=0, tool_calls_made=0,
                        cost_usd=0.0, truncated=False,
                        truncation_reason=None,
                        error=episode_error,
                        duration_s=time.monotonic() - t0,
                    ))
                else:
                    dur = time.monotonic() - t0
                    results.append(TaskResult(
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
                    ))

                # Write incrementally so a mid-run crash doesn't lose
                # data.
                _write_results_json(results_path, results, args, ts)
                write_table4_csv(aggregate_table4(results),
                                  pathlib.Path(table4_path))

                # --- Recycle on episode exception -------------------
                # If the episode raised, the server may be in a broken
                # state. Recycle before the next episode.
                if episode_error is not None:
                    recycles += 1
                    print(f"  ⚠ episode raised; recycling "
                            f"(recycle #{recycles})...")
                    try:
                        reader = await _recycle_reader(reader, args.udid)
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ RECYCLE FAILED: "
                                f"{type(e).__name__}: {e}")
                        return 2 if args.fail_on_error else 0

                # --- Preventive recycle every N episodes -------------
                # Drains accumulated testmanagerd / CoreSimulatorService
                # state before it builds up to a crash. Cost: ~15s every
                # 20 episodes vs ~60s on a real crash + lost results.
                if (completed > 0
                        and completed % _PREVENTIVE_RECYCLE_EVERY_N == 0
                        and completed < total):
                    recycles += 1
                    print(f"  → preventive recycle at episode {completed} "
                            f"(recycle #{recycles})...")
                    try:
                        reader = await _recycle_reader(reader, args.udid)
                    except Exception as e:  # noqa: BLE001
                        print(f"  ✗ RECYCLE FAILED: "
                                f"{type(e).__name__}: {e}")
                        return 2 if args.fail_on_error else 0
    finally:
        try:
            await reader.stop()
        except Exception:  # noqa: BLE001
            pass

    print(f"\nDone — {sum(1 for r in results if r.passed)}/{len(results)} "
            f"passed; {hard_errors} hard errors; {recycles} recycles")
    print(f"results.json  →  {results_path}")
    print(f"table4.csv    →  {table4_path}")

    if hard_errors and args.fail_on_error:
        return 2
    return 0


def _write_results_json(path: str, results: List[TaskResult],
                          args, ts: str) -> None:
    """Atomic-ish JSON dump: write to .tmp then rename. Cheap insurance
    against mid-write crashes during long batches."""
    payload = {
        "started_at": ts,
        "provider": args.provider,
        "model": args.model,
        "task_filter": args.task_filter,
        "seeds": args.seeds,
        "n_results": len(results),
        "n_pass": sum(1 for r in results if r.passed),
        "results": [dataclasses.asdict(r) for r in results],
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch runner for the SIBB API-only baseline.")
    p.add_argument("--udid", required=True,
                    help="iOS simulator UDID. Reused across every "
                         "episode in the batch.")
    p.add_argument("--provider", default="gemini")
    p.add_argument("--model", default=None,
                    help="Provider's default model when None.")
    p.add_argument("--seeds", default="0",
                    help="Comma-separated seed list (default '0' → "
                         "single seed; '0,1,2' → 3 seeds per task).")
    p.add_argument("--task-filter", default="all",
                    help="Filter: 'all', 'api_only', 'ui_only', "
                         "'smoke', or a generator name.")
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--llm-timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--llm-max-retries", type=int,
                    default=DEFAULT_LLM_MAX_RETRIES)
    p.add_argument("--budget-usd-max", type=float, default=None,
                    help="Per-episode soft cap.")
    p.add_argument("--retrieval", action="store_true", default=True,
                    help="Use model-driven Tool Search (default ON).")
    p.add_argument("--no-retrieval", dest="retrieval",
                    action="store_false",
                    help="Static-catalog ablation.")
    p.add_argument("--results-dir", default=None,
                    help="Run dir. Defaults to sibb/api_baseline/"
                         "results/run_<ts>/")
    p.add_argument("--list", action="store_true",
                    help="Print the slate × seeds matrix and exit "
                         "(no episodes run).")
    p.add_argument("--fail-on-error", action="store_true",
                    help="Exit code 2 if any episode hit a hard error "
                         "(LLM init / sim hang / runaway exception).")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.model is None:
        args.model = default_model(args.provider)
    return asyncio.run(run_corpus(args))


if __name__ == "__main__":
    sys.exit(main())
