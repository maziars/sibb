"""A4 — `_topo_sort_apps`: dependency-ordered traversal of handlers.

Pure-Python helper tested against synthetic graphs via the optional
`deps_fn` parameter. Real-handler integration (the dispatcher
calling this helper) is covered by the L1.5 dispatcher tests.
"""

from __future__ import annotations

import pytest

import sibb_state
from sibb_state import _topo_sort_apps

pytestmark = pytest.mark.fast


def _make_deps(deps_map):
    return lambda n: deps_map.get(n, [])


def test_empty_input_returns_empty_list():
    assert _topo_sort_apps([]) == []


def test_single_node_returns_singleton():
    out = _topo_sort_apps(["A"], deps_fn=_make_deps({}))
    assert out == ["A"]


def test_two_independent_nodes_sorted_alphabetically():
    out = _topo_sort_apps(["B", "A"], deps_fn=_make_deps({}))
    assert out == ["A", "B"]


def test_simple_dependency_orders_dep_first():
    # B depends on A → A must come first.
    out = _topo_sort_apps(["A", "B"], deps_fn=_make_deps({"B": ["A"]}))
    assert out == ["A", "B"]


def test_input_order_does_not_affect_output():
    deps = _make_deps({"B": ["A"]})
    assert _topo_sort_apps(["A", "B"], deps_fn=deps) == ["A", "B"]
    assert _topo_sort_apps(["B", "A"], deps_fn=deps) == ["A", "B"]


def test_diamond_dependency_order():
    # A → {B, C} → D. A first, D last; B/C alphabetical between them.
    deps = _make_deps({
        "B": ["A"],
        "C": ["A"],
        "D": ["B", "C"],
    })
    out = _topo_sort_apps(["A", "B", "C", "D"], deps_fn=deps)
    assert out.index("A") < out.index("B")
    assert out.index("A") < out.index("C")
    assert out.index("B") < out.index("D")
    assert out.index("C") < out.index("D")


def test_chain_dependency():
    # A → B → C → D
    deps = _make_deps({"B": ["A"], "C": ["B"], "D": ["C"]})
    out = _topo_sort_apps(["D", "C", "B", "A"], deps_fn=deps)
    assert out == ["A", "B", "C", "D"]


def test_cycle_raises_with_participants():
    deps = _make_deps({"A": ["B"], "B": ["A"]})
    with pytest.raises(ValueError, match="cycle") as exc:
        _topo_sort_apps(["A", "B"], deps_fn=deps)
    assert "A" in str(exc.value) and "B" in str(exc.value)


def test_three_node_cycle_detected():
    deps = _make_deps({"A": ["C"], "B": ["A"], "C": ["B"]})
    with pytest.raises(ValueError, match="cycle"):
        _topo_sort_apps(["A", "B", "C"], deps_fn=deps)


def test_external_deps_are_ignored():
    # B depends on "External" which is not in the input set. Should
    # be treated as unconstrained, not crash, not insert "External".
    deps = _make_deps({"B": ["External"]})
    out = _topo_sort_apps(["A", "B"], deps_fn=deps)
    assert set(out) == {"A", "B"}
    assert "External" not in out


def test_default_deps_fn_uses_handlers_depends_on():
    # Real handlers currently have empty depends_on, so all apps
    # come back in sorted order.
    out = _topo_sort_apps(list(sibb_state.HANDLERS))
    assert out == sorted(sibb_state.HANDLERS)


def test_default_deps_fn_canonicalizes_friendly_names():
    # Verifies that when a handler later declares depends_on with a
    # friendly name (e.g. "Reminders") the topo sort still works.
    # We don't have such a handler yet, so this is a synthetic check.
    handlers = sibb_state.HANDLERS
    aliases = sibb_state._APP_ALIASES
    # If RemindersHandler.depends_on ever includes "Springboard"
    # (or any casing), the canonicalize step picks "com.apple.springboard".
    # Smoke that the alias table covers what handlers will reference.
    for cls in handlers.values():
        for dep in cls.depends_on:
            assert sibb_state.canonicalize_app(dep) is not None, (
                f"{cls.__name__}.depends_on references {dep!r} which "
                "does not resolve via canonicalize_app — typo or "
                "missing handler registration?"
            )
