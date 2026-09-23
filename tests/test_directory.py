"""Directory matching.

The agent is told never to guess who an employee is. That only holds if the
lookup is honest about ambiguity, so these tests pin the ambiguous and
not-found paths as tightly as the happy one.
"""

from __future__ import annotations

import pytest

from camera_agent import directory

ROWS = [
    {
        "employee_name": "Priya Sharma",
        "employee_email": "priya.sharma@co.com",
        "manager_name": "Dan Ortiz",
        "manager_email": "dan@co.com",
        "hr_name": "Asha Rao",
        "hr_email": "hr@co.com",
        "department": "Design",
    },
    {
        "employee_name": "Priya Menon",
        "employee_email": "priya.menon@co.com",
        "manager_name": "Lena Fox",
        "manager_email": "lena@co.com",
        "hr_name": "Asha Rao",
        "hr_email": "hr@co.com",
        "department": "Engineering",
    },
    {
        "employee_name": "Tom Baker",
        "employee_email": "tom@co.com",
        "manager_name": "Dan Ortiz",
        "manager_email": "dan@co.com",
        "hr_name": "Asha Rao",
        "hr_email": "hr@co.com",
        "department": "Sales",
    },
]


@pytest.fixture(autouse=True)
def loaded_directory(monkeypatch):
    class FakeCache:
        def rows(self):
            return ROWS

    monkeypatch.setattr(directory, "_cache", FakeCache())


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Employee Name", "employee_name"),
        ("employee  name", "employee_name"),
        ("Work Email", "employee_email"),
        ("Reporting Manager", "manager_name"),
        ("HR Contact Email", "hr_email"),
        ("Team", "department"),
        ("Random Column", None),
    ],
)
def test_header_aliases(header, expected):
    assert directory._canonical_column(header) == expected


def test_exact_email_is_certain():
    result = directory.lookup_directory("priya.sharma@co.com")
    top = result["candidates"][0]
    assert top["employee_name"] == "Priya Sharma"
    assert top["confidence"] == 1.0


def test_full_name_inside_a_sentence_matches():
    result = directory.lookup_directory("Tom Baker had his camera off again")
    assert result["candidates"][0]["employee_name"] == "Tom Baker"


def test_bare_first_name_is_reported_as_ambiguous():
    """Two Priyas. The tool must not silently pick one."""
    result = directory.lookup_directory("Priya")
    assert len(result["candidates"]) >= 2
    assert "ambiguous" in result["note"].lower()


def test_no_match_returns_empty_with_guidance():
    result = directory.lookup_directory("Nobody Here")
    assert result["match_count"] == 0
    assert result["candidates"] == []
    assert "No directory record" in result["note"]


def test_empty_hint_matches_nothing():
    assert directory.lookup_directory("")["match_count"] == 0


def test_manager_and_hr_ride_along_with_the_employee():
    top = directory.lookup_directory("Priya Sharma")["candidates"][0]
    assert top["manager_email"] == "dan@co.com"
    assert top["hr_email"] == "hr@co.com"


def test_unreadable_sheet_is_reported_not_raised(monkeypatch):
    """A broken sheet should become a CLARIFY, not a 500."""

    class BrokenCache:
        def rows(self):
            raise RuntimeError("sheet unreachable")

    monkeypatch.setattr(directory, "_cache", BrokenCache())
    result = directory.lookup_directory("anyone")
    assert result["match_count"] == 0
    assert "could not be read" in result["note"]
