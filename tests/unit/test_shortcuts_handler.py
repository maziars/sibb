"""ShortcutsHandler — L1 tests.

v1 ships:
  - `apply(type="run", name, input)` — invokes a Library shortcut by
    name via `simctl openurl shortcuts://run-shortcut?...`
  - `shortcuts.installed` resource fetcher — AX read of the Library
    tab cell labels, parsed by `_parse_shortcuts_library_tree`

v1 does NOT ship (Apple-side constraints, captured in
TODO_DEFERRED §G1):
  - create / edit / delete shortcuts (no public API)
  - run-by-name for trigger-based Automations (URL scheme is
    Library-only)

These tests cover the URL-building logic, the openurl subprocess
shape, dispatch routing in `apply()`, and the AX-tree parser. The
AX-fetcher integration is covered indirectly via the parser;
end-to-end with a live sim is L2 territory.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse

import pytest

import sibb_state
from sibb_spec import RunShortcut, SPEC_TYPES, validate_entry
from sibb_state import (
    HANDLERS,
    ShortcutsHandler,
    _build_run_shortcut_url,
    _parse_shortcuts_library_tree,
    canonicalize_app,
    collect_tcc_services,
)

pytestmark = pytest.mark.fast


# ─────────────────────────── handler-protocol lints ──────────────────


def test_shortcuts_handler_registered_by_bundle_id():
    assert ShortcutsHandler.bundle_id == "com.apple.shortcuts"
    assert HANDLERS[ShortcutsHandler.bundle_id] is ShortcutsHandler


def test_shortcuts_handler_no_tcc_services():
    """No TCC grant needed — `simctl openurl` doesn't gate on any
    privacy service. Lints against accidental over-granting."""
    assert ShortcutsHandler.tcc_services == []


def test_shortcuts_handler_is_not_a_pre_runner():
    assert ShortcutsHandler.pre_runner is False
    assert ShortcutsHandler.pre_runner_kinds == []


def test_shortcuts_handler_no_depends_on():
    """No formal handler ordering even though shortcuts touch other
    apps (Reminders/Files/etc.). The side effects are produced by
    Shortcuts' own action engine — handler dispatch order doesn't
    matter."""
    assert ShortcutsHandler.depends_on == []


def test_canonicalize_shortcuts_friendly_name():
    assert canonicalize_app("Shortcuts") == "com.apple.shortcuts"
    assert canonicalize_app("shortcuts") == "com.apple.shortcuts"
    assert canonicalize_app("SHORTCUTS") == "com.apple.shortcuts"


def test_shortcuts_does_not_contribute_to_collect_tcc_services():
    """Shouldn't add anything to the runner's TCC grants — Shortcuts
    is a URL-scheme target, not a permission-gated API."""
    services = collect_tcc_services()
    # Just verify Shortcuts didn't inject anything unexpected. The
    # health-share/update/etc from other handlers are fine.
    assert all("shortcut" not in s.lower() for s in services)


# ─────────────────────── RunShortcut spec ───────────────────────────


def test_run_shortcut_spec_registered():
    assert ("Shortcuts", "run") in SPEC_TYPES
    assert SPEC_TYPES[("Shortcuts", "run")] is RunShortcut


def test_run_shortcut_to_dict_canonical_shape():
    spec = RunShortcut(name="Quick Reminder", input="Buy milk")
    d = spec.to_dict()
    assert d == {
        "app": "Shortcuts", "type": "run",
        "name": "Quick Reminder", "input": "Buy milk",
    }


def test_run_shortcut_round_trip():
    original = RunShortcut(name="Make List", input={"title": "X", "list": "Y"})
    back = RunShortcut.from_dict(original.to_dict())
    assert back == original


def test_run_shortcut_default_input_is_none():
    spec = RunShortcut(name="No Input Shortcut")
    assert spec.input is None
    assert spec.to_dict()["input"] is None


def test_validate_entry_accepts_run_shortcut():
    typed, err = validate_entry({
        "app": "Shortcuts", "type": "run",
        "name": "Quick Reminder", "input": "Buy milk",
    })
    assert err is None
    assert isinstance(typed, RunShortcut)


# ─────────────────────── _build_run_shortcut_url ─────────────────────


def test_build_url_simple_no_input():
    url = _build_run_shortcut_url("Quick Reminder")
    assert url == "shortcuts://run-shortcut?name=Quick+Reminder"


def test_build_url_str_input_added_as_text_param():
    url = _build_run_shortcut_url("Quick Reminder", "Buy milk")
    assert url.startswith("shortcuts://run-shortcut?name=Quick+Reminder")
    assert "&input=text&text=Buy+milk" in url


def test_build_url_dict_input_serialized_as_sorted_json():
    """Dict inputs are JSON-encoded with sorted keys so the same
    payload always produces the same URL (stable for caching,
    diffing, snapshot tests)."""
    url = _build_run_shortcut_url(
        "Make List", {"title": "X", "list": "Y"})
    qs = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(qs)
    assert params["name"] == ["Make List"]
    assert params["input"] == ["text"]
    # Parsing the JSON back gives the same dict.
    payload = json.loads(params["text"][0])
    assert payload == {"title": "X", "list": "Y"}
    # Sorted-keys check: serialize a permuted dict, expect same JSON.
    url2 = _build_run_shortcut_url(
        "Make List", {"list": "Y", "title": "X"})
    assert url == url2


def test_build_url_url_encodes_special_chars_in_name():
    """Names with `&`, `=`, `?`, etc. must round-trip cleanly so
    iOS sees the literal name, not a parsed query string."""
    url = _build_run_shortcut_url("Q&A: Test")
    qs = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(qs)
    assert params["name"] == ["Q&A: Test"]


def test_build_url_url_encodes_special_chars_in_text():
    url = _build_run_shortcut_url(
        "Echo", "hello & goodbye=now")
    qs = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(qs)
    assert params["text"] == ["hello & goodbye=now"]


def test_build_url_unicode_input_round_trips():
    url = _build_run_shortcut_url("Test", "café — naïve résumé")
    qs = urllib.parse.urlparse(url).query
    params = urllib.parse.parse_qs(qs)
    assert params["text"] == ["café — naïve résumé"]


def test_build_url_rejects_empty_name():
    with pytest.raises(ValueError, match="non-empty"):
        _build_run_shortcut_url("")


def test_build_url_rejects_non_string_name():
    with pytest.raises(ValueError, match="non-empty"):
        _build_run_shortcut_url(42)


def test_build_url_rejects_non_str_non_dict_input():
    """Lists / numbers / arbitrary objects aren't valid URL-scheme
    inputs. The handler API only accepts str | dict | None."""
    with pytest.raises(ValueError, match="must be str, dict, or None"):
        _build_run_shortcut_url("Test", ["a", "b"])
    with pytest.raises(ValueError, match="must be str, dict, or None"):
        _build_run_shortcut_url("Test", 42)


# ─────────────────────── apply() validation ──────────────────────────


class _UdidStub:
    def __init__(self, udid: str = "FAKE-UDID"):
        self.udid = udid


async def test_apply_unknown_kind_raises():
    h = ShortcutsHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="unknown entry type"):
        await h.apply({"type": "tap_dance"})


@pytest.mark.parametrize("kind", ["create", "edit", "delete"])
async def test_apply_create_edit_delete_raise_with_pointer_to_g1(kind):
    """The error message must mention the Apple-API limitation +
    TODO_DEFERRED §G1 so a future engineer doesn't think this is
    just unimplemented."""
    h = ShortcutsHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match=r"TODO_DEFERRED.*G1"):
        await h.apply({"type": kind})


async def test_apply_run_automation_raises_with_ui_workaround():
    """Automations can't be name-addressed via URL scheme — error
    message must point at the UI workaround ('Run Immediately')."""
    h = ShortcutsHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="Run Immediately"):
        await h.apply({"type": "run_automation", "name": "X"})


async def test_apply_run_requires_reader_with_udid():
    h = ShortcutsHandler(reader=None)
    with pytest.raises(RuntimeError, match=".udid"):
        await h.apply({"type": "run", "name": "Quick Reminder"})


async def test_apply_run_requires_name():
    h = ShortcutsHandler(reader=_UdidStub())
    with pytest.raises(ValueError, match="name.*required"):
        await h.apply({"type": "run"})
    with pytest.raises(ValueError, match="name.*required"):
        await h.apply({"type": "run", "name": ""})
    with pytest.raises(ValueError, match="name.*required"):
        await h.apply({"type": "run", "name": 42})


async def test_apply_run_drives_simctl_openurl_with_correct_url(
        monkeypatch):
    """Full apply(run) path: monkeypatched subprocess captures the
    exact command shape that lands at `xcrun simctl openurl`."""
    captured = {}

    async def fake_create_subprocess_exec(*cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

            def kill(self):
                pass

        return _Proc()

    monkeypatch.setattr(sibb_state.asyncio, "create_subprocess_exec",
                         fake_create_subprocess_exec)

    h = ShortcutsHandler(reader=_UdidStub("ABC-123"))
    await h.apply({
        "type": "run",
        "name": "Quick Reminder",
        "input": "Buy milk",
    })

    assert captured["cmd"][:4] == (
        "xcrun", "simctl", "openurl", "ABC-123")
    url = captured["cmd"][4]
    assert url.startswith("shortcuts://run-shortcut?name=Quick+Reminder")
    assert "&input=text&text=Buy+milk" in url


async def test_apply_run_passes_dict_input_as_json(monkeypatch):
    captured = {}

    async def fake_create_subprocess_exec(*cmd, **kw):
        captured["url"] = cmd[4]

        class _Proc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

            def kill(self):
                pass

        return _Proc()

    monkeypatch.setattr(sibb_state.asyncio, "create_subprocess_exec",
                         fake_create_subprocess_exec)

    h = ShortcutsHandler(reader=_UdidStub("ABC"))
    await h.apply({
        "type": "run",
        "name": "Make List",
        "input": {"title": "Groceries", "list": "Reminders"},
    })

    qs = urllib.parse.urlparse(captured["url"]).query
    params = urllib.parse.parse_qs(qs)
    payload = json.loads(params["text"][0])
    assert payload == {"title": "Groceries", "list": "Reminders"}


async def test_apply_run_surfaces_simctl_failure(monkeypatch):
    """A non-zero exit from openurl must raise with stderr in the
    message — opaque iOS exit codes (149, 4) are otherwise
    undebuggable."""

    async def fake_create_subprocess_exec(*cmd, **kw):
        class _Proc:
            returncode = 149

            async def communicate(self):
                return (b"", b"Unable to lookup in current state: Shutdown")

            def kill(self):
                pass

        return _Proc()

    monkeypatch.setattr(sibb_state.asyncio, "create_subprocess_exec",
                         fake_create_subprocess_exec)

    h = ShortcutsHandler(reader=_UdidStub("ABC"))
    with pytest.raises(RuntimeError,
                         match=r"exit 149.*Shutdown"):
        await h.apply({"type": "run", "name": "X"})


async def test_apply_run_timeout_kills_subprocess(monkeypatch):
    """simctl can hang if the sim is shutting down mid-command;
    timeout must kill the subprocess rather than block forever."""

    async def fake_create_subprocess_exec(*cmd, **kw):
        class _Proc:
            returncode = None
            killed = False

            async def communicate(self):
                # Slow communicate triggers the wait_for timeout.
                # On retry (after kill), return immediately so the
                # cleanup path doesn't deadlock.
                if not self.killed:
                    await asyncio.sleep(30)
                return (b"", b"")

            def kill(self):
                self.killed = True

        return _Proc()

    monkeypatch.setattr(sibb_state.asyncio, "create_subprocess_exec",
                         fake_create_subprocess_exec)

    h = ShortcutsHandler(reader=_UdidStub("ABC"))
    with pytest.raises(RuntimeError, match="timed out"):
        await sibb_state._shortcuts_openurl(
            "ABC", "shortcuts://run-shortcut?name=X", timeout=0.05)


async def test_reset_is_noop():
    """Clone-from-baseline gives episode isolation. reset() should
    not attempt to delete shortcuts (Apple has no API anyway)."""
    h = ShortcutsHandler(reader=None)
    await h.reset()  # must not raise


# ─────────────────────── library tree parser ────────────────────────


class _StubElement:
    """Minimal AX element stub for the parser. `effective_role`
    must be an `ElementRole` value; `effective_label` is the cell
    label string."""

    def __init__(self, role, label):
        self.effective_role = role
        self.effective_label = label


def test_parse_library_extracts_user_shortcuts():
    """User shortcuts render as `<name>, <N> action[s]` cells; the
    parser pulls name + action count. Empirically verified against
    iOS 26.3 (the "Create List New, 1 action" cell shape)."""
    from sibb_scaffold import ElementRole

    elements = [
        _StubElement(ElementRole.CELL, "Create List New, 1 action"),
        _StubElement(ElementRole.CELL, "Find Places, 1 action"),
        _StubElement(ElementRole.CELL, "Share, 2 actions"),
    ]
    rows = _parse_shortcuts_library_tree(elements)
    assert rows == [
        {"name": "Create List New", "action_count": 1},
        {"name": "Find Places", "action_count": 1},
        {"name": "Share", "action_count": 2},
    ]


def test_parse_library_skips_app_suggestion_cells():
    """Apple's app-grouped suggestion cells (Scan Document, Recents,
    Places, …) lack the `, N action[s]` suffix — the parser is the
    discriminator that keeps them out of `shortcuts.installed`."""
    from sibb_scaffold import ElementRole

    elements = [
        _StubElement(ElementRole.CELL, "Scan Document"),
        _StubElement(ElementRole.CELL, "Recents"),
        _StubElement(ElementRole.CELL, "Places"),
        _StubElement(ElementRole.CELL, "Real Shortcut, 1 action"),
    ]
    rows = _parse_shortcuts_library_tree(elements)
    assert rows == [{"name": "Real Shortcut", "action_count": 1}]


def test_parse_library_handles_singular_vs_plural_action_word():
    """Cell labels use 'action' for 1, 'actions' for >1 — the regex
    must match both."""
    from sibb_scaffold import ElementRole

    elements = [
        _StubElement(ElementRole.CELL, "Single, 1 action"),
        _StubElement(ElementRole.CELL, "Multiple, 7 actions"),
    ]
    rows = _parse_shortcuts_library_tree(elements)
    assert {r["name"] for r in rows} == {"Single", "Multiple"}


def test_parse_library_handles_commas_in_shortcut_name():
    """Names with commas must still parse correctly — the regex
    uses a lazy `(.+?)` then anchors on `, N action[s]$`."""
    from sibb_scaffold import ElementRole

    el = _StubElement(ElementRole.CELL, "Tasks, work, errands, 3 actions")
    rows = _parse_shortcuts_library_tree([el])
    assert rows == [{
        "name": "Tasks, work, errands",
        "action_count": 3,
    }]


def test_parse_library_skips_non_cell_elements():
    """Buttons, StaticText, Images are noise — only Cell-role
    elements with the action-count annotation count as shortcuts."""
    from sibb_scaffold import ElementRole

    elements = [
        _StubElement(ElementRole.BUTTON, "Create Shortcut, 1 action"),
        _StubElement(ElementRole.STATIC_TEXT, "Library, 1 action"),
        _StubElement(ElementRole.CELL, "Real, 1 action"),
    ]
    rows = _parse_shortcuts_library_tree(elements)
    assert rows == [{"name": "Real", "action_count": 1}]


def test_parse_library_empty_tree():
    """No shortcuts (or no Library yet) → empty list, not error."""
    assert _parse_shortcuts_library_tree([]) == []


# ─────────────────────── resource fetcher wiring ─────────────────────


def test_shortcuts_installed_in_resource_fetchers():
    from sibb_verify import RESOURCE_FETCHERS
    assert "shortcuts.installed" in RESOURCE_FETCHERS


async def test_fetcher_requires_udid():
    from sibb_verify import RESOURCE_FETCHERS, ResourceFetchError

    class _NoUdid:
        pass
    fetcher = RESOURCE_FETCHERS["shortcuts.installed"]
    with pytest.raises(ResourceFetchError, match=".udid"):
        await fetcher(_NoUdid(), {})
