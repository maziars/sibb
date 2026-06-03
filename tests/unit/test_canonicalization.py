"""A3 — bundle-id canonicalization on lookup.

The dispatch path is now resilient to casing/spelling drift in
generator-emitted `task.apps` and `spec[*]["app"]` strings. Closes
the historical "SpringBoard" vs "Springboard" silent-skip bug
(`noise_layout:624` vs the old `HANDLERS["Springboard"]`).
"""

from __future__ import annotations

import pytest

import sibb_state

pytestmark = pytest.mark.fast


# ───────────────────────── HANDLERS keying ────────────────────────────

def test_handlers_registry_keyed_by_bundle_id():
    for key in sibb_state.HANDLERS:
        assert key.startswith("com."), (
            f"HANDLERS key {key!r} is not a bundle id; A3 re-keys "
            "the registry by `cls.bundle_id` so friendly-name drift "
            "is impossible at the lookup layer."
        )


def test_handlers_keys_match_bundle_id_attr():
    for key, cls in sibb_state.HANDLERS.items():
        assert key == cls.bundle_id, (
            f"HANDLERS[{key!r}] is {cls.__name__} with "
            f"bundle_id={cls.bundle_id!r}; keys must agree."
        )


def test_handlers_keys_unique():
    keys = list(sibb_state.HANDLERS)
    assert len(keys) == len(set(keys))


# ───────────────────── canonicalize_app behavior ─────────────────────

@pytest.mark.parametrize("variant", [
    "Reminders", "reminders", "REMINDERS", "ReMiNdErS",
    "com.apple.reminders", "COM.APPLE.REMINDERS",
])
def test_canonicalize_reminders_variants(variant: str):
    assert sibb_state.canonicalize_app(variant) == "com.apple.reminders"


@pytest.mark.parametrize("variant", [
    "Springboard", "SpringBoard", "springboard", "SPRINGBOARD",
    "com.apple.springboard",
])
def test_canonicalize_springboard_variants_kills_casing_bug(variant: str):
    # The literal bug: `noise_layout` in sibb_task_generator_v3.py:624
    # historically emitted "SpringBoard" while HANDLERS keyed by
    # "Springboard" — silent skip. After A3 every casing maps to the
    # bundle id.
    assert sibb_state.canonicalize_app(variant) == "com.apple.springboard"


@pytest.mark.parametrize("bad", [
    "Wallet",                     # deferred — see IOS_SIM_QUIRKS §15
    "Preview",                    # skipped — unlaunchable on sim
    "spaceship",
    "com.apple.notarealapp",
    "",
])
def test_canonicalize_unknown_returns_none(bad: str):
    assert sibb_state.canonicalize_app(bad) is None


@pytest.mark.parametrize("bad", [None, 42, 3.14, [], {}, object()])
def test_canonicalize_non_string_returns_none(bad):
    # Defensive: a malformed task.apps entry must not crash dispatch.
    assert sibb_state.canonicalize_app(bad) is None


def test_canonicalize_round_trips_through_handlers():
    # For any registered handler, canonicalizing the friendly name
    # gets us back the same class via HANDLERS.
    for bid, cls in sibb_state.HANDLERS.items():
        friendly = cls.__name__[: -len("Handler")] if cls.__name__.endswith("Handler") else cls.__name__
        assert sibb_state.HANDLERS[sibb_state.canonicalize_app(friendly)] is cls
        assert sibb_state.HANDLERS[sibb_state.canonicalize_app(bid)] is cls


# ───── is_pre_runner_entry under casing drift (the actual bug) ─────

def test_is_pre_runner_entry_canonicalizes_casing():
    # The historical bug surface. Both casings now route correctly.
    for casing in ("Springboard", "SpringBoard", "springboard", "SPRINGBOARD"):
        assert sibb_state.is_pre_runner_entry(
            {"app": casing, "type": "layout"}
        ) is True, (
            f"casing variant {casing!r} should route to Springboard "
            "(this is the noise_layout silent-skip bug A3 closed)"
        )


def test_is_pre_runner_entry_canonicalizes_bundle_id():
    assert sibb_state.is_pre_runner_entry(
        {"app": "com.apple.springboard", "type": "dock"}
    ) is True


def test_is_pre_runner_entry_unknown_app_safe():
    # A typo in `entry["app"]` must NOT crash dispatch nor be treated
    # as pre-runner (it's left to fall through to the runtime error path).
    assert sibb_state.is_pre_runner_entry(
        {"app": "Spaceshipboard", "type": "layout"}
    ) is False
