"""L1 tests for sibb/api_baseline/sibb_api_tools.py.

These tests pin two layers of invariants:
  1. Structural — the TOOLS table has the right shape, every tool maps
     to either a Swift handler (for Apple-SDK tools) or the terminal
     answer path, schemas are closed, and the deferred / non-deferred
     buckets satisfy the Anthropic Tool Search 400-on-all-deferred rule.
  2. Behavioral — the dispatcher routes agent.answer to the local slot
     without touching the socket, forwards Swift commands to
     reader._send, surfaces socket errors, and refuses the
     synthetic-update workaround until a per-item delete handler exists
     on the Swift side.
"""

import asyncio
import re
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SWIFT_SETUP = REPO_ROOT / "sibb" / "simulator" / "sibb_xcuitest_setup.sh"

# Ensure `sibb.api_baseline.sibb_api_tools` is importable. The
# api_baseline package and the sibb package both ship empty __init__.py
# files (see test_classification_yaml.py for the rationale).
sys.path.insert(0, str(REPO_ROOT))

from sibb.api_baseline import sibb_api_tools as M  # noqa: E402


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


def test_tool_count_and_names_match_the_locked_catalog():
    """Locked catalog: 11 Apple-SDK tools + 2 system tools +
    agent.answer + agent.search_tools + agent.fail = 16. system.now /
    system.locale give the API agent the date/time/locale primitives
    the UI agent gets implicitly from the screen. agent.fail mirrors
    the UI scaffold's FAIL verb — primarily for the hybrid baseline,
    but harmless on API-only runs (the agent rarely emits it)."""
    expected_names = {
        "eventkit.create_event",
        "eventkit.list_events",
        "eventkit.create_calendar",
        "eventkit.list_calendars",
        "eventkit.create_reminder",
        "eventkit.list_reminders",
        "eventkit.create_list",
        "cn.create_contact",
        "cn.list_contacts",
        "cn.update_contact",
        "mklocalsearch.query",
        "system.now",
        "system.locale",
        "agent.answer",
        "agent.search_tools",
        "agent.fail",
    }
    actual_names = {t.name for t in M.TOOLS}
    assert actual_names == expected_names, (
        f"missing: {expected_names - actual_names}, "
        f"extra: {actual_names - expected_names}")
    assert len(M.TOOLS) == 16
    assert len(M.TOOLS_BY_NAME) == 16


def test_every_tool_has_mcp_shape():
    """Each tool must have a non-empty name + description + input_schema
    that is JSON-Schema-shaped enough that downstream translators can
    consume it."""
    for t in M.TOOLS:
        assert t.name and isinstance(t.name, str)
        assert t.description and len(t.description) > 30, (
            f"{t.name}: description must be keyword-rich for BM25; "
            f"got {len(t.description)} chars")
        assert isinstance(t.input_schema, dict)
        assert t.input_schema.get("type") == "object", (
            f"{t.name}: root input_schema must be 'object', not "
            f"{t.input_schema.get('type')}")
        assert "properties" in t.input_schema


def test_every_swift_backed_tool_has_a_command_type():
    """A Swift-backed tool with no command_type would be undispatchable.
    Python-only tools (agent.answer + agent.search_tools + agent.fail)
    are handled by the dispatcher specially and don't have a
    command_type."""
    PYTHON_ONLY = {"agent.answer", "agent.search_tools", "agent.fail"}
    for t in M.TOOLS:
        if t.name in PYTHON_ONLY:
            assert t.command_type is None, (
                f"{t.name} is Python-only — should have command_type=None")
        else:
            assert t.command_type is not None, (
                f"{t.name} is Swift-backed — must set command_type")


def test_every_command_type_exists_in_swift_setup():
    """Every Swift command we plan to send must have a `case` in
    SIBBServer.swift (via sibb_xcuitest_setup.sh). Otherwise the socket
    returns 'unknown' and the tool call always fails."""
    if not SWIFT_SETUP.exists():
        pytest.skip("sibb_xcuitest_setup.sh missing")
    swift = SWIFT_SETUP.read_text()
    for t in M.TOOLS:
        if t.command_type is None:
            continue
        pattern = re.compile(rf'case "{re.escape(t.command_type)}"')
        assert pattern.search(swift), (
            f"tool {t.name} requires Swift case "
            f'"{t.command_type}" which is missing')


