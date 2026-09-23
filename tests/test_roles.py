"""Sender role resolution.

Role decides who may close an incident, so it is an authorization check and is
computed from the incident record rather than taken from the model's output.
"""

from __future__ import annotations

import pytest

import main

INCIDENT = {
    "employee_email": "priya@co.com",
    "manager_email": "dan@co.com",
    "hr_email": "asha@co.com",
}


@pytest.mark.parametrize(
    "sender,expected",
    [
        ("priya@co.com", "employee"),
        ("PRIYA@CO.COM", "employee"),
        ("dan@co.com", "manager"),
        ("asha@co.com", "HR"),
        ("hr-compliance@co.com", "HR"),
        ("stranger@co.com", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_role_resolution(sender, expected):
    assert main._role_for(INCIDENT, sender) == expected


@pytest.mark.parametrize("sender", ["stranger@co.com", "priya@co.com", "", None])
def test_only_manager_or_hr_can_close(sender):
    assert main._role_for(INCIDENT, sender) not in ("manager", "HR")


def test_manager_can_close():
    assert main._role_for(INCIDENT, "dan@co.com") in ("manager", "HR")


def test_gaps_in_the_record_do_not_promote_a_stranger():
    sparse = {"employee_email": "priya@co.com"}
    assert main._role_for(sparse, "someone@co.com") == "unknown"


def test_empty_stored_emails_do_not_match_an_empty_sender():
    assert main._role_for({"employee_email": "", "manager_email": ""}, "") == "unknown"
