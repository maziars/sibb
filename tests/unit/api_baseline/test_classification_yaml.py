"""Day-1 invariants for sibb/api_baseline/classification.yaml.

These tests pin the foundation decisions made during the 5-reviewer Bucket-A
pass . If any of
these fail, the upcoming code in sibb/api_baseline/sibb_api_tools.py and
sibb_api_runner.py will silently misbehave — either the runner will try to
load a generator that doesn't exist, the dispatcher will route to a Swift
handler that doesn't exist, or the κ summary will be off.

We deliberately parse the YAML with stdlib regex rather than PyYAML — stock
Python 3.9 from CommandLineTools (the project's pinned interpreter) does not
ship PyYAML, and the foundations should not introduce a new install
requirement just to test themselves.
"""

import re
import os
import pathlib
from typing import Dict, List, Set, Tuple

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
YAML_PATH = REPO_ROOT / "sibb" / "api_baseline" / "classification.yaml"
GENERATOR_SOURCE = (
    REPO_ROOT / "sibb" / "benchmark" / "sibb_task_generator_v3.py")
SWIFT_SETUP = REPO_ROOT / "sibb" / "simulator" / "sibb_xcuitest_setup.sh"


def _read(path: pathlib.Path) -> str:
    with open(path, "r") as fh:
        return fh.read()


def _section_bounds(text: str) -> Tuple[int, int, int]:
    """Locate the byte offsets of the three list-valued top-level blocks."""
    a = text.index("\ntasks:\n")
    b = text.index("\nhybrid_tasks_for_kappa:\n")
    c = text.index("\nsummary:\n")
    return a, b, c


def _parse_tasks(text: str, start: int, end: int) -> List[Dict[str, str]]:
    """Walk the task entries in a YAML section and return their flat fields.

    Each entry looks like:
        - generator: name_here
          class: api_only
          api_tools: [a, b, c]
          ui_verbs: []
          framework: SomeFramework
          rationale: >
            multi-line text...

    We capture `generator`, `class`, and `api_tools` since those are the
    fields the runner consumes. Other fields go through unchecked at this
    layer (the operational-definition rubric covers them at a higher level).
    """
    section = text[start:end]
    entries: List[Dict[str, str]] = []
    cur: Dict[str, str] = {}
    for line in section.splitlines():
        m_gen = re.match(r"^  - generator: (\S+)", line)
        m_cls = re.match(r"^    class: (\S+)", line)
        m_tools = re.match(r"^    api_tools: \[(.*?)\]", line)
        if m_gen:
            if cur:
                entries.append(cur)
            cur = {"generator": m_gen.group(1)}
        elif m_cls:
            cur["class"] = m_cls.group(1)
        elif m_tools:
            raw = m_tools.group(1).strip()
            tools = [t.strip() for t in raw.split(",")] if raw else []
            cur["api_tools"] = tools  # type: ignore[assignment]
    if cur:
        entries.append(cur)
    return entries


# --- Fixtures (computed once) -----------------------------------------------

YAML_TEXT = _read(YAML_PATH)
A, B, C = _section_bounds(YAML_TEXT)
TASKS = _parse_tasks(YAML_TEXT, A, B)
HYBRID = _parse_tasks(YAML_TEXT, B, C)


# --- Generator-name resolution ---------------------------------------------

def test_yaml_parses_to_expected_counts():
    """After Bucket-1 (10-critic panel reclassification): 19 + 7 = 26
    scored, plus 5 hybrid. The 3 Reminders tasks that relied on the
    list+wipe+create workaround (gen_make_reminder_recurring,
    gen_set_priority, gen_add_notes_to_reminder) moved from api_only
    to hybrid because the dispatcher's synthetic_update_reminder helper
    raises NotImplementedError until a per-item Swift delete handler
    is built."""
    classes = [t["class"] for t in TASKS]
    assert classes.count("api_only") == 19, (
        f"expected 19 api_only, got {classes.count('api_only')}")
    assert classes.count("ui_only") == 7, (
        f"expected 7 ui_only, got {classes.count('ui_only')}")
    assert len(TASKS) == 26
    assert len(HYBRID) == 5


def test_every_generator_name_resolves_to_a_def():
    """Every `generator:` in the YAML must exist as a def in sibb_task_generator_v3.py."""
    src = _read(GENERATOR_SOURCE)
    defined = set(re.findall(r"^def (gen_\w+)\(", src, re.M))
    yaml_names = [t["generator"] for t in TASKS] + [
        t["generator"] for t in HYBRID]
    missing = [n for n in yaml_names if n not in defined]
    assert not missing, (
        f"YAML references {len(missing)} undefined generators: {missing}")


def test_no_duplicate_generator_names_across_sections():
    """A generator must not be classified twice (once in tasks, once in hybrid)."""
    all_names = [t["generator"] for t in TASKS] + [
        t["generator"] for t in HYBRID]
    duplicates = [n for n in set(all_names) if all_names.count(n) > 1]
    assert not duplicates, f"duplicate classifications: {duplicates}"


