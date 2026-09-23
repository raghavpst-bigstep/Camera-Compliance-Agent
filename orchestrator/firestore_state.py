"""Incident state, in Firestore.

The orchestrator owns state; the agent never writes it. States:

    AWAITING_JUSTIFICATION  space opened, employee asked, clock running
    JUSTIFICATION_RECEIVED  employee replied, waiting on a human
    AWAITING_FOLLOWUP       manager/HR asked a follow-up, clock running again
    RESOLVED                a manager or HR explicitly closed it

Only AWAITING_* states carry a ``due_at`` and are eligible for reminders.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from google.cloud import firestore

from config import FIRESTORE_DATABASE, PROJECT_ID, RESPONSE_PERIOD_MINUTES

log = logging.getLogger(__name__)

INCIDENTS = "incidents"
PROCESSED = "processed_events"
SYSTEM = "system_state"
FAILED_INTAKES = "failed_intakes"

# After this many failed attempts a report is left for a human rather than
# retried forever.
MAX_INTAKE_ATTEMPTS = 4

AWAITING_JUSTIFICATION = "AWAITING_JUSTIFICATION"
JUSTIFICATION_RECEIVED = "JUSTIFICATION_RECEIVED"
AWAITING_FOLLOWUP = "AWAITING_FOLLOWUP"
RESOLVED = "RESOLVED"

AWAITING_STATES = (AWAITING_JUSTIFICATION, AWAITING_FOLLOWUP)
OPEN_STATES = (AWAITING_JUSTIFICATION, JUSTIFICATION_RECEIVED, AWAITING_FOLLOWUP)

_client: firestore.Client | None = None


def client() -> firestore.Client:
    global _client
    if _client is None:
        _client = firestore.Client(project=PROJECT_ID, database=FIRESTORE_DATABASE)
    return _client


def now() -> datetime:
    return datetime.now(timezone.utc)


def due_from_now(minutes: int | None = None) -> datetime:
    return now() + timedelta(
        minutes=minutes if minutes is not None else RESPONSE_PERIOD_MINUTES
    )


# --------------------------------------------------------------------------
# Idempotency. Pub/Sub delivers at least once, so every handler checks first.
# --------------------------------------------------------------------------


def claim_event(event_key: str) -> bool:
    """Return True the first time an event key is seen, False on redelivery."""
    ref = client().collection(PROCESSED).document(event_key)

    @firestore.transactional
    def _claim(transaction: firestore.Transaction) -> bool:
        snapshot = ref.get(transaction=transaction)
        if snapshot.exists:
            return False
        transaction.set(ref, {"claimed_at": now()})
        return True

    return _claim(client().transaction())


def release_event(event_key: str) -> None:
    """Undo a claim so the work can be retried.

    Claiming before doing the work is what stops duplicate processing, but a
    claim that outlives a failure would silently drop the work instead.
    """
    try:
        client().collection(PROCESSED).document(event_key).delete()
    except Exception:  # noqa: BLE001 - a stuck claim is retried by the sweep
        log.exception("could not release claim %s", event_key)


# --------------------------------------------------------------------------
# Intake retries. A manager's report must never disappear because of a
# transient failure, so a failed intake is parked here and retried by /sweep.
# --------------------------------------------------------------------------


def queue_failed_intake(message_id: str, error: str) -> int:
    ref = client().collection(FAILED_INTAKES).document(message_id)
    snapshot = ref.get()
    attempts = ((snapshot.to_dict() or {}).get("attempts", 0) if snapshot.exists else 0) + 1
    ref.set(
        {
            "message_id": message_id,
            "attempts": attempts,
            "last_error": error[:1500],
            "last_attempt_at": now(),
            "abandoned": attempts >= MAX_INTAKE_ATTEMPTS,
        }
    )
    return attempts


def pending_intakes(limit: int = 25) -> Iterable[dict[str, Any]]:
    query = (
        client()
        .collection(FAILED_INTAKES)
        .where(filter=firestore.FieldFilter("abandoned", "==", False))
        .limit(limit)
    )
    for snapshot in query.stream():
        yield snapshot.to_dict()


def clear_failed_intake(message_id: str) -> None:
    client().collection(FAILED_INTAKES).document(message_id).delete()


# --------------------------------------------------------------------------
# Incidents
# --------------------------------------------------------------------------


def next_incident_id() -> str:
    """Sequential, human-quotable incident id: CAM-2026-0001."""
    year = now().year
    counter_ref = client().collection(SYSTEM).document(f"incident_counter_{year}")

    @firestore.transactional
    def _increment(transaction: firestore.Transaction) -> int:
        snapshot = counter_ref.get(transaction=transaction)
        current = (snapshot.to_dict() or {}).get("value", 0) if snapshot.exists else 0
        nxt = current + 1
        transaction.set(counter_ref, {"value": nxt}, merge=True)
        return nxt

    sequence = _increment(client().transaction())
    return f"CAM-{year}-{sequence:04d}"


def create_incident(data: dict[str, Any]) -> dict[str, Any]:
    incident_id = data["incident_id"]
    record = {
        **data,
        "state": AWAITING_JUSTIFICATION,
        "reported_on": now(),
        "due_at": due_from_now(),
        "reminders_sent": 0,
        "justification_text": "",
        "reviewed_by": "",
        "outcome": "",
        "resolved_on": None,
        "closed_by": "",
        "audit_written": False,
        "updated_at": now(),
    }
    client().collection(INCIDENTS).document(incident_id).set(record)
    log.info("incident=%s created", incident_id)
    return record


def get_incident(incident_id: str) -> dict[str, Any] | None:
    snapshot = client().collection(INCIDENTS).document(incident_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def find_incident_by_space(space_name: str) -> dict[str, Any] | None:
    query = (
        client()
        .collection(INCIDENTS)
        .where(filter=firestore.FieldFilter("space_name", "==", space_name))
        .limit(1)
    )
    for snapshot in query.stream():
        return snapshot.to_dict()
    return None


def find_incident_by_source_message(message_id: str) -> dict[str, Any] | None:
    """Has this email already produced an incident?

    Guards the retry path: re-processing a report must not open a second Chat
    space for the same email.
    """
    if not message_id:
        return None
    query = (
        client()
        .collection(INCIDENTS)
        .where(filter=firestore.FieldFilter("source_message_id", "==", message_id))
        .limit(1)
    )
    for snapshot in query.stream():
        return snapshot.to_dict()
    return None


def find_open_incident_for_employee(employee_email: str) -> dict[str, Any] | None:
    """An employee has at most one open incident at a time; reuse its space."""
    query = (
        client()
        .collection(INCIDENTS)
        .where(filter=firestore.FieldFilter("employee_email", "==", employee_email))
        .where(filter=firestore.FieldFilter("state", "in", list(OPEN_STATES)))
        .limit(1)
    )
    for snapshot in query.stream():
        return snapshot.to_dict()
    return None


def update_incident(incident_id: str, fields: dict[str, Any]) -> None:
    fields = {**fields, "updated_at": now()}
    client().collection(INCIDENTS).document(incident_id).update(fields)
    log.info("incident=%s updated fields=%s", incident_id, sorted(fields))


def record_justification(incident_id: str, text: str, sender_email: str) -> None:
    incident = get_incident(incident_id) or {}
    existing = incident.get("justification_text", "")
    combined = f"{existing}\n\n[{sender_email}] {text}".strip() if existing else text
    update_incident(
        incident_id,
        {
            "state": JUSTIFICATION_RECEIVED,
            "justification_text": combined,
            "due_at": None,
            "reminders_sent": 0,
        },
    )


def record_followup(incident_id: str, asked_by: str) -> None:
    update_incident(
        incident_id,
        {
            "state": AWAITING_FOLLOWUP,
            "due_at": due_from_now(),
            "reminders_sent": 0,
            "last_followup_by": asked_by,
        },
    )


def record_closure(incident_id: str, closed_by: str, outcome_text: str) -> None:
    update_incident(
        incident_id,
        {
            "state": RESOLVED,
            "closed_by": closed_by,
            "reviewed_by": closed_by,
            "outcome": outcome_text,
            "resolved_on": now(),
            "due_at": None,
        },
    )


def overdue_incidents(limit: int = 100) -> Iterable[dict[str, Any]]:
    query = (
        client()
        .collection(INCIDENTS)
        .where(filter=firestore.FieldFilter("state", "in", list(AWAITING_STATES)))
        .where(filter=firestore.FieldFilter("due_at", "<=", now()))
        .limit(limit)
    )
    for snapshot in query.stream():
        yield snapshot.to_dict()


def open_incidents(limit: int = 200) -> Iterable[dict[str, Any]]:
    """Every incident that is not yet resolved."""
    query = (
        client()
        .collection(INCIDENTS)
        .where(filter=firestore.FieldFilter("state", "in", list(OPEN_STATES)))
        .limit(limit)
    )
    for snapshot in query.stream():
        yield snapshot.to_dict()


def incidents_needing_audit(limit: int = 100) -> Iterable[dict[str, Any]]:
    query = (
        client()
        .collection(INCIDENTS)
        .where(filter=firestore.FieldFilter("state", "==", RESOLVED))
        .where(filter=firestore.FieldFilter("audit_written", "==", False))
        .limit(limit)
    )
    for snapshot in query.stream():
        yield snapshot.to_dict()


def record_reminder(incident_id: str, reminders_sent: int) -> None:
    update_incident(
        incident_id,
        {"reminders_sent": reminders_sent + 1, "due_at": due_from_now()},
    )


def stop_reminding(incident_id: str) -> None:
    """Reminder budget exhausted: leave the state, drop the clock."""
    update_incident(incident_id, {"due_at": None, "reminder_budget_exhausted": True})


# --------------------------------------------------------------------------
# Gmail watch bookkeeping
# --------------------------------------------------------------------------

_WATCH_DOC = "gmail_watch"


def get_watch_state() -> dict[str, Any]:
    snapshot = client().collection(SYSTEM).document(_WATCH_DOC).get()
    return snapshot.to_dict() or {} if snapshot.exists else {}


def set_watch_state(fields: dict[str, Any]) -> None:
    client().collection(SYSTEM).document(_WATCH_DOC).set(fields, merge=True)
