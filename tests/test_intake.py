"""Intake orchestration: what actually happens when a manager's mail arrives.

Every Google call is recorded rather than made, so these assert the decisions —
whether a space is opened, who is invited, what the manager is told — without
touching a live project.
"""

from __future__ import annotations

import pytest

import main

MANAGER_MAIL = {
    "message_id": "msg-1",
    "sender": "dan@co.com",
    "subject": "Camera off in standup",
    "raw_text": "Priya had her camera off during this morning's standup.",
    "received_at": "2026-09-23T09:30:00Z",
    "screenshot_attached": True,
}

RESOLVED = {
    "status": "OK",
    "employee": {"name": "Priya Sharma", "email": "priya@co.com"},
    "manager": {"name": "Dan Ortiz", "email": "dan@co.com"},
    "hr": {"name": "Asha Rao", "email": "hr@co.com"},
    "meeting": "Daily standup",
    "meeting_date": "2026-09-23",
    "issue": "camera off for the whole call",
    "screenshot_attached": True,
    "message_to_post": "Please share why your camera was off.",
    "missing": [],
}


class Recorder:
    """Stands in for every outbound Google call."""

    def __init__(self):
        self.spaces_created = []
        self.members_added = []
        self.messages_posted = []
        self.subscriptions = []
        self.emails_sent = []
        self.incidents = []


@pytest.fixture
def rec(monkeypatch):
    r = Recorder()

    def create_space(display_name):
        r.spaces_created.append(display_name)
        return {"name": "spaces/TEST"}

    def add_members(space, emails):
        r.members_added.append((space, list(emails)))
        return {"added": list(emails), "failed": []}

    monkeypatch.setattr(main.chat_client, "create_space", create_space)
    monkeypatch.setattr(main.chat_client, "add_members", add_members)
    monkeypatch.setattr(
        main.chat_client, "post_message",
        lambda space, text: r.messages_posted.append((space, text)) or {"name": "m"},
    )
    monkeypatch.setattr(
        main.chat_client, "subscribe_to_space",
        lambda space: r.subscriptions.append(space) or "subscriptions/S1",
    )
    monkeypatch.setattr(main.chat_client, "space_url", lambda name: f"https://chat/{name}")
    monkeypatch.setattr(
        main.gmail_client, "send_reply",
        lambda original, body: r.emails_sent.append((original["sender"], body)) or True,
    )
    monkeypatch.setattr(main.state, "next_incident_id", lambda: "CAM-2026-0001")
    monkeypatch.setattr(main.state, "find_open_incident_for_employee", lambda email: None)

    def create_incident(data):
        record = dict(data, state="AWAITING_JUSTIFICATION")
        r.incidents.append(record)
        return record

    monkeypatch.setattr(main.state, "create_incident", create_incident)
    return r


def _agent_returns(monkeypatch, result):
    monkeypatch.setattr(main.agent_client, "invoke", lambda payload, rid: result)


def test_resolved_report_opens_one_incident(rec, monkeypatch):
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")

    assert len(rec.spaces_created) == 1
    assert rec.spaces_created[0] == "Camera Compliance CAM-2026-0001 - Priya Sharma"
    assert len(rec.incidents) == 1
    assert rec.incidents[0]["incident_id"] == "CAM-2026-0001"


def test_all_three_parties_are_invited(rec, monkeypatch):
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")

    _, invited = rec.members_added[0]
    assert set(invited) == {"priya@co.com", "dan@co.com", "hr@co.com"}


def test_the_space_is_watched_for_replies(rec, monkeypatch):
    """Without a subscription the employee's answer never comes back."""
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")

    assert rec.subscriptions == ["spaces/TEST"]
    assert rec.incidents[0]["subscription_name"] == "subscriptions/S1"


def test_the_employee_is_asked_for_a_justification(rec, monkeypatch):
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")

    posted = " ".join(text for _, text in rec.messages_posted)
    assert "CAM-2026-0001" in posted
    assert "Daily standup" in posted
    assert RESOLVED["message_to_post"] in posted


def test_manager_is_acknowledged_with_the_space_link(rec, monkeypatch):
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")

    assert len(rec.emails_sent) == 1
    recipient, body = rec.emails_sent[0]
    assert recipient == "dan@co.com"
    assert "CAM-2026-0001" in body
    assert "https://chat/spaces/TEST" in body


def test_acknowledgement_promises_no_outcome(rec, monkeypatch):
    """The agent must never imply it will judge the justification."""
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")

    body = rec.emails_sent[0][1].lower()
    for forbidden in ("valid", "acceptable", "approved", "we will decide", "justified"):
        assert forbidden not in body


def test_clarify_opens_nothing_and_contacts_nobody(rec, monkeypatch):
    """An unresolved report must not reach an employee."""
    _agent_returns(
        monkeypatch,
        {"status": "CLARIFY", "missing": ["employee_email"], "message_to_post": ""},
    )
    main._process_email(MANAGER_MAIL, "req-1")

    assert rec.spaces_created == []
    assert rec.members_added == []
    assert rec.messages_posted == []
    assert rec.incidents == []

    recipient, body = rec.emails_sent[0]
    assert recipient == "dan@co.com"
    assert "employee_email" in body
    assert "No incident has been created" in body


def test_second_report_reuses_the_open_incident(rec, monkeypatch):
    """One employee, one space — never a second space with the same people."""
    existing = {
        "incident_id": "CAM-2026-0001",
        "space_name": "spaces/TEST",
        "space_url": "https://chat/spaces/TEST",
        "employee_name": "Priya Sharma",
    }
    monkeypatch.setattr(main.state, "find_open_incident_for_employee", lambda e: existing)
    _agent_returns(monkeypatch, RESOLVED)

    main._process_email(MANAGER_MAIL, "req-2")

    assert rec.spaces_created == []
    assert rec.incidents == []
    assert len(rec.messages_posted) == 1
    assert "further camera compliance report" in rec.messages_posted[0][1]

    body = rec.emails_sent[0][1]
    assert "added to the open incident CAM-2026-0001" in body


def test_screenshot_flag_is_carried_onto_the_incident(rec, monkeypatch):
    _agent_returns(monkeypatch, RESOLVED)
    main._process_email(MANAGER_MAIL, "req-1")
    assert rec.incidents[0]["screenshot_attached"] is True
