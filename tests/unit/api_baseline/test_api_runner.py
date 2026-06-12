"""L1 tests for sibb/api_baseline/sibb_api_runner.py.

These cover the pieces of the runner that don't need a sim:
  - classification.yaml parser (slate count, ordering, subset mapping)
  - task selection filters ('all', 'api_only', 'ui_only', 'smoke',
    single-task name with or without 'gen_' prefix)
  - Table 4 aggregation math
  - results.json / table4.csv shape

The actual run_corpus loop is exercised at smoke time against a real
sim — there's no good way to mock it in L1 without re-implementing
the whole episode pipeline.
"""

from __future__ import annotations

import csv
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Dict, List

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from sibb.api_baseline import sibb_api_runner as R  # noqa: E402


# ---------------------------------------------------------------------------
# Classification slate parser
# ---------------------------------------------------------------------------


def test_parse_classification_slate_returns_pure_design_a_count():
    """After the Bucket-1 reclassification: 19 api_only + 7 ui_only =
    26 scored. Hybrid entries are excluded from the slate."""
    slate = R.parse_classification_slate()
    assert len(slate) == 26
    by_cls = {}
    for e in slate:
        by_cls.setdefault(e.cls, []).append(e)
    assert len(by_cls.get("api_only", [])) == 19
    assert len(by_cls.get("ui_only", [])) == 7
    assert "hybrid" not in by_cls


def test_parse_classification_slate_assigns_subset_for_every_entry():
    """The _SUBSET_BY_GENERATOR map must cover every scored generator.
    A missing entry would silently land in 'Other' and corrupt
    Table 4."""
    slate = R.parse_classification_slate()
    others = [e for e in slate if e.subset == "Other"]
    assert not others, (
        "These scored generators have no entry in _SUBSET_BY_GENERATOR: "
        f"{[e.generator for e in others]}")


def test_parse_classification_slate_subsets_are_paper_table_rows():
    """The paper's Table 4 has 5 rows: Reminders, Calendar, Contacts,
    Cross-app, UI-required. Every slate entry's subset must be one of
    these — otherwise aggregate_table4 will create rogue rows."""
    expected = {"Reminders", "Calendar", "Contacts",
                 "Cross-app", "UI-required"}
    slate = R.parse_classification_slate()
    actual = {e.subset for e in slate}
    assert actual == expected, (
        f"subsets drifted; got {actual}, expected {expected}")


def test_parse_classification_slate_runner_key_strips_gen_prefix():
    """The classification.yaml lists `gen_*` Python def names; the
    runtime GENERATORS dict registers under the bare form."""
    slate = R.parse_classification_slate()
    for e in slate:
        assert e.generator.startswith("gen_")
        assert e.runner_key == e.generator[len("gen_"):]
        assert not e.runner_key.startswith("gen_")


# ---------------------------------------------------------------------------
# Task filters
# ---------------------------------------------------------------------------


def _make_slate() -> List[R.SlateEntry]:
    return [
        R.SlateEntry(generator="gen_add_reminder_to_existing_list",
                      runner_key="", cls="api_only", subset="Reminders"),
        R.SlateEntry(generator="gen_list_due_today",
                      runner_key="", cls="api_only", subset="Reminders"),
        R.SlateEntry(generator="gen_message_save_sender",
                      runner_key="", cls="ui_only", subset="UI-required"),
    ]


def test_select_tasks_filter_all_returns_full_slate():
    slate = _make_slate()
    out = R.select_tasks(slate, "all")
    assert len(out) == 3


def test_select_tasks_filter_api_only():
    slate = _make_slate()
    out = R.select_tasks(slate, "api_only")
    assert {e.generator for e in out} == {
        "gen_add_reminder_to_existing_list", "gen_list_due_today"}


def test_select_tasks_filter_ui_only():
    slate = _make_slate()
    out = R.select_tasks(slate, "ui_only")
    assert {e.generator for e in out} == {"gen_message_save_sender"}


def test_select_tasks_filter_smoke_returns_three_tasks():
    """Smoke is the validation-run subset: one easy api_only, one
    read-style answer task, one ui_only. Pin the 3-task contract."""
    # Build a slate that contains the 3 smoke names.
    slate = R.parse_classification_slate()
    out = R.select_tasks(slate, "smoke")
    assert len(out) == 3
    expected = {"gen_add_reminder_to_existing_list",
                 "gen_list_due_today",
                 "gen_message_save_sender"}
    assert {e.generator for e in out} == expected


