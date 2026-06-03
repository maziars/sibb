"""L1 tests for the message-payload picker + spec builder used by
the Phase 3 message-driven Contacts/Maps generators.

Covers:
  - `_pick_message_payload` returns str OR list[str] based on the
    multi_prob random draw
  - `_build_message_spec` serializes single → `text`, list → `texts`
  - Per-variant outer instructions match the user-approved phrasing
    (anchored to "Find the latest received message")
  - The label (home/work/other) appears in every address-variant body
    (single or multi) — the agent has to parse it from the message,
    NOT the outer prompt
  - All 5 generators emit one Messages spec entry with valid payload
"""
from __future__ import annotations
import os
import random
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(THIS_DIR, "..", "..", "benchmark")))

from sibb_task_generator_v3 import (   # noqa: E402
    _pick_message_payload, _build_message_spec,
    _MESSAGE_TEMPLATES_SELF_INTRO_SINGLE,
    _MESSAGE_TEMPLATES_SELF_INTRO_MULTI,
    _MESSAGE_TEMPLATES_ADDRESS_SINGLE,
    _MESSAGE_TEMPLATES_ADDRESS_MULTI,
    _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_SINGLE,
    _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_MULTI,
    _INSTRUCTION_A, _INSTRUCTION_B, _INSTRUCTION_C,
    _INSTRUCTION_D, _INSTRUCTION_E, _INSTRUCTION_F,
    gen_message_save_sender, gen_message_save_body,
    gen_message_save_address, gen_message_to_contact_to_maps,
    gen_message_to_new_contact_to_maps,
)


# ── picker ────────────────────────────────────────────────────────────────────

def test_picker_returns_string_with_multi_prob_zero():
    """multi_prob=0 → always single-bubble (string return)."""
    random.seed(42)
    for _ in range(10):
        out = _pick_message_payload(
            _MESSAGE_TEMPLATES_SELF_INTRO_SINGLE,
            _MESSAGE_TEMPLATES_SELF_INTRO_MULTI,
            0.0,
            "Sarah", "Lin")
        assert isinstance(out, str)


def test_picker_returns_list_with_multi_prob_one():
    """multi_prob=1.0 → always multi-bubble (list return)."""
    random.seed(42)
    for _ in range(10):
        out = _pick_message_payload(
            _MESSAGE_TEMPLATES_SELF_INTRO_SINGLE,
            _MESSAGE_TEMPLATES_SELF_INTRO_MULTI,
            1.0,
            "Sarah", "Lin")
        assert isinstance(out, list)
        assert all(isinstance(t, str) for t in out)
        assert len(out) >= 2, "multi-bubble must have ≥2 bubbles"


def test_picker_falls_back_to_single_when_multi_empty():
    """If no multi templates configured, picker returns single
    regardless of multi_prob."""
    random.seed(42)
    out = _pick_message_payload(
        _MESSAGE_TEMPLATES_SELF_INTRO_SINGLE,
        [],   # no multi templates
        1.0,  # would normally always pick multi
        "Sarah", "Lin")
    assert isinstance(out, str)


# ── spec builder ──────────────────────────────────────────────────────────────

def test_spec_builder_single_string_uses_text_key():
    spec = _build_message_spec("hello world")
    assert spec["text"] == "hello world"
    assert "texts" not in spec
    assert spec["app"] == "Messages"
    assert spec["type"] == "send_in_thread"
    assert spec["thread"] == "JA"


def test_spec_builder_list_uses_texts_key():
    spec = _build_message_spec(["a", "b", "c"])
    assert spec["texts"] == ["a", "b", "c"]
    assert "text" not in spec


def test_spec_builder_list_drops_empty_bubbles():
    """Empty strings in the bubble list are dropped (handler also
    drops them defensively; this keeps the spec clean)."""
    spec = _build_message_spec(["a", "", "b", None])
    assert spec["texts"] == ["a", "b"]


# ── outer instructions ────────────────────────────────────────────────────────

def test_all_instructions_anchor_to_find_latest():
    """User-approved phrasing: every variant's outer prompt opens
    with 'Find the latest received message'."""
    for inst in (_INSTRUCTION_A, _INSTRUCTION_B, _INSTRUCTION_C,
                 _INSTRUCTION_D, _INSTRUCTION_E, _INSTRUCTION_F):
        assert inst.startswith("Find the latest received message"), \
            f"instruction must start with the anchor: {inst!r}"


def test_a_instruction_names_phone_source():
    assert "phone number the message came from" in _INSTRUCTION_A


def test_b_instruction_warns_off_sender_phone():
    """B specifically rules out the sender's phone — closes the
    iOS Messages-shortcut cheat from the 2026-05-27 trial."""
    assert "only the name and phone in the message" in _INSTRUCTION_B
    assert "not the number it came from" in _INSTRUCTION_B