def test_dropped_legacy_generators_are_actually_gone():
    """The 3 generators dropped during Bucket A — pin they didn't sneak back."""
    DROPPED = {
        "gen_update_contact_phone",
        "gen_partial_feasibility_blocking",
        "gen_ambiguous_contact_missing_phone",
    }
    yaml_names = {t["generator"] for t in TASKS} | {
        t["generator"] for t in HYBRID}
    leaked = DROPPED & yaml_names
    assert not leaked, (
        f"legacy generators that lack verify_checks crept back: {leaked}")


# --- Tool/handler consistency ----------------------------------------------

# Tool namespace → Swift handler command type. The dispatcher uses this map.
EXPECTED_TOOL_TO_HANDLER = {
    "eventkit.create_event": "create_event",
    "eventkit.list_events": "list_events",
    "eventkit.create_calendar": "create_calendar",
    "eventkit.list_calendars": "list_calendars",
    "eventkit.create_reminder": "create_reminder",
    "eventkit.list_reminders": "list_reminders",
    "eventkit.create_list": "create_list",
    "cn.create_contact": "create_contact",
    "cn.list_contacts": "list_contacts",
    "cn.update_contact": "update_contact",
    "mklocalsearch.query": "geocode_query",
    "system.now": "system_now",
    "system.locale": "system_locale",
    "agent.answer": None,         # Python-only; no Swift handler
    "agent.search_tools": None,   # Python-only; dispatcher handles
}


def test_all_yaml_tools_are_known_namespace_names():
    """Every api_tools entry must be a tool we have in the expected map."""
    seen: Set[str] = set()
    for t in TASKS:
        for tool in t.get("api_tools", []):
            seen.add(tool)
    unknown = seen - set(EXPECTED_TOOL_TO_HANDLER)
    assert not unknown, (
        f"api_tools references unknown namespaces: {unknown}")


def test_swift_handlers_for_every_non_agent_tool_exist():
    """Every Swift handler the dispatcher will route to must exist."""
    if not SWIFT_SETUP.exists():
        pytest.skip("sibb_xcuitest_setup.sh not present")
    swift = _read(SWIFT_SETUP)
    for tool, handler in EXPECTED_TOOL_TO_HANDLER.items():
        if handler is None:
            continue
        # Swift handlers are dispatched by `case "<name>":` blocks in
        # SIBBServer.swift, which sibb_xcuitest_setup.sh writes inline.
        pattern = re.compile(rf'case "{re.escape(handler)}"')
        assert pattern.search(swift), (
            f'tool {tool} maps to Swift handler "{handler}" which is missing '
            f"from sibb_xcuitest_setup.sh")


def test_ui_only_tasks_have_empty_api_tools():
    """ui_only is the by-construction floor — there must be no tools that
    could be tried. Catches a sneak-edit where someone adds a tool to a
    ui_only entry "just to see" — which would silently undermine the
    headline 0% claim."""
    for t in TASKS:
        if t["class"] == "ui_only":
            tools = t.get("api_tools", [])
            assert tools == [], (
                f"{t['generator']} is ui_only but has api_tools={tools!r}")


def test_api_only_tasks_have_at_least_one_api_tool():
    """An api_only task with zero tools is structurally suspicious."""
    for t in TASKS:
        if t["class"] == "api_only":
            tools = t.get("api_tools", [])
            assert len(tools) >= 1, (
                f"{t['generator']} is api_only but lists no api_tools")


# --- Summary self-consistency ---------------------------------------------

def test_summary_block_matches_actual_counts():
    """The summary: block at the bottom of the YAML claims specific counts.
    They must match what the task lists actually contain."""
    summary_text = YAML_TEXT[C:]
    m_api = re.search(r"^  api_only: (\d+)", summary_text, re.M)
    m_ui = re.search(r"^  ui_only: (\d+)", summary_text, re.M)
    m_hyb = re.search(r"^  hybrid_for_kappa: (\d+)", summary_text, re.M)
    m_total = re.search(r"^  total_scored: (\d+)", summary_text, re.M)
    m_kappa_total = re.search(r"^  total_for_kappa: (\d+)", summary_text, re.M)
    assert m_api and m_ui and m_hyb and m_total and m_kappa_total, (
        "summary: block is missing one of the required count fields")

    classes = [t["class"] for t in TASKS]
    api_n = classes.count("api_only")
    ui_n = classes.count("ui_only")
    hybrid_n = len(HYBRID)

    assert int(m_api.group(1)) == api_n
    assert int(m_ui.group(1)) == ui_n
    assert int(m_hyb.group(1)) == hybrid_n
    assert int(m_total.group(1)) == api_n + ui_n
    assert int(m_kappa_total.group(1)) == api_n + ui_n + hybrid_n