def test_select_tasks_filter_single_task_accepts_gen_prefix():
    slate = _make_slate()
    out = R.select_tasks(slate, "gen_list_due_today")
    assert len(out) == 1
    assert out[0].generator == "gen_list_due_today"


def test_select_tasks_filter_single_task_accepts_bare_form():
    slate = _make_slate()
    out = R.select_tasks(slate, "list_due_today")
    assert len(out) == 1
    assert out[0].generator == "gen_list_due_today"


def test_select_tasks_filter_unknown_name_raises_systemexit():
    slate = _make_slate()
    with pytest.raises(SystemExit, match="matched nothing"):
        R.select_tasks(slate, "gen_does_not_exist")


def test_select_tasks_filter_comma_separated_list_accepts_any_form():
    slate = _make_slate()
    out = R.select_tasks(
        slate, "gen_add_reminder_to_existing_list,list_due_today")
    assert {e.generator for e in out} == {
        "gen_add_reminder_to_existing_list", "gen_list_due_today"}


def test_select_tasks_filter_comma_separated_list_raises_on_unknown():
    slate = _make_slate()
    with pytest.raises(SystemExit, match="could not match"):
        R.select_tasks(slate, "list_due_today,does_not_exist")


# ---------------------------------------------------------------------------
# Table 4 aggregation
# ---------------------------------------------------------------------------


def _make_results(pairs: List[Dict[str, Any]]) -> List[R.TaskResult]:
    """Convenience constructor for TaskResult fixtures.
    `pairs` is a list of dicts; missing fields default to safe values."""
    out: List[R.TaskResult] = []
    for p in pairs:
        out.append(R.TaskResult(
            generator=p["generator"],
            runner_key=p.get("runner_key", ""),
            cls=p["cls"],
            subset=p["subset"],
            seed=p.get("seed", 0),
            passed=p["passed"],
            turns_used=p.get("turns_used", 3),
            tool_calls_made=p.get("tool_calls_made", 2),
            cost_usd=p.get("cost_usd", 0.001),
            truncated=p.get("truncated", False),
            truncation_reason=p.get("truncation_reason"),
            error=p.get("error"),
            duration_s=p.get("duration_s", 30.0),
        ))
    return out


def test_aggregate_table4_groups_by_subset_and_class():
    results = _make_results([
        {"generator": "g1", "cls": "api_only", "subset": "Reminders",
          "passed": True, "cost_usd": 0.001, "turns_used": 3},
        {"generator": "g2", "cls": "api_only", "subset": "Reminders",
          "passed": False, "cost_usd": 0.002, "turns_used": 8},
        {"generator": "g3", "cls": "ui_only", "subset": "UI-required",
          "passed": False, "cost_usd": 0.0005, "turns_used": 2},
    ])
    rows = R.aggregate_table4(results)
    # 2 rows: Reminders/api_only, UI-required/ui_only.
    assert len(rows) == 2
    rem = next(r for r in rows if r["subset"] == "Reminders")
    assert rem["n_run"] == 2
    assert rem["n_pass"] == 1
    assert rem["pass_rate"] == 0.5
    assert rem["total_cost_usd"] == pytest.approx(0.003)
    assert rem["mean_turns"] == pytest.approx(5.5)
    ui = next(r for r in rows if r["subset"] == "UI-required")
    assert ui["n_run"] == 1
    assert ui["n_pass"] == 0
    assert ui["pass_rate"] == 0.0


def test_aggregate_table4_zero_runs_does_not_divide_by_zero():
    rows = R.aggregate_table4([])
    assert rows == []


def test_write_table4_csv_emits_expected_columns():
    results = _make_results([
        {"generator": "g1", "cls": "api_only", "subset": "Reminders",
          "passed": True, "cost_usd": 0.001},
    ])
    rows = R.aggregate_table4(results)
    with tempfile.TemporaryDirectory() as td:
        path = pathlib.Path(td) / "table4.csv"
        R.write_table4_csv(rows, path)
        assert path.exists()
        with open(path) as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
        assert len(rows) == 1
        row = rows[0]
        assert set(row.keys()) == {
            "subset", "class", "n_run", "n_pass",
            "pass_rate", "total_cost_usd", "mean_turns"}
        # Numeric formatting: 4 decimals on the rates, 2 on turns.
        assert row["pass_rate"] == "1.0000"
        assert row["mean_turns"] == "3.00"


