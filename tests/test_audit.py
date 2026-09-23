"""The audit row.

This is the record HR keeps. A column added to the header but not to the row
(or vice versa) would shift every value one cell left from that point on, and
nothing would fail loudly, so the two are pinned to each other here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import audit

RESOLVED_INCIDENT = {
    "incident_id": "CAM-2026-0001",
    "employee_name": "Priya Sharma",
    "employee_email": "priya@co.com",
    "manager_name": "Dan Ortiz",
    "manager_email": "dan@co.com",
    "meeting": "Daily standup",
    "meeting_date": "2026-09-23",
    "reported_on": datetime(2026, 9, 23, 9, 30, tzinfo=timezone.utc),
    "space_url": "https://chat.google.com/room/AAA",
    "justification_text": "Laptop camera stopped responding.",
    "reminders_sent": 2,
    "reviewed_by": "dan@co.com",
    "outcome": "resolved",
    "resolved_on": datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc),
    "closed_by": "dan@co.com",
    "screenshot_attached": True,
}


def test_row_width_matches_header():
    assert len(audit._row(RESOLVED_INCIDENT)) == len(audit.HEADERS)


def test_row_is_positional_not_keyed():
    """Each value must land under its own header."""
    row = dict(zip(audit.HEADERS, audit._row(RESOLVED_INCIDENT)))
    assert row["Incident ID"] == "CAM-2026-0001"
    assert row["Meeting date"] == "2026-09-23"
    assert row["Reminders sent"] == "2"
    assert row["Closed by"] == "dan@co.com"
    assert row["Chat space link"] == "https://chat.google.com/room/AAA"
    assert row["Justification text"] == "Laptop camera stopped responding."


def test_screenshot_is_recorded_as_a_flag_never_stored():
    """The spec says evidence is not retained, only that it existed."""
    row = dict(zip(audit.HEADERS, audit._row(RESOLVED_INCIDENT)))
    assert row["Screenshot attached"] == "yes"

    without = dict(RESOLVED_INCIDENT, screenshot_attached=False)
    assert dict(zip(audit.HEADERS, audit._row(without)))["Screenshot attached"] == "no"


def test_missing_incident_fields_do_not_crash_the_write():
    """A sparse record still produces a full-width row."""
    row = audit._row({"incident_id": "CAM-2026-0002"})
    assert len(row) == len(audit.HEADERS)
    assert row[0] == "CAM-2026-0002"


def test_person_formatting():
    assert audit._person("Priya", "p@co.com") == "Priya <p@co.com>"
    assert audit._person("Priya", "") == "Priya"
    assert audit._person("", "p@co.com") == "p@co.com"
    assert audit._person("", "") == ""


def test_datetimes_are_iso_not_python_repr():
    row = dict(zip(audit.HEADERS, audit._row(RESOLVED_INCIDENT)))
    assert row["Reported on"].startswith("2026-09-23T09:30")
    assert row["Resolved on"].startswith("2026-09-24T11:00")
