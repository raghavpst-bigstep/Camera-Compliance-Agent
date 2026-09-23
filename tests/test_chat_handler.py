"""What the orchestrator does with a message posted in an incident space.

The classification comes from the model; the authority to act on it does not.
These tests pin that separation, because a closure by the wrong person would
end an incident without review.
"""

from __future__ import annotations

import pytest

import main

SPACE = "spaces/TEST"

INCIDENT = {
    "incident_id": "CAM-2026-0001",
    "space_name": SPACE,
    "state": "AWAITING_JUSTIFICATION",
    "employee_name": "Priya Sharma",
    "employee_email": "priya@co.com",
    "manager_email": "dan@co.com",
    "hr_email": "hr@co.com",
    "subscription_name": "subscriptions/S1",
}


def event(sender_email: str, text: str, sender_type: str = "HUMAN") -> dict:
    return {
        "message": {
            "name": "spaces/TEST/messages/M1",
            "space": {"name": SPACE},
            "sender": {
                "name": "users/1",
                "email": sender_email,
                "displayName": sender_email.split("@")[0],
                "type": sender_type,
            },
            "text": text,
            "createTime": "2026-09-23T10:00:00Z",
        }
    }


ATTRS = {"ce-type": "google.workspace.chat.message.v1.created"}


class Calls:
    def __init__(self):
        self.justifications = []
        self.followups = []
        self.closures = []
        self.posted = []
        self.unsubscribed = []
        self.audited = []


@pytest.fixture
def calls(monkeypatch):
    c = Calls()
    monkeypatch.setattr(main.state, "claim_event", lambda key: True)
    monkeypatch.setattr(main.state, "find_incident_by_space", lambda s: dict(INCIDENT))
    monkeypatch.setattr(main.state, "get_incident", lambda i: dict(INCIDENT))
    monkeypatch.setattr(main.state, "update_incident", lambda i, f: None)
    monkeypatch.setattr(
        main.state, "record_justification",
        lambda i, t, s: c.justifications.append((i, t, s)),
    )
    monkeypatch.setattr(
        main.state, "record_followup", lambda i, by: c.followups.append((i, by))
    )
    monkeypatch.setattr(
        main.state, "record_closure",
        lambda i, by, out: c.closures.append((i, by, out)),
    )
    monkeypatch.setattr(
        main.chat_client, "post_message",
        lambda s, t: c.posted.append((s, t)) or {"name": "m"},
    )
    monkeypatch.setattr(
        main.chat_client, "delete_subscription",
        lambda n: c.unsubscribed.append(n) or True,
    )
    monkeypatch.setattr(
        main.audit, "append_incident", lambda inc: c.audited.append(inc) or True
    )
    return c


def _classifies_as(monkeypatch, classification):
    monkeypatch.setattr(
        main.agent_client, "invoke",
        lambda payload, rid: {"classification": classification,
                              "sender_role": payload["sender_role"]},
    )


def test_employee_justification_is_recorded(calls, monkeypatch):
    _classifies_as(monkeypatch, "EMPLOYEE_JUSTIFICATION")
    main._handle_chat("pm-1", event("priya@co.com", "My camera stopped working"), ATTRS)

    assert len(calls.justifications) == 1
    assert calls.justifications[0][1] == "My camera stopped working"
    assert calls.closures == []


def test_manager_can_close(calls, monkeypatch):
    _classifies_as(monkeypatch, "CLOSURE_COMMAND")
    main._handle_chat("pm-2", event("dan@co.com", "resolved"), ATTRS)

    assert len(calls.closures) == 1
    assert calls.closures[0][1] == "dan@co.com"
    assert calls.unsubscribed == ["subscriptions/S1"]
    assert len(calls.audited) == 1


def test_hr_can_close(calls, monkeypatch):
    _classifies_as(monkeypatch, "CLOSURE_COMMAND")
    main._handle_chat("pm-3", event("hr@co.com", "closed"), ATTRS)
    assert len(calls.closures) == 1


@pytest.mark.parametrize("sender", ["priya@co.com", "stranger@co.com"])
def test_nobody_else_can_close_even_if_the_model_says_so(calls, monkeypatch, sender):
    """The model classifying CLOSURE_COMMAND is not authority to close."""
    _classifies_as(monkeypatch, "CLOSURE_COMMAND")
    main._handle_chat("pm-4", event(sender, "resolved"), ATTRS)

    assert calls.closures == []
    assert calls.audited == []
    assert calls.unsubscribed == []


def test_followup_from_manager_restarts_the_clock(calls, monkeypatch):
    _classifies_as(monkeypatch, "FOLLOWUP_QUESTION")
    main._handle_chat("pm-5", event("dan@co.com", "Did you try restarting?"), ATTRS)
    assert calls.followups == [("CAM-2026-0001", "dan@co.com")]


def test_followup_from_a_stranger_is_ignored(calls, monkeypatch):
    _classifies_as(monkeypatch, "FOLLOWUP_QUESTION")
    main._handle_chat("pm-6", event("stranger@co.com", "what's going on"), ATTRS)
    assert calls.followups == []


def test_other_messages_change_nothing(calls, monkeypatch):
    _classifies_as(monkeypatch, "OTHER")
    main._handle_chat("pm-7", event("priya@co.com", "thanks"), ATTRS)
    assert (calls.justifications, calls.followups, calls.closures) == ([], [], [])


def test_the_agents_own_posts_are_ignored(calls, monkeypatch):
    """Otherwise it would answer its own prompts and reminders."""
    _classifies_as(monkeypatch, "EMPLOYEE_JUSTIFICATION")
    main._handle_chat("pm-8", event("", "Please provide a justification", "BOT"), ATTRS)
    assert calls.justifications == []


def test_a_resolved_incident_accepts_nothing_further(calls, monkeypatch):
    monkeypatch.setattr(
        main.state, "find_incident_by_space", lambda s: dict(INCIDENT, state="RESOLVED")
    )
    _classifies_as(monkeypatch, "EMPLOYEE_JUSTIFICATION")
    main._handle_chat("pm-9", event("priya@co.com", "actually wait"), ATTRS)
    assert calls.justifications == []


def test_a_message_in_an_unknown_space_is_ignored(calls, monkeypatch):
    monkeypatch.setattr(main.state, "find_incident_by_space", lambda s: None)
    _classifies_as(monkeypatch, "CLOSURE_COMMAND")
    main._handle_chat("pm-10", event("dan@co.com", "resolved"), ATTRS)
    assert calls.closures == []


def test_a_redelivered_message_is_processed_once(calls, monkeypatch):
    monkeypatch.setattr(main.state, "claim_event", lambda key: False)
    _classifies_as(monkeypatch, "CLOSURE_COMMAND")
    main._handle_chat("pm-11", event("dan@co.com", "resolved"), ATTRS)
    assert calls.closures == []


def test_non_message_events_are_ignored(calls, monkeypatch):
    _classifies_as(monkeypatch, "CLOSURE_COMMAND")
    main._handle_chat(
        "pm-12", event("dan@co.com", "resolved"),
        {"ce-type": "google.workspace.chat.membership.v1.updated"},
    )
    assert calls.closures == []