def test_input_schemas_are_closed():
    """`additionalProperties: False` keeps Anthropic/OpenAI strict-mode
    happy and stops the agent from sending extraneous keys that the
    Swift handler would silently ignore."""
    for t in M.TOOLS:
        if t.is_terminal:
            # agent.answer's payload is intentionally open by design.
            assert t.input_schema.get("additionalProperties") is False, (
                f"{t.name}: even the terminal tool's outer object must "
                "be closed so providers can't inject extra fields")
            continue
        assert t.input_schema.get("additionalProperties") is False, (
            f"{t.name}: input_schema is open — strict-mode FC will "
            "either reject or silently accept extra keys")


def test_required_fields_are_strictly_subsets_of_properties():
    """A `required` entry that isn't in `properties` is a spec bug; the
    schema would never validate any input."""
    for t in M.TOOLS:
        props = set(t.input_schema.get("properties", {}).keys())
        required = set(t.input_schema.get("required", []))
        leaked = required - props
        assert not leaked, (
            f"{t.name}: required {leaked} not in properties {props}")


def test_agent_answer_is_terminal_and_always_loaded():
    """The terminal tool must never be deferred — the agent must always
    be able to reach it to end a read-only task."""
    answer = M.TOOLS_BY_NAME["agent.answer"]
    assert answer.is_terminal is True
    assert answer.defer_loading is False
    assert answer.command_type is None


def test_non_deferred_reserve_is_exactly_the_three_agent_meta_tools():
    """Pure Design A reserve: agent.answer + agent.search_tools +
    agent.fail (the terminal channels + retrieval entry-point) and
    NOTHING ELSE. Every Apple-SDK tool must be discovered via
    agent.search_tools — no curation shortcut. Anthropic's
    all-deferred-400 constraint is satisfied by the agent.* trio
    being non-deferred."""
    reserve = M.non_deferred_tool_names()
    assert set(reserve) == {
        "agent.answer", "agent.search_tools", "agent.fail",
    }, (
        f"reserve drifted from the pure Design A set; got {reserve}. "
        "Adding curation shortcuts undermines the paper's claim that the "
        "API agent discovers tools via search, not via author preload.")


def test_deferred_plus_non_deferred_partition_the_catalog():
    """No tool double-counted; no tool missed."""
    deferred = set(M.deferred_tool_names())
    non_deferred = set(M.non_deferred_tool_names())
    assert deferred.isdisjoint(non_deferred), (
        f"both sets share: {deferred & non_deferred}")
    assert deferred | non_deferred == set(M.TOOLS_BY_NAME.keys())


# ---------------------------------------------------------------------------
# mcp_tools() canonical output
# ---------------------------------------------------------------------------


def test_mcp_tools_emits_canonical_mcp_shape():
    """The wire-format translator in sibb_llm.py consumes this list. The
    contract: `name`, `description`, `inputSchema` (camelCase, per MCP)
    — and nothing else from the dataclass."""
    out = M.mcp_tools()
    assert len(out) == 16
    for entry in out:
        assert set(entry.keys()) == {"name", "description", "inputSchema"}, (
            f"unexpected keys in {entry.get('name')}: {set(entry.keys())}")
        # Ensure no dispatch metadata leaked.
        assert "command_type" not in entry
        assert "defer_loading" not in entry


# ---------------------------------------------------------------------------
# Dispatcher behavior
# ---------------------------------------------------------------------------