# ---------------------------------------------------------------------------
# results.json writer
# ---------------------------------------------------------------------------


def test_write_results_json_is_atomic_and_loadable():
    """The writer dumps to .tmp then renames so a mid-write crash can't
    corrupt a long batch's accumulator. Verify the post-rename file is
    valid JSON."""
    results = _make_results([
        {"generator": "g1", "cls": "api_only", "subset": "Reminders",
          "passed": True},
    ])

    class _Args:
        provider = "gemini"
        model = "gemini-2.5-flash"
        task_filter = "smoke"
        seeds = "0"

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "results.json")
        R._write_results_json(path, results, _Args(), "20260610_200000")
        # The .tmp file should NOT exist post-rename.
        assert not os.path.exists(path + ".tmp")
        with open(path) as fh:
            data = json.load(fh)
        assert data["n_results"] == 1
        assert data["n_pass"] == 1
        assert data["provider"] == "gemini"
        assert len(data["results"]) == 1
        assert data["results"][0]["generator"] == "g1"


# ---------------------------------------------------------------------------
# Episode-args builder
# ---------------------------------------------------------------------------


def test_build_episode_args_threads_per_episode_overrides():
    """The runner builds a fresh argparse Namespace per episode (so each
    episode independently logs to a per-task JSONL). Pin the field set."""
    class _RunnerArgs:
        udid = "ABC-123"
        provider = "gemini"
        model = "gemini-2.5-flash"
        max_turns = 8
        max_tokens = 2048
        temperature = 0.0
        llm_timeout = 60.0
        llm_max_retries = 5
        budget_usd_max = None
        retrieval = True

    entry = R.SlateEntry(
        generator="gen_list_due_today",
        runner_key="list_due_today",
        cls="api_only", subset="Reminders")
    ep_args = R._build_episode_args(_RunnerArgs(), entry, seed=3,
                                       log_dir="/tmp/run_x")
    assert ep_args.udid == "ABC-123"
    assert ep_args.generator == "list_due_today"  # bare form
    assert ep_args.seed == 3
    assert ep_args.log_dir == "/tmp/run_x"
    assert ep_args.retrieval is True
    # Default: no injected reader (standalone CLI mode).
    assert ep_args.inject_reader is None


def test_build_episode_args_threads_inject_reader_through():
    """The batch runner constructs args with `inject_reader` set so the
    episode reuses the shared XCUITest server."""
    class _RunnerArgs:
        udid = "ABC-123"
        provider = "gemini"
        model = "gemini-2.5-flash"
        max_turns = 8
        max_tokens = 2048
        temperature = 0.0
        llm_timeout = 60.0
        llm_max_retries = 5
        budget_usd_max = None
        retrieval = True

    entry = R.SlateEntry(
        generator="gen_list_due_today",
        runner_key="list_due_today",
        cls="api_only", subset="Reminders")
    shared_reader = object()  # sentinel
    ep_args = R._build_episode_args(
        _RunnerArgs(), entry, seed=0, log_dir="/tmp",
        inject_reader=shared_reader)
    assert ep_args.inject_reader is shared_reader


# ---------------------------------------------------------------------------
# Healthcheck + recycle (mocked socket)
# ---------------------------------------------------------------------------


