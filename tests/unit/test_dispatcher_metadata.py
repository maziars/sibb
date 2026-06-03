"""A2 — handler-declared metadata consumed by the dispatcher.

`pre_runner_kinds` (per-handler list) replaces the external
`PRE_RUNNER_KINDS` denylist; `collect_tcc_services()` aggregates
handler TCC services for the simulator-side pre-grant.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

import sibb_state

pytestmark = pytest.mark.fast


# ───────────────────────── pre_runner_kinds ──────────────────────────

@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_handler_declares_pre_runner_kinds(name: str):
    cls = sibb_state.HANDLERS[name]
    assert isinstance(cls.pre_runner_kinds, list)
    assert all(isinstance(k, str) for k in cls.pre_runner_kinds)


@pytest.mark.parametrize("name", sorted(sibb_state.HANDLERS))
def test_pre_runner_flag_matches_kinds_truthiness(name: str):
    cls = sibb_state.HANDLERS[name]
    assert cls.pre_runner == bool(cls.pre_runner_kinds), (
        f"{cls.__name__}: pre_runner ({cls.pre_runner}) must equal "
        f"bool(pre_runner_kinds) ({cls.pre_runner_kinds!r}); inconsistent "
        "values cause the dispatcher to silently mis-route entries."
    )


def test_module_level_pre_runner_kinds_constant_removed():
    # A2 deleted `PRE_RUNNER_KINDS = {("Springboard","layout"), ...}` at
    # sibb_state.py:267. Reintroducing it as a module-level binding
    # bypasses the per-handler declaration and defeats A2. (We deliberately
    # check the symbol, not the string — A2's doc comments may legitimately
    # reference the old name when explaining the migration.)
    assert not hasattr(sibb_state, "PRE_RUNNER_KINDS"), (
        "sibb_state.PRE_RUNNER_KINDS module-level set must stay deleted; "
        "use `is_pre_runner_entry(entry)` instead."
    )


def test_is_pre_runner_entry_springboard_layout_is_pre_runner():
    assert sibb_state.is_pre_runner_entry(
        {"app": "Springboard", "type": "layout"}
    ) is True


def test_is_pre_runner_entry_springboard_dock_is_pre_runner():
    assert sibb_state.is_pre_runner_entry(
        {"app": "Springboard", "type": "dock"}
    ) is True


def test_is_pre_runner_entry_springboard_start_page_is_runtime():
    assert sibb_state.is_pre_runner_entry(
        {"app": "Springboard", "type": "start_page"}
    ) is False


def test_is_pre_runner_entry_reminders_kinds_are_runtime():
    for kind in ("list", "item"):
        assert sibb_state.is_pre_runner_entry(
            {"app": "Reminders", "type": kind}
        ) is False


def test_is_pre_runner_entry_unknown_app_safe_default():
    # A typo in `entry["app"]` should NOT crash the dispatcher; it
    # should fall through to the runtime path (where unknown-app
    # errors are reported in the per-entry error log).
    assert sibb_state.is_pre_runner_entry(
        {"app": "Spaceship", "type": "layout"}
    ) is False
    assert sibb_state.is_pre_runner_entry({}) is False


# ───────────────────── collect_tcc_services ───────────────────────────

def test_collect_tcc_services_returns_sorted_list_of_strings():
    services = sibb_state.collect_tcc_services()
    assert isinstance(services, list)
    assert all(isinstance(s, str) for s in services)
    assert services == sorted(services)


def test_collect_tcc_services_is_union_with_dedupe():
    services = sibb_state.collect_tcc_services()
    expected = set()
    for cls in sibb_state.HANDLERS.values():
        expected.update(cls.tcc_services)
    assert set(services) == expected
    assert len(services) == len(set(services)), "duplicates leaked"


def test_collect_tcc_services_today_covers_reminders():
    # Soft assertion: the currently registered handlers include
    # Reminders, so `reminders` is grantable. When Calendar/Contacts
    # land they appear here too; that's the point of the helper.
    assert "reminders" in sibb_state.collect_tcc_services()


# ─────────── ensure_runner_permissions wiring (source lint) ───────────

def test_ensure_runner_permissions_no_longer_hardcodes_services():
    # The hardcoded tuple at sibb_xcuitest_client.py:125 was the bug;
    # A2 replaced it with collect_tcc_services() via lazy import.
    import sibb_xcuitest_client
    src = pathlib.Path(sibb_xcuitest_client.__file__).read_text()
    assert '("reminders", "calendar", "contacts")' not in src, (
        "ensure_runner_permissions reverted to a hardcoded service tuple "
        "— must source from sibb_state.collect_tcc_services()."
    )


def test_ensure_runner_permissions_calls_collect_tcc_services():
    import sibb_xcuitest_client
    src = pathlib.Path(sibb_xcuitest_client.__file__).read_text()
    assert "collect_tcc_services" in src, (
        "ensure_runner_permissions must call collect_tcc_services() "
        "to derive the grant list from handler declarations."
    )


def test_ensure_runner_permissions_signature_unchanged():
    # We deliberately kept the (udid) signature so all callers in
    # XCUITestReader.start() and elsewhere don't need to change.
    import sibb_xcuitest_client
    sig = inspect.signature(sibb_xcuitest_client.ensure_runner_permissions)
    params = list(sig.parameters.keys())
    assert params == ["udid"], (
        f"ensure_runner_permissions signature changed to {params!r}; "
        "A2 was meant to keep this contract stable and source services "
        "from HANDLERS via lazy import."
    )
