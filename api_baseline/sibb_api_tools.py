"""MCP-shape tool definitions and dispatcher for the Option A+ API agent.

This module owns three things:

1. **Tool definitions.** Eleven MCP-style entries (`name`, `description`,
   `input_schema`) plus a tiny piece of dispatch metadata (the Swift handler
   `command_type`, terminal-tool flag, and `defer_loading` hint for Tool
   Search BM25). The input schemas are JSON-Schema Draft 2020-12 and pin
   each tool to the same fields the corresponding Swift handler in
   `sibb/simulator/sibb_xcuitest_setup.sh` actually accepts.

2. **The dispatcher.** Given a tool call (`name`, `args`), routes to the
   XCUITest socket (for the 10 Apple-SDK tools) or to the Python-side
   answer-capture path (for `agent.answer`). For tools the Swift surface
   lacks an update for (`update_event`, `update_reminder`), implements the
   `list+wipe+create` workaround mandated by `operational_definition.md`
   §3.bis — the dispatcher copies every public field from the listed item
   before recreating, and stamps `synthetic_update: true` on the result.

3. **The `agent.answer` channel.** Read-style tasks complete by emitting
   `agent.answer`; the answer payload routes through the same
   `context["agent_answer"]` slot the UI baseline uses, so the existing
   `sibb_verify.verify_via(...)` pipeline sees one shape regardless of
   baseline.

Per-provider wire-format translation does NOT live here — that lives in
`sibb/benchmark/sibb_llm.py` once that extension lands. This module emits
the canonical MCP shape and lets the LLM client translate.

CLAUDE.md compliance notes:
- Python 3.9 — no `list[X]`, no `X | None`, no `X | Y`.
- No edits to `sibb/benchmark/` (we import `XCUITestReader`).
- No edits to `SIBBServer.swift`.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# We import the XCUITest socket client from `sibb/simulator/`. Adding the
# repo `sibb/` root to sys.path keeps the existing benchmark/simulator code
# importable without restructuring it as a package — the UI baseline does
# the same. The module-form invocation guidance in README.md is honored:
# `python -m sibb.api_baseline.sibb_api_tools` would set sys.path correctly,
# but tests and the runner both already arrange that.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
for _sub in ("sibb/simulator", "sibb/benchmark"):
    _p = os.path.join(_REPO_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# MCP-shape tool definitions
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class APITool:
    """One tool in the API-only agent's catalog.

    The first three fields (`name`, `description`, `input_schema`) are the
    canonical MCP shape. The remaining fields are dispatch metadata kept
    out of the wire format by the per-provider translator in `sibb_llm.py`.
    """
    name: str
    description: str
    input_schema: Dict[str, Any]
    # Dispatch metadata:
    command_type: Optional[str]  # Swift command type; None for python-only
    is_terminal: bool = False    # True for agent.answer
    # `defer_loading` is True for tools we want Tool Search BM25 to defer.
    # `agent.answer` plus 1–2 frequently used tools should stay
    # non-deferred — Anthropic returns 400 if every tool defers.
    defer_loading: bool = True

    def to_mcp(self) -> Dict[str, Any]:
        """Strip dispatch metadata; return the wire-format MCP entry."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


# ----- Common schema fragments used across multiple tools ------------------

_LABELED_VALUE_ITEM = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "description": (
                "One of 'home', 'work', 'mobile', 'iPhone', 'main', 'other'. "
                "Case-insensitive."),
        },
        "value": {"type": "string"},
    },
    "required": ["label", "value"],
    "additionalProperties": False,
}

_POSTAL_ADDRESS_ITEM = {
    "type": "object",
    "properties": {
        "label": {"type": "string",
                  "description": "One of 'home', 'work', 'school', 'other'."},
        "street": {"type": "string"},
        "city": {"type": "string"},
        "state": {"type": "string"},
        "postal_code": {"type": "string"},
        "country": {"type": "string"},
    },
    "required": ["label", "street", "city", "state",
                  "postal_code", "country"],
    "additionalProperties": False,
}

_RECURRENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "frequency": {
            "type": "string",
            "enum": ["daily", "weekly", "monthly", "yearly"],
        },
        "interval": {
            "type": "integer", "minimum": 1, "default": 1,
            "description": (
                "1 = every period, 2 = every other period, etc."),
        },
        "end_iso": {
            "type": "string",
            "description": (
                "ISO date 'YYYY-MM-DD' (date-only treated as end-of-day) "
                "or 'YYYY-MM-DDTHH:MM:SS'. Mutually exclusive with "
                "end_count."),
        },
        "end_count": {
            "type": "integer", "minimum": 1,
            "description": "Number of occurrences. Mutually exclusive with end_iso.",
        },
    },
    "required": ["frequency"],
    "additionalProperties": False,
}


