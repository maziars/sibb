"""Phase-B parser — ANSWER terminal action grammar.

Covers `SIBBScaffold.parse_action` for the new `ANSWER <json>` verb.
ANSWER is terminal (joins DONE/FAIL); the payload must be a single-line
JSON object. Backtick fences with optional `json` tag are tolerated.
Anything else — multi-line, prose, non-object top-level — fails parse
with `parse_error` set and `answer_payload=None`, which the verifier
turns into `failure_kind=no_answer`.
"""

from __future__ import annotations

import pytest

from sibb_scaffold import SIBBScaffold, AgentAction

pytestmark = pytest.mark.fast


@pytest.fixture
def parser():
    return SIBBScaffold("MOCK_UDID")


# ─────────────────────────── positive parses ─────────────────────────

def test_parse_answer_items_list(parser):
    a = parser.parse_action(
        'ANSWER {"items": [{"title": "Buy milk"}, {"title": "Call Bob"}]}'
    )
    assert a.action_type == "answer"
    assert a.parse_error is None
    assert a.answer_payload == {
        "items": [{"title": "Buy milk"}, {"title": "Call Bob"}]
    }


def test_parse_answer_count(parser):
    a = parser.parse_action('ANSWER {"count": 5}')
    assert a.action_type == "answer"
    assert a.answer_payload == {"count": 5}


def test_parse_answer_value_string(parser):
    a = parser.parse_action('ANSWER {"value": "+1-555-1234"}')
    assert a.answer_payload == {"value": "+1-555-1234"}


def test_parse_answer_boolean(parser):
    a = parser.parse_action('ANSWER {"answer": true}')
    assert a.answer_payload == {"answer": True}


def test_parse_answer_with_json_fences(parser):
    a = parser.parse_action('ANSWER ```json{"count": 5}```')
    assert a.parse_error is None
    assert a.answer_payload == {"count": 5}


def test_parse_answer_with_plain_fences(parser):
    a = parser.parse_action('ANSWER ```{"count": 5}```')
    assert a.parse_error is None
    assert a.answer_payload == {"count": 5}


def test_parse_answer_with_single_backticks(parser):
    a = parser.parse_action('ANSWER `{"count": 5}`')
    assert a.parse_error is None
    assert a.answer_payload == {"count": 5}


def test_parse_answer_handles_unicode_titles(parser):
    a = parser.parse_action(
        'ANSWER {"items": [{"title": "Café résumé"}]}'
    )
    assert a.parse_error is None
    assert a.answer_payload["items"][0]["title"] == "Café résumé"


def test_parse_answer_handles_escaped_quotes(parser):
    a = parser.parse_action(
        r'ANSWER {"value": "she said \"hi\""}'
    )
    assert a.parse_error is None
    assert a.answer_payload == {"value": 'she said "hi"'}


# ─────────────────────────── negative parses ─────────────────────────

def test_parse_answer_malformed_json_records_error(parser):
    a = parser.parse_action('ANSWER {malformed')
    assert a.action_type == "answer"
    assert a.answer_payload is None
    assert a.parse_error is not None
    assert "ANSWER JSON parse error" in a.parse_error


def test_parse_answer_top_level_list_rejected(parser):
    # The contract is "JSON object" — a top-level list, string, or
    # number is rejected so generators can rely on a dict shape.
    a = parser.parse_action('ANSWER [1, 2, 3]')
    assert a.action_type == "answer"
    assert a.answer_payload is None
    assert "must be a JSON object" in a.parse_error


def test_parse_answer_top_level_string_rejected(parser):
    a = parser.parse_action('ANSWER "just a string"')
    assert a.answer_payload is None
    assert "must be a JSON object" in a.parse_error


def test_parse_answer_top_level_number_rejected(parser):
    a = parser.parse_action("ANSWER 42")
    assert a.answer_payload is None
    assert "must be a JSON object" in a.parse_error


def test_parse_answer_prose_after_verb_fails(parser):
    # "ANSWER The reminders due tomorrow are..." doesn't survive
    # json.loads on the prose tail. Demonstrates we won't accept
    # narrative ANSWERs even if they end with valid JSON.
    a = parser.parse_action("ANSWER The reminders are: nope")
    assert a.answer_payload is None
    assert a.parse_error is not None


def test_parse_answer_bare_token_fails(parser):
    a = parser.parse_action("ANSWER")
    assert a.answer_payload is None
    # Empty raw → json.loads('') errors; we treat that as a parse error.
    assert a.parse_error is not None


# ─────────────────────────── grammar regressions ──────────────────────

def test_done_unaffected(parser):
    a = parser.parse_action('DONE "all set"')
    assert a.action_type == "done"
    assert a.answer_payload is None
    assert a.parse_error is None


def test_fail_unaffected(parser):
    a = parser.parse_action('FAIL "stuck"')
    assert a.action_type == "fail"
    assert a.answer_payload is None


def test_tap_unaffected(parser):
    a = parser.parse_action("TAP @e042")
    assert a.action_type == "tap"
    assert a.target_ref == "e042"
    assert a.answer_payload is None


def test_unrecognized_verb_still_fails(parser):
    a = parser.parse_action("REPLY {}")
    assert a.action_type == "fail"
    assert "Unrecognized" in (a.reason or "")


# ───────── multi-line input — scan for the LAST action verb ──────────
# Frontier LLMs (Claude, GPT-4) typically reason aloud before emitting
# the final terminal action. The parser must scan from the bottom for
# the last line that starts with a recognized verb; the old
# `split("\n")[0]` semantics silently misparsed every reasoning emission.

def test_parse_last_line_answer_after_reasoning(parser):
    msg = (
        "Let me count the overdue items.\n"
        "I see 'Buy milk' (yesterday) and 'Pay rent' (last week).\n"
        'ANSWER {"count": 2}'
    )
    a = parser.parse_action(msg)
    assert a.action_type == "answer"
    assert a.answer_payload == {"count": 2}


def test_parse_last_line_done_after_reasoning(parser):
    msg = "I completed the task.\nDONE \"all set\""
    a = parser.parse_action(msg)
    assert a.action_type == "done"


def test_parse_first_action_line_when_only_one_present(parser):
    # Backward-compat: single-line input still works.
    a = parser.parse_action('ANSWER {"count": 1}')
    assert a.action_type == "answer"
    assert a.answer_payload == {"count": 1}


def test_parse_picks_last_verb_when_multiple_present(parser):
    # If reasoning includes verb-looking words ("I will TAP the…"),
    # the parser should still pick the LAST verb-prefixed line.
    msg = (
        "TAP @e0042\n"
        "Actually, let me reconsider.\n"
        'ANSWER {"value": "final"}'
    )
    a = parser.parse_action(msg)
    assert a.action_type == "answer"
    assert a.answer_payload == {"value": "final"}


def test_parse_ignores_inline_verb_token_inside_prose(parser):
    # The verb scan is anchored to start-of-line. Mid-line uses don't
    # pretend to be the action.
    msg = (
        "I should TAP on Reminders.\n"
        'ANSWER {"count": 5}'
    )
    a = parser.parse_action(msg)
    assert a.action_type == "answer"
    assert a.answer_payload == {"count": 5}
