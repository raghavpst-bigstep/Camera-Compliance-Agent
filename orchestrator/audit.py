"""The HR audit log: one append-only row per closed incident.

Screenshots are never written here. Whether one was attached to the original
report is recorded as a yes/no, because that is evidence metadata rather than
the evidence itself.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from config import AUDIT_SHEET_ID, AUDIT_SHEET_RANGE, SHEETS_SCOPE
from gcp_auth import build_service, default_credentials

log = logging.getLogger(__name__)

HEADERS = [
    "Incident ID",
    "Employee",
    "Manager",
    "Meeting/type",
    "Meeting date",
    "Reported on",
    "Chat space link",
    "Justification text",
    "Reminders sent",
    "Reviewed by",
    "Outcome",
    "Resolved on",
    "Closed by",
    "Screenshot attached",
]

_service = None


def _sheets():
    global _service
    if _service is None:
        _service = build_service(
            "sheets", "v4", default_credentials(scopes=[SHEETS_SCOPE])
        )
    return _service


def _person(name: str, email: str) -> str:
    """"Name <email>", or whichever half exists, never a bare "<>"."""
    name, email = (name or "").strip(), (email or "").strip()
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else ""


def _row(incident: dict[str, Any]) -> list[str]:
    return [
        incident.get("incident_id", ""),
        _person(incident.get("employee_name", ""), incident.get("employee_email", "")),
        _person(incident.get("manager_name", ""), incident.get("manager_email", "")),
        incident.get("meeting", ""),
        incident.get("meeting_date", ""),
        _iso(incident.get("reported_on")),
        incident.get("space_url", ""),
        incident.get("justification_text", ""),
        str(incident.get("reminders_sent", 0)),
        incident.get("reviewed_by", ""),
        incident.get("outcome", ""),
        _iso(incident.get("resolved_on")),
        incident.get("closed_by", ""),
        "yes" if incident.get("screenshot_attached") else "no",
    ]


def append_incident(incident: dict[str, Any]) -> bool:
    try:
        _sheets().spreadsheets().values().append(
            spreadsheetId=AUDIT_SHEET_ID,
            range=AUDIT_SHEET_RANGE,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [_row(incident)]},
        ).execute()
        log.info("incident=%s written to audit log", incident.get("incident_id"))
        return True
    except Exception:  # noqa: BLE001 - a failed audit write must retry, not crash
        log.exception("incident=%s audit write failed", incident.get("incident_id"))
        return False