# ----- The 11 tool definitions ---------------------------------------------

# Note for the BM25 retriever: tool `description` is the indexed text. We
# write keyword-rich descriptions naming the framework (`EventKit`,
# `Contacts`, `MapKit`), the operation verb (`create`, `list`, `update`,
# `lookup`), and the resource kind (`event`, `reminder`, `calendar`,
# `contact`, `address`, `place`). Tools the agent should use frequently
# (and that BM25 may not surface from short instructions) are kept
# non-deferred.

TOOLS: List[APITool] = [
    # ---- EventKit / Reminders -----------------------------------------------
    APITool(
        name="eventkit.create_list",
        description=(
            "Create a new Reminders list (an EKCalendar of type .reminder) "
            "via EventKit. Use when the user asks to add a new Reminders "
            "list or category. The list is created in the default reminder "
            "source on the device."),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "List title."},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        command_type="create_list",
        defer_loading=True,  # less common; behind agent.search_tools
    ),
    APITool(
        name="eventkit.create_reminder",
        description=(
            "Create a reminder in an existing list via EventKit. Use for "
            "any task that adds a TODO, sets a due date, attaches notes, "
            "or sets a recurrence rule on a reminder. The reminder is "
            "saved with EKEventStore.save(_:commit:true)."),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "list": {"type": "string",
                         "description": "Name of the list to add to."},
                "priority": {
                    "type": "string",
                    "enum": ["high", "medium", "low", "none"],
                    "description": "Reminder priority. Defaults to none.",
                },
                "completed": {"type": "boolean", "default": False},
                "due_iso": {
                    "type": "string",
                    "description": (
                        "ISO 'YYYY-MM-DD' for date-only or "
                        "'YYYY-MM-DDTHH:MM:SS' for time-of-day."),
                },
                "notes": {"type": "string"},
                "url": {"type": "string"},
                "recurrence": _RECURRENCE_SCHEMA,
            },
            "required": ["title", "list"],
            "additionalProperties": False,
        },
        command_type="create_reminder",
        defer_loading=True,
    ),
    APITool(
        name="eventkit.list_reminders",
        description=(
            "List reminders via EventKit. Optionally filter by list name "
            "and whether to include completed items. Use for read-only "
            "lookup tasks (list due today, lookup notes by title) and as "
            "the FIRST half of an update via the list+wipe+create "
            "workaround for fields EventKit can change but our Swift "
            "wrapper has no direct update for. Returns dueDate, notes, "
            "priority, recurrence, identifier per row."),
        input_schema={
            "type": "object",
            "properties": {
                "list": {"type": "string",
                         "description": "Case-insensitive list-name filter."},
                "include_completed": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
        command_type="list_reminders",
        defer_loading=True,
    ),
    APITool(
        name="eventkit.create_calendar",
        description=(
            "Create a new Calendar (EKCalendar of type .event) via "
            "EventKit. Use when the user asks for a new calendar. "
            "Distinct from create_list (which creates Reminders lists)."),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "color": {
                    "type": "string",
                    "description": "Optional hex color like '#FF6600'.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        command_type="create_calendar",
        defer_loading=True,
    ),
    APITool(
        name="eventkit.list_calendars",
        description=(
            "List writable Calendars via EventKit. Use to discover which "
            "Calendar to write into before create_event when the user "
            "names a calendar by title."),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        command_type="list_calendars",
        defer_loading=True,
    ),
    APITool(
        name="eventkit.create_event",
        description=(
            "Create a Calendar event via EventKit. Required: title, "
            "start_iso, end_iso. Optional: calendar name, all_day, "
            "location, notes, url, recurrence. Use for any task that "
            "puts a meeting/appointment on the calendar."),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start_iso": {
                    "type": "string",
                    "description": "ISO 'YYYY-MM-DDTHH:MM:SS' local time.",
                },
                "end_iso": {"type": "string"},
                "calendar": {"type": "string"},
                "all_day": {"type": "boolean", "default": False},
                "location": {"type": "string"},
                "notes": {"type": "string"},
                "url": {"type": "string"},
                "recurrence": _RECURRENCE_SCHEMA,
            },
            "required": ["title", "start_iso", "end_iso"],
            "additionalProperties": False,
        },
        command_type="create_event",
        defer_loading=True,
    ),
    APITool(
        name="eventkit.list_events",
        description=(
            "List Calendar events via EventKit. Optionally filter by "
            "calendar name and ISO start/end window (defaults to ±1 year "
            "around now). Returns one row per recurring-event master "
            "(not per expanded occurrence). Use for read-only event "
            "lookups (today's events, conflict detection, next event) "
            "and as the FIRST half of an event update via the "
            "list+wipe+create workaround."),
        input_schema={
            "type": "object",
            "properties": {
                "calendar": {"type": "string"},
                "start_iso": {"type": "string"},
                "end_iso": {"type": "string"},
                "writable_only": {"type": "boolean", "default": True},
                "master_only": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        command_type="list_events",
        defer_loading=True,
    ),

    # ---- Contacts ----------------------------------------------------------
    APITool(
        name="cn.create_contact",
        description=(
            "Create a new Contact via Contacts.framework "
            "(CNMutableContact + CNSaveRequest). Required: given_name OR "
            "family_name. Optional simple fields (organization, job_title, "
            "department, birthday). Optional labeled-multi-value arrays "
            "(phones, emails, postal_addresses, urls, dates). Use for any "
            "task that adds a new contact to the address book."),
        input_schema={
            "type": "object",
            "properties": {
                "given_name": {"type": "string"},
                "family_name": {"type": "string"},
                "middle_name": {"type": "string"},
                "nickname": {"type": "string"},
                "organization": {"type": "string"},
                "job_title": {"type": "string"},
                "department": {"type": "string"},
                "birthday": {
                    "type": "string",
                    "description": (
                        "'YYYY-MM-DD' with year, or '--MM-DD' for "
                        "year-unknown (iOS Contacts.app default)."),
                },
                "phones": {"type": "array", "items": _LABELED_VALUE_ITEM},
                "emails": {"type": "array", "items": _LABELED_VALUE_ITEM},
                "postal_addresses": {"type": "array",
                                      "items": _POSTAL_ADDRESS_ITEM},
                "urls": {"type": "array", "items": _LABELED_VALUE_ITEM},
            },
            "additionalProperties": False,
        },
        command_type="create_contact",
        defer_loading=True,
    ),
    APITool(
        name="cn.list_contacts",
        description=(
            "List Contacts via Contacts.framework "
            "(CNContactStore.unifiedContacts). Optionally filter by name. "
            "Returns full schema (phones, emails, postal_addresses, "
            "birthday, organization, identifier). Use to look up phone "
            "numbers/addresses for an existing contact, and as the FIRST "
            "step of any cn.update_contact (which requires the target's "
            "identifier)."),
        input_schema={
            "type": "object",
            "properties": {
                "name_filter": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring match against given, "
                        "family, or full name."),
                },
            },
            "additionalProperties": False,
        },
        command_type="list_contacts",
        defer_loading=True,
    ),
    APITool(
        name="cn.update_contact",
        description=(
            "Update an existing Contact via Contacts.framework. Required: "
            "identifier (from cn.list_contacts). Any field present "
            "REPLACES that field. Multi-value arrays REPLACE the entire "
            "array — to append a phone, list_contacts first and pass the "
            "combined list. Use for any task that edits a contact: set "
            "birthday, add a phone label, change an address."),
        input_schema={
            "type": "object",
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "From cn.list_contacts row.identifier.",
                },
                "given_name": {"type": "string"},
                "family_name": {"type": "string"},
                "middle_name": {"type": "string"},
                "nickname": {"type": "string"},
                "organization": {"type": "string"},
                "job_title": {"type": "string"},
                "department": {"type": "string"},
                "birthday": {"type": "string"},
                "phones": {"type": "array", "items": _LABELED_VALUE_ITEM},
                "emails": {"type": "array", "items": _LABELED_VALUE_ITEM},
                "postal_addresses": {"type": "array",
                                      "items": _POSTAL_ADDRESS_ITEM},
                "urls": {"type": "array", "items": _LABELED_VALUE_ITEM},
            },
            "required": ["identifier"],
            "additionalProperties": False,
        },
        command_type="update_contact",
        defer_loading=True,
    ),

    # ---- MapKit ------------------------------------------------------------
    # ---- System info (sim-side) --------------------------------------------
    APITool(
        name="system.now",
        description=(
            "Get the current date and time from the iOS device. Returns "
            "today's date (YYYY-MM-DD), the current weekday (Monday, "
            "Tuesday, ...), the local datetime, the timezone, and a "
            "GMT-offset. Use whenever you need to know what day it is, "
            "what time it is, today's date, tomorrow's date, or the "
            "current weekday — for example to set a due date relative "
            "to today, to compute 'tomorrow' or 'next Monday', or to "
            "fill a 'starts at' time field. Backed by Foundation's "
            "Date()/Calendar.current on the device."),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        command_type="system_now",
        defer_loading=True,
    ),
    APITool(
        name="system.locale",
        description=(
            "Get the iOS device's locale settings: language code "
            "(en, ja, …), region code (US, JP, …), currency, the "
            "first day of the week (1=Sunday, 2=Monday, …), and the "
            "measurement system (us, uk, metric). Use when a task "
            "depends on locale-specific conventions — e.g. when a "
            "weekly recurrence depends on first-day-of-week, when a "
            "postal address format varies by region, or when "
            "interpreting a 12-hour vs 24-hour time. Backed by "
            "Foundation's Locale.current on the device."),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        command_type="system_locale",
        defer_loading=True,
    ),

    APITool(
        name="mklocalsearch.query",
        description=(
            "Resolve a place name or street-address query to a coordinate "
            "via MapKit's MKLocalSearch — the same backend Maps.app's "
            "search box uses. Returns {lat, lon, name, formatted_address} "
            "for the top match. Use when a task needs the address of a "
            "named place (e.g. 'Look up Apple Park' or 'Where is the Salk "
            "Institute')."),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Free-text place or address."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        command_type="geocode_query",  # legacy Swift handler name
        defer_loading=True,
    ),

    # ---- Python-only: model-driven tool discovery --------------------------
    APITool(
        name="agent.search_tools",
        description=(
            "Search the iOS API tool catalog for tools relevant to a "
            "query. Returns matching tool definitions (name, "
            "description, inputSchema) that BECOME CALLABLE on "
            "subsequent turns. Use when you need a capability you don't "
            "currently see in your catalog — e.g. listing Calendars by "
            "name, creating a Calendar (distinct from a Reminders list), "
            "looking up an address by place name. Pass a query in "
            "natural language (e.g. 'find a calendar by name', 'resolve "
            "an address'); the search ranks tools by BM25 over their "
            "name+description text. Top-K defaults to 5."),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural-language description of the capability "
                        "you're looking for."),
                },
                "k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "default": 5,
                    "description": "Number of matches to return.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        command_type=None,  # handled specially by the dispatcher
        defer_loading=False,  # always present in the initial catalog
    ),

    # ---- Python-only: terminal answer --------------------------------------
    APITool(
        name="agent.answer",
        description=(
            "Submit the final answer for a read-only / lookup task. "
            "The payload is whatever shape the task expects (a number, a "
            "string, a list, a small JSON object). Once invoked the "
            "episode ends — emit exactly once, on the final turn."),
        input_schema={
            "type": "object",
            "properties": {
                "answer": {
                    "description": (
                        "Free-form payload. For 'how many' tasks, an "
                        "integer. For 'what is X' tasks, a string. For "
                        "structured tasks, a JSON object."),
                },
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
        command_type=None,
        is_terminal=True,
        # Never defer — agent must always be able to reach it.
        defer_loading=False,
    ),

    # ---- Python-only: terminal fail --------------------------------------
    # Mirrors the UI scaffold's `FAIL "reason"` text verb. Primarily
    # used by the hybrid baseline (sibb/hybrid_baseline/) where the
    # agent might emit either `FAIL "reason"` (text) or
    # agent.fail(reason=...) (structured) — both are normalized to the
    # same JSONL event-kind at dispatcher level.
    APITool(
        name="agent.fail",
        description=(
            "Submit FAIL when the task cannot be completed with the "
            "tools available. Provide a brief one-sentence reason "
            "(no API for inbound iMessage; no public app for X; …). "
            "Use sparingly — only after agent.search_tools confirms "
            "nothing in the catalog covers the gap. Once invoked the "
            "episode ends — emit exactly once."),
        input_schema={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence explaining what's blocking "
                        "completion. The verifier records this and "
                        "scores the task as a failure."),
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        command_type=None,
        is_terminal=True,
        # Never defer — agent must always be able to reach it.
        defer_loading=False,
    ),
]


# Convenience lookups (cheap; constructed once at import).
TOOLS_BY_NAME: Dict[str, APITool] = {t.name: t for t in TOOLS}


# Tool → bundle attribution for the verifier's `context["observed_bundles"]`
# gate. The UI baseline populates `observed_bundles` from AX-tree reads
# (sibb_assistant.py uses `tree.bundle_id` per turn); the API agent has
# no AX reads, so it must attribute each successful tool call to the
# bundle whose system store the tool's Apple SDK touched. The verifier's
# `_check_agent_answer` (sibb_verify.py:2459-2479) requires
# `observed_bundles` to contain the expected resource bundle, or every
# read-task answer fails as `failure_kind=no_evidence`.
TOOL_TO_BUNDLE: Dict[str, str] = {
    "eventkit.create_event": "com.apple.mobilecal",
    "eventkit.list_events": "com.apple.mobilecal",
    "eventkit.create_calendar": "com.apple.mobilecal",
    "eventkit.list_calendars": "com.apple.mobilecal",
    "eventkit.create_reminder": "com.apple.reminders",
    "eventkit.list_reminders": "com.apple.reminders",
    "eventkit.create_list": "com.apple.reminders",
    "cn.create_contact": "com.apple.MobileAddressBook",
    "cn.list_contacts": "com.apple.MobileAddressBook",
    "cn.update_contact": "com.apple.MobileAddressBook",
    "mklocalsearch.query": "com.apple.Maps",
    # system.now / system.locale read Foundation primitives that ride on
    # the runner's own host process (SpringBoard/Foundation); use the
    # SpringBoard bundle so any verifier observation_required including
    # the system is satisfied.
    "system.now": "com.apple.springboard",
    "system.locale": "com.apple.springboard",
    # agent.answer and agent.search_tools touch no system store; not
    # in the map.
}


# ---------------------------------------------------------------------------
# list+wipe+create workaround helpers
# ---------------------------------------------------------------------------


# Public-field schemas to carry across a wipe+create. These are the fields
# the verifier inspects (per `operational_definition.md` §3.bis); copying
# anything narrower silently fails baseline-preservation checks.
_REMINDER_CARRY_FIELDS: Tuple[str, ...] = (
    "due", "notes", "url", "priority", "recurrence", "completed",
)

# Mapping from `list_reminders` output field name → `create_reminder` input
# field name. Most are identical; `due` → `due_iso` is the one rename.
_REMINDER_FIELD_RENAMES: Dict[str, str] = {
    "due": "due_iso",
}


def _carry_reminder_fields(listed_row: Dict[str, Any]
                            ) -> Dict[str, Any]:
    """Return the set of create_reminder kwargs needed to preserve every
    public field on the listed row. The caller layers task-specific
    mutations on top of this."""
    carried: Dict[str, Any] = {}
    for src_field in _REMINDER_CARRY_FIELDS:
        if src_field not in listed_row:
            continue
        dst_field = _REMINDER_FIELD_RENAMES.get(src_field, src_field)
        carried[dst_field] = copy.deepcopy(listed_row[src_field])
    return carried


def _maybe_parse_json_answer(answer: Any) -> Any:
    """If `answer` is a string that looks like a JSON object or array,
    parse it. Otherwise return it unchanged.

    Background: agent.answer's input_schema declares `answer: {}`
    (any type), so the provider's strict-mode parser doesn't coerce
    structured answers. Empirically Gemini sometimes stringifies dict
    answers via JSON.stringify-style encoding before passing them,
    producing payloads like `'{"items": [{"title": "x"}]}'` (a string)
    instead of `{"items": [{"title": "x"}]}` (a dict).

    The downstream verifier's `_walk_path` traverses the answer with
    JSONPath-style segments (e.g. `$.items[0].title`). On a string-
    typed answer the walk fails with `path_miss` and the check fails
    even though the agent's content was substantively correct.

    Heuristic: if `answer` is a string whose first non-whitespace char
    is `{` or `[`, attempt `json.loads`. On any failure, return the
    original string — we never corrupt the agent's intent.
    """
    if not isinstance(answer, str):
        return answer
    stripped = answer.strip()
    if not stripped:
        return answer
    if stripped[0] not in "{[":
        return answer
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return answer


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ToolCallResult:
    """Structured result of one dispatcher invocation.

    `ok` is the success flag the LLM sees as `is_error: False` (or True).
    `payload` is the JSON-serializable body sent back as the tool_result
    content. `terminal` signals the agent loop to stop. `synthetic_update`
    flags a wipe+create workaround in the trajectory log so the κ pipeline
    can audit those separately per §3.bis. `latency_ms` is wall-clock for
    cost analysis."""
    ok: bool
    payload: Any
    terminal: bool = False
    synthetic_update: bool = False
    latency_ms: int = 0


class APIToolDispatcher:
    """Routes one MCP tool call to the right backend.

    The dispatcher holds:
      - the connected XCUITestReader (for Swift handlers),
      - a reference to the agent-answer slot that the runner reads after
        the loop ends,
      - the set of tools the model has DISCOVERED via agent.search_tools
        this episode. Discovered tools persist across turns — once
        retrieved, they remain callable for the rest of the episode.

    Tool dispatch routing:
      - `agent.answer`         → captures payload, terminal=True, no socket.
      - `agent.search_tools`   → runs BM25 over deferred tools, returns
                                 matching MCP defs, marks them discovered.
      - everything else        → serialized to a Swift command dict
                                 `{"type": "<command_type>", **args}` and
                                 awaits `reader._send(cmd)`.
    """

    def __init__(self, reader: Any) -> None:
        self.reader = reader
        self.answer_payload: Optional[Dict[str, Any]] = None
        # Tools the model has discovered via agent.search_tools this
        # episode. Always-loaded tools (defer_loading=False) are NOT in
        # this set — the loop sources its initial catalog from
        # `non_deferred_tool_names()` and merges in `discovered_tools`
        # before each chat() call.
        self.discovered_tools: List[str] = []
        # Lazy-initialized BM25 index over the deferred subset.
        self._search_index: Optional[BM25ToolIndex] = None

    # ----- Public surface --------------------------------------------------

    def current_catalog(self) -> List[str]:
        """Return the tool names currently exposed to the model.

        That's the union of:
          - always-loaded tools (defer_loading=False) — including
            agent.answer and agent.search_tools.
          - tools discovered via agent.search_tools this episode.
        Order is stable: always-loaded first, then discovered in the
        order they were retrieved."""
        out = list(non_deferred_tool_names())
        for name in self.discovered_tools:
            if name not in out:
                out.append(name)
        return out

    def _ensure_search_index(self) -> "BM25ToolIndex":
        """Lazy-build the BM25 index over the DEFERRED tools only.

        agent.search_tools is the entry point to the deferred set; it
        wouldn't make sense to retrieve always-loaded tools (the model
        already has them). Built once per dispatcher (once per
        episode). Pass `reserve_names=[]` so the result is a pure
        ranking — not polluted by the non-deferred reserve."""
        if self._search_index is None:
            deferred_tools = [t for t in TOOLS if t.defer_loading]
            self._search_index = BM25ToolIndex(
                deferred_tools, reserve_names=[])
        return self._search_index

    async def dispatch(self, name: str, args: Dict[str, Any]
                        ) -> ToolCallResult:
        """Execute one tool call by name. Args are an already-validated
        dict matching the tool's `input_schema` — schema validation
        happens upstream (per-provider FC with `strict: true` for
        Anthropic/OpenAI; manual validate for Gemini)."""
        t0 = time.monotonic()
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return ToolCallResult(
                ok=False,
                payload={"error": f"unknown tool: {name}"},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # ---- agent.answer (Python only) ---------------------------------
        if tool.is_terminal:
            answer = args.get("answer")
            # Gemini (and occasionally other providers) sometimes wraps
            # structured answers as a JSON-encoded string instead of
            # passing a dict. The verifier's _walk_path can't traverse a
            # string typed as `{"items": [...]}` — it errors with
            # path_miss. Detect strings that look like JSON objects or
            # arrays and parse them so the verifier sees the structured
            # shape it expects. Bare scalars / free-form text are left
            # alone.
            answer = _maybe_parse_json_answer(answer)
            self.answer_payload = {"answer": answer}
            return ToolCallResult(
                ok=True,
                payload={"received": True},
                terminal=True,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # ---- agent.search_tools (Python only) ---------------------------
        if name == "agent.search_tools":
            query = args.get("query", "")
            k = int(args.get("k", 5))
            idx = self._ensure_search_index()
            # The search index is built only over deferred tools (the
            # ones the model can't already see). top_k returns the
            # reserve UNION top-K; but the deferred index's reserve is
            # empty (we passed only deferred tools to BM25ToolIndex),
            # so this is a pure ranked list of deferred matches.
            ranked = idx.top_k(query, k=k)
            # Mark them discovered so they appear in the next turn's
            # catalog. Duplicates are no-ops.
            for nm in ranked:
                if nm not in self.discovered_tools:
                    self.discovered_tools.append(nm)
            # Return the FULL MCP definition for each match so the model
            # can reason about parameters in the same turn.
            matches = [TOOLS_BY_NAME[nm].to_mcp()
                        for nm in ranked
                        if nm in TOOLS_BY_NAME]
            return ToolCallResult(
                ok=True,
                payload={
                    "matches": matches,
                    "now_available": ranked,
                    "note": (
                        "The matched tools are now callable on this and "
                        "subsequent turns. Use them like any other tool."),
                },
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # ---- Swift socket calls -----------------------------------------
        # All non-terminal tools have a command_type by construction.
        assert tool.command_type is not None, (
            f"tool {name} has no command_type and is not terminal — "
            "fix the TOOLS table")
        # Defensive: a model could emit `type` inside its args dict
        # (the field isn't in any schema, but Gemini doesn't enforce
        # additionalProperties:false). dict.update() would let the
        # agent's `type` override our command_type and reach a
        # different Swift handler than intended (e.g. routing
        # create_list → wipe_reminders). Build cmd as {**args, type}
        # so our command_type is always authoritative.
        cmd = {**args, "type": tool.command_type}
        try:
            resp = await self.reader._send(cmd)
        except Exception as exc:  # noqa: BLE001 — surface any socket error
            return ToolCallResult(
                ok=False,
                payload={"error": f"socket error: "
                                    f"{type(exc).__name__}: {exc}"},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        ok = bool(resp.get("ok", False))
        return ToolCallResult(
            ok=ok,
            payload=resp,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )

    async def synthetic_update_reminder(
        self,
        *,
        list_name: str,
        match_title: str,
        mutations: Dict[str, Any],
    ) -> ToolCallResult:
        """Implements the list+wipe+create workaround for fields the Swift
        surface has no direct update for (recurrence, priority, notes
        edits).

        Steps:
          1. list_reminders(list=list_name) → find the target by title
          2. delete it via wipe (we use create_reminder with completed=true
             as a workaround — TODO: the wipe handler is per-list, not
             per-item; for v1 we use the dedicated remove pathway when
             one lands. This stub raises until a per-item delete handler
             is wired through the Swift side.)
          3. create_reminder with carried fields ⊕ mutations

        Until step 2 is wired through a per-item Swift delete, this
        helper raises so the agent loop is forced to surface the gap
        in the trajectory rather than silently passing a half-baked
        update. The L1 safety test
        (sibb/tests/unit/api_baseline/test_synthetic_update_safety.py)
        pins this behavior.
        """
        # We intentionally do NOT silently mutate state on the device when
        # the workaround is incomplete. Surface the gap explicitly.
        raise NotImplementedError(
            "synthetic_update_reminder requires a per-item delete handler "
            "on the Swift side; currently we have only wipe_reminders "
            "(per-list). Adding eventkit.delete_reminder is the next "
            "engineering step before any synthetic-update task can run "
            "in v1. See operational_definition.md §3.bis.")


# ---------------------------------------------------------------------------
# Provider-translation helpers (thin shims; the heavy lifting goes in
# sibb_llm.py in a separate pass)
# ---------------------------------------------------------------------------


def mcp_tools() -> List[Dict[str, Any]]:
    """Canonical MCP-shape tool list for downstream translators.

    The per-provider wire-format translator in `sibb_llm.py` takes this
    list and emits the Anthropic / OpenAI / Gemini wire format. Keeping
    the canonical list here means the TOOLS table is the single source
    of truth across providers.
    """
    return [t.to_mcp() for t in TOOLS]


def deferred_tool_names() -> List[str]:
    """Tool names that should set `defer_loading: true` under Anthropic
    Tool Search BM25. Non-deferred names (agent.answer + the most-used
    tools) stay always-loaded so Anthropic never sees a fully-deferred
    catalog (which 400s)."""
    return [t.name for t in TOOLS if t.defer_loading]


def non_deferred_tool_names() -> List[str]:
    """The always-loaded reserve. By construction this is non-empty,
    keeping the Anthropic 400 at bay."""
    names = [t.name for t in TOOLS if not t.defer_loading]
    assert names, (
        "non-deferred reserve is empty; Anthropic Tool Search would 400 "
        "on a fully-deferred catalog. Mark at least agent.answer + 1 "
        "frequent tool as defer_loading=False in TOOLS.")
    return names


# ---------------------------------------------------------------------------
# Client-side BM25 tool retrieval
# ---------------------------------------------------------------------------
#
# The locked decision (consolidated memo Part 1.4 + the Tool Search memo,
# reaffirmed by the user 2026-06-10) is to ship retrieval-based tool
# selection at v1 for ecological validity. Anthropic's Tool Search BM25
# only works on their stack; for cross-provider uniformity we ship a
# small client-side BM25 over the (name + description) text of each tool.
# At n=11 the retrieval shape is what matters, not the selection
# accuracy — but plumbing it from v1 means scaling to 100+ App Intents
# (v2 production deployment) is one configuration change away.


import math
import re
from collections import Counter


_BM25_K1 = 1.5
_BM25_B = 0.75

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _tokenize_for_bm25(text: str) -> List[str]:
    """Cheap tokenizer: case-fold and split on non-identifier chars.
    Splits camelCase / snake_case identifiers into pieces so a query
    like 'reminder' matches a tool named 'eventkit.create_reminder'."""
    out: List[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        # Also split tool-namespace segments (eventkit.create_reminder
        # → ['eventkit', 'create', 'reminder']) via underscore-split.
        for piece in tok.split("_"):
            if piece:
                out.append(piece)
    return out


@dataclasses.dataclass
class _BM25Doc:
    name: str
    tokens: List[str]
    tf: Dict[str, int]
    length: int


class BM25ToolIndex:
    """Per-episode BM25 retrieval over the tool catalog.

    The index is built once at startup over (name + description) of
    each tool. `top_k(query, k)` returns the top-K tool names ranked
    by BM25 score against the query. Tools marked `defer_loading=False`
    (the non-deferred reserve, which includes `agent.answer` plus the
    most-used tools) are ALWAYS included regardless of score, so the
    agent can always reach them.

    At v1's 11 tools the selection accuracy is effectively perfect for
    any reasonable query; we ship retrieval for the *shape*, not the
    *signal*. At v2 with hundreds of App Intents, the same code path
    becomes load-bearing.
    """

    def __init__(self, tools: Optional[List[APITool]] = None,
                  reserve_names: Optional[List[str]] = None):
        if tools is None:
            tools = TOOLS
        self._docs: List[_BM25Doc] = []
        for t in tools:
            text = f"{t.name} {t.description}"
            tokens = _tokenize_for_bm25(text)
            tf = dict(Counter(tokens))
            self._docs.append(_BM25Doc(
                name=t.name, tokens=tokens, tf=tf, length=len(tokens)))
        self._avg_doc_len = (
            sum(d.length for d in self._docs) / max(1, len(self._docs)))
        # Document frequency for IDF.
        self._df: Dict[str, int] = Counter()
        for d in self._docs:
            for term in set(d.tokens):
                self._df[term] += 1
        self._N = len(self._docs)
        # The "always include" reserve. Default is the project-wide
        # non-deferred set (for callers indexing the full catalog).
        # The model-driven search dispatcher passes an empty list so
        # the result is pure BM25 ranking over the deferred subset.
        if reserve_names is None:
            reserve_names = non_deferred_tool_names()
        self._reserve = set(reserve_names)

    def _bm25_score(self, doc: _BM25Doc, query_tokens: List[str]) -> float:
        score = 0.0
        for q in query_tokens:
            df = self._df.get(q, 0)
            if df == 0:
                continue
            # Robertson-Sparck-Jones IDF with floor at 0 to avoid
            # negative scores for very common terms.
            idf = max(0.0, math.log(
                (self._N - df + 0.5) / (df + 0.5) + 1.0))
            tf = doc.tf.get(q, 0)
            if tf == 0:
                continue
            denom = tf + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * doc.length / max(1.0, self._avg_doc_len))
            score += idf * (tf * (_BM25_K1 + 1)) / denom
        return score

    def top_k(self, query: str, k: int = 5) -> List[str]:
        """Return the top-K tool names by BM25 score, UNION the
        non-deferred reserve (which is always included).

        At v1's 11-tool catalog with k=5, the result is typically
        7-9 tools. Order: reserve first, then BM25-ranked retrievals
        not already in reserve."""
        query_tokens = _tokenize_for_bm25(query)
        scored: List[Tuple[float, str]] = []
        for doc in self._docs:
            scored.append((self._bm25_score(doc, query_tokens), doc.name))
        scored.sort(key=lambda x: x[0], reverse=True)
        retrieved: List[str] = []
        for _, name in scored[:k]:
            if name not in self._reserve and name not in retrieved:
                retrieved.append(name)
        # Reserve first, then top-K retrievals.
        out = list(self._reserve)
        out.extend(retrieved)
        return out