class _FakeReader:
    """Records every cmd sent over the socket; returns a canned response."""

    def __init__(self, response: Optional[Dict[str, Any]] = None,
                  raise_exc: Optional[BaseException] = None) -> None:
        self.sent: List[Dict[str, Any]] = []
        self._response = response if response is not None else {"ok": True}
        self._raise_exc = raise_exc

    async def _send(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        self.sent.append(cmd)
        if self._raise_exc:
            raise self._raise_exc
        return self._response


def test_maybe_parse_json_answer_parses_object_strings():
    """Gemini sometimes stringifies dict answers. The dispatcher
    auto-parses so the verifier's path-walk sees structured data."""
    out = M._maybe_parse_json_answer('{"items": [{"title": "x"}]}')
    assert out == {"items": [{"title": "x"}]}


def test_maybe_parse_json_answer_parses_array_strings():
    out = M._maybe_parse_json_answer('[{"title": "x"}, {"title": "y"}]')
    assert out == [{"title": "x"}, {"title": "y"}]


def test_maybe_parse_json_answer_handles_leading_whitespace():
    out = M._maybe_parse_json_answer('  \n  {"a": 1}')
    assert out == {"a": 1}


def test_maybe_parse_json_answer_leaves_dicts_alone():
    """If the answer is ALREADY a dict, don't touch it."""
    d = {"items": [{"title": "x"}]}
    assert M._maybe_parse_json_answer(d) is d


def test_maybe_parse_json_answer_leaves_scalars_alone():
    assert M._maybe_parse_json_answer(42) == 42
    assert M._maybe_parse_json_answer(3.14) == 3.14
    assert M._maybe_parse_json_answer(True) is True
    assert M._maybe_parse_json_answer(None) is None


def test_maybe_parse_json_answer_leaves_freeform_text_alone():
    """A natural-language answer shouldn't trigger the parser. Only
    strings starting with `{` or `[` are candidates."""
    text = "There are 5 reminders due today."
    assert M._maybe_parse_json_answer(text) == text


def test_maybe_parse_json_answer_leaves_invalid_json_alone():
    """If parsing fails, return the original string — we never corrupt
    the agent's intent."""
    bad = '{"unterminated"'
    assert M._maybe_parse_json_answer(bad) == bad


def test_maybe_parse_json_answer_handles_empty_string():
    assert M._maybe_parse_json_answer("") == ""
    assert M._maybe_parse_json_answer("   ") == "   "


@pytest.mark.asyncio
async def test_dispatcher_parses_stringified_answer_at_terminal_path():
    """End-to-end: when the model passes agent.answer({"answer":
    '{"items": [...]}'} ) as a string, the dispatcher's terminal path
    auto-parses so verify_via sees the structured payload."""
    reader = _FakeReader()
    disp = M.APIToolDispatcher(reader)
    payload_str = '{"items": [{"title": "Schedule reviews"}]}'
    result = await disp.dispatch(
        "agent.answer", {"answer": payload_str})
    assert result.ok is True
    assert result.terminal is True
    # The stored answer is the PARSED dict, not the original string.
    assert disp.answer_payload == {
        "answer": {"items": [{"title": "Schedule reviews"}]}}


@pytest.mark.asyncio
async def test_dispatcher_routes_agent_answer_without_socket_touch():
    """agent.answer captures the payload locally and marks terminal=True
    — no socket round-trip."""
    reader = _FakeReader()
    disp = M.APIToolDispatcher(reader)

    result = await disp.dispatch(
        "agent.answer", {"answer": {"count": 5, "titles": ["a", "b"]}})

    assert result.ok is True
    assert result.terminal is True
    assert reader.sent == [], (
        "agent.answer must NOT touch the socket; got "
        f"sent={reader.sent}")
    assert disp.answer_payload == {
        "answer": {"count": 5, "titles": ["a", "b"]}}
    assert result.latency_ms >= 0


@pytest.mark.asyncio
async def test_dispatcher_forwards_swift_command_with_type_and_args():
    """A non-terminal call MUST send {type: <command_type>, **args} to
    the socket and forward the response verbatim as the payload."""
    canned = {"ok": True, "identifier": "abc-123", "name": "Travel"}
    reader = _FakeReader(response=canned)
    disp = M.APIToolDispatcher(reader)

    result = await disp.dispatch(
        "eventkit.create_list", {"name": "Travel"})

    assert reader.sent == [{"type": "create_list", "name": "Travel"}]
    assert result.ok is True
    assert result.payload == canned
    assert result.terminal is False


@pytest.mark.asyncio
async def test_dispatcher_surfaces_socket_errors_as_ok_false():
    """If the socket raises (closed, timeout, JSON garbage), the
    dispatcher MUST return ok=False with a structured error rather
    than propagating — the agent loop catches False and decides
    whether to retry or end."""
    reader = _FakeReader(
        raise_exc=ConnectionResetError("socket closed by server"))
    disp = M.APIToolDispatcher(reader)

    result = await disp.dispatch(
        "eventkit.create_list", {"name": "Travel"})

    assert result.ok is False
    assert "socket error" in result.payload["error"]
    assert "ConnectionResetError" in result.payload["error"]


@pytest.mark.asyncio
async def test_dispatcher_rejects_unknown_tool_name():
    """A tool name not in TOOLS_BY_NAME is a contract violation upstream
    — the translator should never emit it — but we return ok=False
    instead of crashing the agent loop."""
    reader = _FakeReader()
    disp = M.APIToolDispatcher(reader)

    result = await disp.dispatch("eventkit.delete_event", {"id": "x"})

    assert result.ok is False
    assert "unknown tool" in result.payload["error"]
    assert reader.sent == []


@pytest.mark.asyncio
async def test_dispatcher_propagates_ok_false_from_swift():
    """Swift handlers return {ok: false, error: ...} on validation
    failures. The dispatcher must surface that as ToolCallResult.ok=False
    so the agent loop can react."""
    canned = {"ok": False, "error": "title and list required"}
    reader = _FakeReader(response=canned)
    disp = M.APIToolDispatcher(reader)

    result = await disp.dispatch(
        "eventkit.create_reminder", {"title": "", "list": ""})

    assert result.ok is False
    assert result.payload == canned


# ---------------------------------------------------------------------------
# Reminder field-carry helper
# ---------------------------------------------------------------------------


def test_carry_reminder_fields_translates_due_to_due_iso():
    """`list_reminders` emits `due`; `create_reminder` accepts `due_iso`.
    The carry helper must rename — otherwise wipe+create silently drops
    the due date and the verifier's preservation check fails."""
    row = {"title": "Pay rent", "list": "Bills",
            "due": "2026-07-01", "notes": "via Zelle"}
    carried = M._carry_reminder_fields(row)
    assert carried == {"due_iso": "2026-07-01", "notes": "via Zelle"}


def test_carry_reminder_fields_preserves_recurrence_and_priority():
    """The §3.bis hazard: dropping recurrence on wipe+create turns a
    monthly reminder into a one-shot. The helper must carry it through."""
    row = {
        "title": "Standup",
        "list": "Work",
        "priority": 5,
        "recurrence": {"frequency": "daily", "interval": 1},
        "completed": False,
    }
    carried = M._carry_reminder_fields(row)
    assert carried["priority"] == 5
    assert carried["recurrence"] == {"frequency": "daily", "interval": 1}
    assert carried["completed"] is False


def test_carry_reminder_fields_deep_copies_recurrence_dict():
    """Mutating the carried dict must not affect the listed row — the
    runner sometimes layers a mutation onto the carried fields and we
    don't want to corrupt the trajectory log's listed_row snapshot."""
    row = {"title": "x", "list": "y",
            "recurrence": {"frequency": "weekly", "interval": 2}}
    carried = M._carry_reminder_fields(row)
    carried["recurrence"]["interval"] = 99
    assert row["recurrence"]["interval"] == 2


def test_carry_reminder_fields_omits_missing_optional_keys():
    """Fields not present on the listed row must NOT show up in carried.
    A present-with-null create kwarg would override EventKit's default."""
    row = {"title": "x", "list": "y"}
    carried = M._carry_reminder_fields(row)
    assert carried == {}


# ---------------------------------------------------------------------------
# Synthetic-update gating
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bucket-2 fixes
# ---------------------------------------------------------------------------


def test_nested_schemas_have_additional_properties_false():
    """Bucket-2 fix #9: OpenAI strict mode requires
    additionalProperties:false on EVERY object schema. We had it on the
    top-level tool schemas but not on _RECURRENCE_SCHEMA / _LABELED_VALUE_ITEM
    / _POSTAL_ADDRESS_ITEM. Pin each."""
    assert M._RECURRENCE_SCHEMA["additionalProperties"] is False
    assert M._LABELED_VALUE_ITEM["additionalProperties"] is False
    assert M._POSTAL_ADDRESS_ITEM["additionalProperties"] is False


@pytest.mark.asyncio
async def test_dispatcher_command_type_authoritative_against_args_injection():
    """Bucket-2 fix #10: a model could emit `type` as part of its args
    dict (no schema enforces this on Gemini). dict.update(args) would
    let the agent override our command_type and reach a different
    Swift handler. We must build cmd with command_type LAST so it's
    authoritative."""
    reader = _FakeReader(response={"ok": True})
    disp = M.APIToolDispatcher(reader)
    # Simulate an attack: agent emits create_list args plus a malicious
    # `type` field that would route to wipe_reminders.
    await disp.dispatch(
        "eventkit.create_list",
        {"name": "Travel", "type": "wipe_reminders"})
    sent = reader.sent[0]
    # The sent command's `type` is OUR command_type, not the agent's.
    assert sent["type"] == "create_list"
    assert sent["name"] == "Travel"


# ---------------------------------------------------------------------------
# BM25 tool index
# ---------------------------------------------------------------------------


def test_bm25_tokenizer_splits_namespace_and_snake_case():
    """Tools have names like `eventkit.create_reminder` and descriptions
    full of camelCase + snake_case. The tokenizer must split on both
    so a query for 'reminder' matches `create_reminder`."""
    toks = M._tokenize_for_bm25("eventkit.create_reminder Create a "
                                  "reminder via EventKit's "
                                  "EKReminder.recurrenceRules")
    assert "eventkit" in toks
    assert "create" in toks
    assert "reminder" in toks
    # All lowercase.
    assert all(t == t.lower() for t in toks)


def test_bm25_index_top_k_returns_reserve_plus_relevant_retrievals():
    """For a Reminders query, BM25 should retrieve eventkit.* tools
    over cn.* / mklocalsearch. The non-deferred reserve is always
    included regardless of rank."""
    idx = M.BM25ToolIndex()
    out = idx.top_k("create a new reminder under the Tomorrow list", k=3)
    # Reserve is present (agent.answer + the eventkit-prefixed ones we
    # locked as non-deferred):
    assert "agent.answer" in out
    # BM25 retrieved at least one explicit reminder/eventkit tool:
    assert any("reminder" in name or "eventkit" in name for name in out)
    # mklocalsearch (place query) should NOT be in the top-3 retrieval
    # — but might be in reserve. It's not in our non-deferred reserve.
    # Verify it does NOT appear (since it has 0 BM25 overlap and isn't
    # in the reserve).
    assert "mklocalsearch.query" not in out


def test_bm25_index_top_k_returns_disjoint_results_under_irrelevant_query():
    """Even with no query match, the result is still the non-deferred
    reserve (so the agent can always end the episode via agent.answer)."""
    idx = M.BM25ToolIndex()
    out = idx.top_k("xylophone elephant umbrella", k=3)
    assert "agent.answer" in out


def test_bm25_search_finds_system_now_for_date_queries():
    """system.now must be discoverable via searches about dates and
    times — otherwise the agent can't use it. We don't mention
    system.now in the system prompt, so BM25 discoverability is the
    only path the model has."""
    # Build the index the dispatcher's search uses (deferred subset,
    # empty reserve so the result is pure rank).
    deferred_tools = [t for t in M.TOOLS if t.defer_loading]
    idx = M.BM25ToolIndex(deferred_tools, reserve_names=[])
    for query in (
        "what is today's date",
        "current date and time",
        "get today's date",
        "what day of the week is it",
        "what's the date today",
    ):
        hits = idx.top_k(query, k=5)
        assert "system.now" in hits, (
            f"system.now not retrieved by query {query!r}; got {hits}. "
            "The description must include the keywords agents use to "
            "ask about dates.")


def test_bm25_search_finds_system_locale_for_region_queries():
    """system.locale must surface for locale-related queries."""
    deferred_tools = [t for t in M.TOOLS if t.defer_loading]
    idx = M.BM25ToolIndex(deferred_tools, reserve_names=[])
    for query in (
        "what language is the device set to",
        "first day of the week",
        "get the locale",
        "what region is the device",
    ):
        hits = idx.top_k(query, k=5)
        assert "system.locale" in hits, (
            f"system.locale not retrieved by query {query!r}; got {hits}.")


def test_bm25_index_uses_TOOLS_by_default():
    """The default constructor reads from the module-level TOOLS list."""
    idx = M.BM25ToolIndex()
    # 16 tools indexed (Apple-SDK + agent.* + system.*).
    assert len(idx._docs) == 16


@pytest.mark.asyncio
async def test_synthetic_update_reminder_raises_until_delete_handler_lands():
    """v1 has no per-item Swift delete handler. The workaround MUST raise
    rather than silently mutate state — surfacing the gap in the
    trajectory rather than producing a half-baked update that the
    verifier might accidentally pass."""
    reader = _FakeReader()
    disp = M.APIToolDispatcher(reader)
    with pytest.raises(NotImplementedError, match="per-item delete handler"):
        await disp.synthetic_update_reminder(
            list_name="Bills",
            match_title="Pay rent",
            mutations={"recurrence": {"frequency": "monthly", "interval": 1}})
    # No socket traffic — the gating MUST be local.
    assert reader.sent == []
