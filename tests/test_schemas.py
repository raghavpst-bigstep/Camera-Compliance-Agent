"""The agent's output contract.

The model is asked for JSON; it is not trusted to produce it. These tests pin
the two behaviours the incident flow depends on: unwrapping a response, and
refusing a half-resolved OK.
"""

from __future__ import annotations

import pytest

from camera_agent.schemas import IntakeResult, MessageResult, extract_json

RESOLVED = {
    "status": "OK",
    "employee": {"name": "Priya Sharma", "email": "priya@co.com"},
    "manager": {"name": "Dan Ortiz", "email": "dan@co.com"},
    "hr": {"name": "Asha Rao", "email": "hr@co.com"},
    "meeting": "Daily standup",
    "meeting_date": "2026-09-23",
    "issue": "camera off for the whole call",
    "screenshot_attached": False,
    "message_to_post": "Please share why your camera was off.",
    "missing": [],
}


@pytest.mark.parametrize(
    "raw",
    [
        '{"status":"OK"}',
        '```json\n{"status":"OK"}\n```',
        '```\n{"status":"OK"}\n```',
        'Here you go:\n{"status":"OK"}\nhope that helps',
    ],
)
def test_extract_json_unwraps_what_models_actually_return(raw):
    assert extract_json(raw) == {"status": "OK"}


@pytest.mark.parametrize("raw", ["", "   ", "no json at all"])
def test_extract_json_rejects_unusable_output(raw):
    with pytest.raises(ValueError):
        extract_json(raw)


def test_complete_intake_stays_ok():
    result = IntakeResult.model_validate(RESOLVED)
    assert result.status == "OK"
    assert result.missing == []


@pytest.mark.parametrize(
    "gap,patch",
    [
        ("employee", {"employee": {"name": "Priya", "email": ""}}),
        ("manager", {"manager": {"name": "Dan", "email": ""}}),
        ("hr", {"hr": {"name": "", "email": ""}}),
        ("meeting_date", {"meeting_date": ""}),
        ("message_to_post", {"message_to_post": "   "}),
    ],
)
def test_half_resolved_ok_is_downgraded(gap, patch):
    """A missing field would put the wrong people in a space. Ask instead."""
    result = IntakeResult.model_validate({**RESOLVED, **patch})
    assert result.status == "CLARIFY"
    assert gap in result.missing
    assert result.message_to_post == ""


def test_clarify_never_carries_a_message_to_post():
    result = IntakeResult.model_validate(
        {"status": "CLARIFY", "message_to_post": "should be dropped"}
    )
    assert result.message_to_post == ""
    assert result.missing == ["unspecified"]


def test_clarify_keeps_a_stated_reason():
    result = IntakeResult.model_validate(
        {"status": "CLARIFY", "missing": ["employee_email"]}
    )
    assert result.missing == ["employee_email"]


def test_message_classification_is_closed_set():
    assert (
        MessageResult.model_validate(
            {"classification": "CLOSURE_COMMAND", "sender_role": "manager"}
        ).classification
        == "CLOSURE_COMMAND"
    )
    with pytest.raises(Exception):
        MessageResult.model_validate({"classification": "MADE_UP"})


def test_sender_role_defaults_to_unknown():
    assert MessageResult.model_validate({"classification": "OTHER"}).sender_role == "unknown"