def test_c_instruction_is_unified_create_or_update():
    """C handles both paths in one prompt per user instruction:
    'If they're not already in your contacts, create a new
    contact for them.'"""
    assert "not already in your contacts" in _INSTRUCTION_C
    assert "create a new contact" in _INSTRUCTION_C


def test_d_instruction_names_maps_directions():
    assert "open Maps" in _INSTRUCTION_D
    # Goal-oriented phrasing (added 2026-05-31): the agent must
    # actually start nav (taps GO), not just preview routes — the
    # verifier's geo_within_m check fires only on an activated
    # route. Phrased "start navigating" so the instruction is not
    # coupled to the iOS-version-specific "GO" button name.
    assert "start navigating" in _INSTRUCTION_D
    assert "already in your Contacts" in _INSTRUCTION_D


def test_e_instruction_names_create_then_maps():
    assert "create a new contact" in _INSTRUCTION_E.lower()
    assert "isn't in your contacts" in _INSTRUCTION_E
    assert "start navigating" in _INSTRUCTION_E


# ── label sourcing: must come from body, not instruction ──────────────────────

def test_address_label_emphasized_in_instructions():
    """Address-variant instructions explicitly tell the agent to
    save under the CORRECT label specified in the message — added
    2026-05-28 after variant D trial showed the agent defaulting
    to iOS' "home" label without checking the message body. The
    label terms (home/work/other) must appear in C/D/E/F so the
    LLM is primed to look for them and tap the label cell on the
    Contacts address form."""
    for inst in (_INSTRUCTION_C, _INSTRUCTION_D, _INSTRUCTION_E,
                 _INSTRUCTION_F):
        assert "label" in inst.lower(), (
            f"instruction missing 'label' guidance: {inst!r}")
        assert "home/work/other" in inst, (
            f"instruction missing label choices (home/work/other): "
            f"{inst!r}")


def _every_address_body_contains_label(template_single, template_multi):
    """Render every (single + multi) template for every label and
    assert the rendered body contains the label string. Catches
    template regressions that drop the label."""
    for lbl in ("home", "work", "other"):
        for tmpl in template_single:
            body = tmpl("Sarah", "Lin", "1 Apple Park", "Cupertino", lbl)
            assert lbl in body, (
                f"single template {tmpl.__code__.co_firstlineno} dropped "
                f"label {lbl!r}: {body!r}")
        for tmpl in template_multi:
            body_list = tmpl("Sarah", "Lin", "1 Apple Park", "Cupertino", lbl)
            joined = " ".join(body_list)
            assert lbl in joined, (
                f"multi template dropped label {lbl!r}: {body_list!r}")


def test_variant_c_templates_always_carry_label():
    _every_address_body_contains_label(
        _MESSAGE_TEMPLATES_ADDRESS_SINGLE,
        _MESSAGE_TEMPLATES_ADDRESS_MULTI)


def test_variant_de_templates_always_carry_label():
    _every_address_body_contains_label(
        _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_SINGLE,
        _MESSAGE_TEMPLATES_ADDRESS_PLUS_DIRECTIONS_MULTI)


# ── end-to-end: generators emit valid spec entries ───────────────────────────

def _messages_spec_entry(task):
    msg_entries = [e for e in task.initial_state.spec
                   if e.get("app") == "Messages"]
    assert len(msg_entries) == 1, (
        "exactly one Messages spec entry expected")
    return msg_entries[0]


def test_all_generators_emit_valid_message_spec():
    gens = [gen_message_save_sender, gen_message_save_body,
            gen_message_save_address, gen_message_to_contact_to_maps,
            gen_message_to_new_contact_to_maps]
    for fn in gens:
        for s in (42, 7, 11):
            random.seed(s)
            t = fn()
            entry = _messages_spec_entry(t)
            assert entry["type"] == "send_in_thread"
            assert entry["thread"] == "JA"
            payload = entry.get("texts") or entry.get("text")
            assert payload, (
                f"{fn.__name__} seed={s} emitted empty payload")
            if isinstance(payload, list):
                assert all(isinstance(b, str) and b for b in payload)
            else:
                assert isinstance(payload, str)


def test_generators_use_their_variant_instruction():
    """Each generator returns a Task whose instruction matches its
    variant constant — catches the 'forgot to update instruction='
    refactor bug."""
    cases = [
        (gen_message_save_sender,            _INSTRUCTION_A),
        (gen_message_save_body,              _INSTRUCTION_B),
        (gen_message_save_address,           _INSTRUCTION_C),
        (gen_message_to_contact_to_maps,     _INSTRUCTION_D),
        (gen_message_to_new_contact_to_maps, _INSTRUCTION_E),
    ]
    for fn, expected_inst in cases:
        random.seed(42)
        t = fn()
        assert t.instruction == expected_inst, (
            f"{fn.__name__} instruction mismatch: "
            f"got {t.instruction!r}, expected {expected_inst!r}")
