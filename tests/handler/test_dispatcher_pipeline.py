"""A4 — `apply_initial_state` runs per-app `reset → apply` pipelines
in `depends_on`-topo order.

Uses synthetic handler classes injected into the registry via
monkeypatch so we can construct multi-app graphs (including
explicit `depends_on`) without depending on real Apple frameworks.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar, List

import pytest

import sibb_state

pytestmark = pytest.mark.fake_reader


def _install_synthetic_handlers(monkeypatch, *handler_classes):
    new_registry = {cls.bundle_id: cls for cls in handler_classes}
    monkeypatch.setattr(sibb_state, "HANDLERS", new_registry)
    monkeypatch.setattr(
        sibb_state, "_APP_ALIASES", sibb_state._build_app_aliases()
    )


def _task(apps, spec):
    return SimpleNamespace(
        apps=apps,
        initial_state=SimpleNamespace(spec=spec),
    )


class _TracingHandler:
    """Base for synthetic handlers — records reset/apply calls into a list."""
    bundle_id: ClassVar[str] = ""
    tcc_services: ClassVar[List[str]] = []
    pre_runner: ClassVar[bool] = False
    pre_runner_kinds: ClassVar[List[str]] = []
    depends_on: ClassVar[List[str]] = []
    log: ClassVar[list] = []

    def __init__(self, reader=None):
        self.reader = reader

    async def reset(self):
        self.log.append(("reset", self.bundle_id))

    async def apply(self, entry):
        self.log.append(("apply", self.bundle_id, entry.get("type")))


def _make_handler(name, bundle_id, depends_on=()):
    cls = type(name, (_TracingHandler,), {
        "bundle_id": bundle_id,
        "tcc_services": [],
        "pre_runner": False,
        "pre_runner_kinds": [],
        "depends_on": list(depends_on),
        "log": [],
    })
    return cls


async def test_single_app_resets_then_applies(monkeypatch):
    A = _make_handler("AHandler", "com.test.a")
    _install_synthetic_handlers(monkeypatch, A)

    task = _task(["A"], [
        {"app": "A", "type": "x"},
        {"app": "A", "type": "y"},
    ])

    report = await sibb_state.apply_initial_state(reader=None, task=task)

    assert A.log == [
        ("reset", "com.test.a"),
        ("apply", "com.test.a", "x"),
        ("apply", "com.test.a", "y"),
    ]
    assert report["reset"] == ["com.test.a"]
    assert len(report["applied"]) == 2
    assert report["errors"] == []


async def test_two_apps_independent_reset_in_alphabetical_order(monkeypatch):
    A = _make_handler("AHandler", "com.test.a")
    B = _make_handler("BHandler", "com.test.b")
    _install_synthetic_handlers(monkeypatch, A, B)
    shared_log = []
    A.log = shared_log
    B.log = shared_log

    task = _task(["B", "A"], [
        {"app": "B", "type": "b1"},
        {"app": "A", "type": "a1"},
    ])

    await sibb_state.apply_initial_state(reader=None, task=task)

    a_reset = shared_log.index(("reset", "com.test.a"))
    b_reset = shared_log.index(("reset", "com.test.b"))
    a_apply = shared_log.index(("apply", "com.test.a", "a1"))
    b_apply = shared_log.index(("apply", "com.test.b", "b1"))

    assert a_reset < b_reset, "a alphabetically first should reset first"
    assert a_reset < a_apply, "per-app pipeline: reset before apply"
    assert b_reset < b_apply
    # The pipelines must NOT interleave — A's apply happens before B's reset.
    assert a_apply < b_reset, (
        "expected per-app pipeline (reset-A, apply-A..., reset-B, apply-B...) "
        f"but got: {shared_log!r}"
    )


async def test_depends_on_forces_dep_first(monkeypatch):
    # B depends on A; even though B is alphabetically second AND
    # listed first in task.apps, A must still reset first.
    A = _make_handler("AHandler", "com.test.a")
    B = _make_handler("BHandler", "com.test.b", depends_on=["com.test.a"])
    _install_synthetic_handlers(monkeypatch, A, B)
    shared_log = []
    A.log = shared_log
    B.log = shared_log

    task = _task(["B"], [
        {"app": "B", "type": "b1"},
        {"app": "A", "type": "a1"},
    ])

    await sibb_state.apply_initial_state(reader=None, task=task)

    assert shared_log.index(("reset", "com.test.a")) < \
        shared_log.index(("reset", "com.test.b"))


async def test_depends_on_canonicalizes_friendly_name(monkeypatch):
    # Friendly-name deps must canonicalize to bundle ids before topo.
    A = _make_handler("AppleHandler", "com.test.apple")
    B = _make_handler("BananaHandler", "com.test.banana",
                       depends_on=["Apple"])   # friendly name, no Handler suffix
    _install_synthetic_handlers(monkeypatch, A, B)
    shared_log = []
    A.log = shared_log
    B.log = shared_log

    task = _task(["Banana", "Apple"], [])
    await sibb_state.apply_initial_state(reader=None, task=task)

    assert shared_log.index(("reset", "com.test.apple")) < \
        shared_log.index(("reset", "com.test.banana"))


async def test_duplicate_app_references_collapse_to_single_reset(monkeypatch):
    A = _make_handler("AHandler", "com.test.a")
    _install_synthetic_handlers(monkeypatch, A)

    task = _task(
        ["A", "a", "com.test.a"],   # 3 ways of referring to the same app
        [{"app": "A", "type": "x"}],
    )

    await sibb_state.apply_initial_state(reader=None, task=task)

    reset_count = sum(1 for entry in A.log if entry[0] == "reset")
    assert reset_count == 1, (
        f"expected one reset per app, got {reset_count}: {A.log!r}"
    )


async def test_unknown_app_reports_error_but_processes_known(monkeypatch):
    A = _make_handler("AHandler", "com.test.a")
    _install_synthetic_handlers(monkeypatch, A)

    task = _task(["A", "NotARealApp"], [
        {"app": "A", "type": "x"},
        {"app": "NotARealApp", "type": "boom"},
    ])

    report = await sibb_state.apply_initial_state(reader=None, task=task)

    assert any("NotARealApp" in e for e in report["errors"]), (
        f"expected error mentioning NotARealApp; got {report['errors']!r}"
    )
    assert ("reset", "com.test.a") in A.log
    assert ("apply", "com.test.a", "x") in A.log


async def test_cycle_in_depends_on_reports_error(monkeypatch):
    A = _make_handler("AHandler", "com.test.a", depends_on=["com.test.b"])
    B = _make_handler("BHandler", "com.test.b", depends_on=["com.test.a"])
    _install_synthetic_handlers(monkeypatch, A, B)
    shared_log = []
    A.log = shared_log
    B.log = shared_log

    task = _task(["A", "B"], [])
    report = await sibb_state.apply_initial_state(reader=None, task=task)

    assert any("cycle" in e for e in report["errors"]), (
        f"expected cycle error; got {report['errors']!r}"
    )
    # Pipeline aborted on cycle, so nothing got reset.
    assert shared_log == []


async def test_reset_failure_does_not_stop_pipeline(monkeypatch):
    # Current behavior: a reset that raises is logged in errors but
    # apply still runs (graceful degradation). A4 preserves this.
    class FlakyHandler(_TracingHandler):
        bundle_id = "com.test.flaky"
        depends_on = []
        log: ClassVar[list] = []

        async def reset(self):
            FlakyHandler.log.append(("reset-attempt", self.bundle_id))
            raise RuntimeError("simulated reset failure")

        async def apply(self, entry):
            FlakyHandler.log.append(("apply", self.bundle_id, entry["type"]))

    FlakyHandler.log = []
    _install_synthetic_handlers(monkeypatch, FlakyHandler)

    task = _task(["Flaky"], [
        {"app": "Flaky", "type": "still_runs"},
    ])

    report = await sibb_state.apply_initial_state(reader=None, task=task)

    assert ("reset-attempt", "com.test.flaky") in FlakyHandler.log
    assert ("apply", "com.test.flaky", "still_runs") in FlakyHandler.log
    assert any("reset failed" in e for e in report["errors"])
    assert len(report["applied"]) == 1