class _FakeReader:
    """Reader stub with a programmable _send + stop."""
    def __init__(self, *, ping_response: Any = None,
                  ping_exception: Optional[BaseException] = None,
                  stop_exception: Optional[BaseException] = None):
        self._ping_response = (
            ping_response if ping_response is not None else {"ok": True})
        self._ping_exception = ping_exception
        self._stop_exception = stop_exception
        self.send_calls: List[Dict[str, Any]] = []
        self.stop_called = False

    async def _send(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        self.send_calls.append(cmd)
        if self._ping_exception:
            raise self._ping_exception
        return self._ping_response

    async def stop(self) -> None:
        self.stop_called = True
        if self._stop_exception:
            raise self._stop_exception


@pytest.mark.asyncio
async def test_is_reader_alive_returns_true_on_ping_ok():
    reader = _FakeReader(ping_response={"ok": True})
    assert await R._is_reader_alive(reader, timeout=1.0) is True
    assert reader.send_calls == [{"type": "ping"}]


@pytest.mark.asyncio
async def test_is_reader_alive_returns_false_when_server_responds_not_ok():
    reader = _FakeReader(ping_response={"ok": False, "error": "x"})
    assert await R._is_reader_alive(reader, timeout=1.0) is False


@pytest.mark.asyncio
async def test_is_reader_alive_returns_false_when_send_raises():
    """Catches the zombie 'alive but unreachable' state."""
    reader = _FakeReader(
        ping_exception=ConnectionError("socket closed"))
    assert await R._is_reader_alive(reader, timeout=1.0) is False


@pytest.mark.asyncio
async def test_is_reader_alive_returns_false_on_timeout():
    """If the ping doesn't return in the budget, treat as dead."""
    class _HangingReader:
        async def _send(self, cmd):
            await asyncio.sleep(10)  # never returns
            return {"ok": True}
    assert await R._is_reader_alive(
        _HangingReader(), timeout=0.1) is False


# ---------------------------------------------------------------------------
# SystemExit corral
# ---------------------------------------------------------------------------
#
# Background: `run_api_episode` raises `SystemExit("unknown generator
# 'X'")` when its GENERATORS lookup misses. The original episode catch
# (`except Exception`) let SystemExit through, killing the whole batch
# silently mid-headline. The fix widened to `except (Exception,
# SystemExit)` so a single bad task records a hard-error TaskResult and
# the runner moves on. We pin both halves of that contract.


def test_runner_episode_catch_includes_systemexit():
    """Source-text pin so a future revert to `except Exception` (which
    would re-introduce the silent-batch-kill bug we hit at episode 19
    of run_20260610_214315) trips a unit test.

    Locked-in form: `except (Exception, SystemExit) as e:` in
    sibb_api_runner.py.
    """
    src = pathlib.Path(R.__file__).read_text()
    # The widened catch must be present.
    assert "except (Exception, SystemExit) as e:" in src, (
        "The per-episode catch in run_corpus must catch SystemExit too; "
        "see Agent-1 reviewer note from the 5-reviewer pass.")
    # The bare-Exception form must NOT survive next to it — that would
    # mean someone added the tuple without removing the original.
    assert src.count("except Exception as e:  # noqa: BLE001 — corral runaway") == 0


def test_run_api_episode_raises_systemexit_on_unknown_generator():
    """The behavioral contract the runner's tuple-catch is there to
    catch. `run_api_episode` reads `args.generator` against the
    GENERATORS dict in sibb_replay; an unknown key raises SystemExit
    with a 'unknown generator' message."""
    import asyncio as _asyncio
    from sibb.api_baseline import sibb_api_assistant as A

    class _Args:
        generator = "totally_bogus_generator_xyz"
        seed = 0
        provider = "gemini"
        model = "gemini-2.5-flash"
        max_turns = 1
        max_tokens = 1
        temperature = 0.0
        llm_timeout = 1.0
        llm_max_retries = 0
        budget_usd_max = None
        retrieval = True
        udid = "47265666-C505-470F-ACDB-C20918D8F909"
        log_dir = "/tmp"
        inject_reader = object()  # bypass pre-runner/sim boot

    with pytest.raises(SystemExit) as excinfo:
        _asyncio.run(A.run_api_episode(_Args()))
    msg = str(excinfo.value)
    assert "unknown generator" in msg
    assert "totally_bogus_generator_xyz" in msg


# ---------------------------------------------------------------------------
# resolve_refs is invoked before apply
# ---------------------------------------------------------------------------


def test_run_api_episode_imports_resolve_refs():
    """Source-text pin: the assistant module imports resolve_refs from
    sibb_refs. Without this, cross-app tasks using SymbolicRef fail at
    setup with 'SymbolicRef is not JSON serializable' inside
    reminders.apply (see run_20260610_215205,
    reminder_with_calendar_event)."""
    from sibb.api_baseline import sibb_api_assistant as A
    src = pathlib.Path(A.__file__).read_text()
    assert "from sibb_refs import resolve_refs" in src
    # And both spec + verify_checks must be resolved (mirroring
    # sibb_episode.py:175-181).
    assert "task.initial_state.spec = resolve_refs(" in src
    assert "task.verify_checks = resolve_refs(task.verify_checks)" in src
