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
    """Nothing drafted for an employee may survive an unresolved report."""
    result = IntakeResult.model_validate(
        {"status": "CLARIFY", "message_to_post": "should be dropped"}
    )
    assert result.message_to_post == ""


def test_clarify_names_every_real_gap():
    """An empty result must say what is missing, not just 'unspecified'."""
    result = IntakeResult.model_validate({"status": "CLARIFY"})
    assert set(result.missing) == {"employee", "manager", "hr", "meeting_date"}


def test_the_models_own_reason_is_kept_alongside_computed_gaps():
    result = IntakeResult.model_validate(
        {"status": "CLARIFY", "missing": ["employee_email"]}
    )
    assert "employee_email" in result.missing
    assert "manager" in result.missing


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


# ---------------------------------------------------------------------------
# Date normalisation. The same email produced "2026-09-23" on one run and
# "2026-09-23T00:00:00Z" on the next; that column has to be consistent because
# HR sorts and filters on it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-23", "2026-09-23"),
        ("2026-09-23T00:00:00Z", "2026-09-23"),
        ("2026-09-23T00:00:00+0000", "2026-09-23"),
        ("2026-09-23 00:00", "2026-09-23"),
        ("2026-09-23T10:30", "2026-09-23T10:30"),
        ("2026-09-23T10:30:00Z", "2026-09-23T10:30"),
        ("2026-09-23 10:30:00", "2026-09-23T10:30"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_dates_are_canonicalised(raw, expected):
    from camera_agent.schemas import normalise_date

    assert normalise_date(raw) == expected


def test_midnight_is_treated_as_no_time_given():
    """Nobody holds a standup at midnight; it means the model invented a time."""
    from camera_agent.schemas import normalise_date

    assert "T" not in normalise_date("2026-09-23T00:00:00Z")


def test_unparseable_dates_survive_rather_than_vanish():
    """A human still needs to see what the manager actually wrote."""
    from camera_agent.schemas import normalise_date

    assert normalise_date("last Tuesday") == "last Tuesday"


def test_date_is_normalised_on_the_way_through_the_model():
    result = IntakeResult.model_validate({**RESOLVED, "meeting_date": "2026-09-23T00:00:00Z"})
    assert result.meeting_date == "2026-09-23"
    assert result.status == "OK"


def test_gaps_are_recomputed_not_taken_on_trust():
    """The model's own `missing` list varies run to run for identical input."""
    understated = {
        "status": "CLARIFY",
        "employee": {"name": "", "email": ""},
        "manager": {"name": "", "email": ""},
        "hr": {"name": "", "email": ""},
        "meeting_date": "",
        "missing": ["employee_email"],
    }
    result = IntakeResult.model_validate(understated)
    assert set(result.missing) >= {"employee", "manager", "hr", "meeting_date"}


def test_clarify_with_everything_resolved_is_respected():
    """e.g. the mail was not a compliance report at all."""
    result = IntakeResult.model_validate(
        {**RESOLVED, "status": "CLARIFY", "missing": ["not_a_camera_compliance_report"]}
    )
    assert result.status == "CLARIFY"
    assert result.missing == ["not_a_camera_compliance_report"]
    assert result.message_to_post == ""
