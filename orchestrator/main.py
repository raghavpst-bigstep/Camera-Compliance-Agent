"""Camera Compliance orchestrator.

Owns everything the agent is forbidden to do: incident state, Chat spaces,
timing, reminders, closure, and the audit log. The agent is called only to
read a manager's email and to classify a chat message.

Routes
    POST /mail               Pub/Sub push, Gmail watch notifications
    POST /chat               Pub/Sub push, Workspace Events chat messages
    POST /sweep              Cloud Scheduler, reminders + renewals + audit retries
    POST /admin/start-watch  arm the Gmail watch (first run, or after a lapse)
    GET  /healthz
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

import agent_client
import audit
import chat_client
import firestore_state as state
import gmail_client
from config import (
    AGENT_DISPLAY_NAME,
    COMPLIANCE_MAILBOX,
    LOG_LEVEL,
    MAX_REMINDERS,
    REMINDER_TEMPLATE,
    SPACE_NAME_TEMPLATE,
    WATCH_RENEW_WITHIN_DAYS,
)

logging.basicConfig(
    level=LOG_LEVEL,
    format='{"severity":"%(levelname)s","message":"%(message)s","logger":"%(name)s"}',
)
log = logging.getLogger("orchestrator")

app = FastAPI(title="Camera Compliance Orchestrator")
router = APIRouter()

SPACE_NAME_LIMIT = 120


# ---------------------------------------------------------------------------
# Pub/Sub plumbing
# ---------------------------------------------------------------------------


async def _pubsub_envelope(request: Request) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Unwrap a Pub/Sub push into (messageId, decoded data, attributes)."""
    body = await request.json()
    message = body.get("message", {}) or {}
    message_id = message.get("messageId") or message.get("message_id") or ""
    attributes = message.get("attributes", {}) or {}

    raw = message.get("data", "")
    data: dict[str, Any] = {}
    if raw:
        try:
            data = json.loads(base64.b64decode(raw).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            log.warning("pubsub message %s carried undecodable data", message_id)

    return message_id, data, attributes


def _role_for(incident: dict[str, Any], sender_email: str) -> str:
    """Decide the sender's role here, never from the model.

    Role gates what the message is allowed to do (only a manager or HR can
    close an incident), so it is an authorization decision and belongs in
    deterministic code.
    """
    sender = (sender_email or "").lower()
    if not sender:
        return "unknown"
    if sender == (incident.get("employee_email") or "").lower():
        return "employee"
    if sender == (incident.get("manager_email") or "").lower():
        return "manager"
    if sender in {
        (incident.get("hr_email") or "").lower(),
        COMPLIANCE_MAILBOX.lower(),
    }:
        return "HR"
    return "unknown"


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def _open_incident(
    message: dict[str, Any], result: dict[str, Any], request_id: str
) -> tuple[dict[str, Any] | None, bool]:
    """Turn a resolved INTAKE result into a live incident.

    Returns (incident, reused) so the caller can word the manager's
    acknowledgement correctly.
    """
    employee = result.get("employee", {})
    manager = result.get("manager", {})
    hr = result.get("hr", {})

    existing = state.find_open_incident_for_employee(employee.get("email", ""))
    if existing:
        # An employee has at most one open incident; a second report joins it
        # rather than opening a competing space.
        log.info(
            "requestId=%s reusing incident=%s for %s",
            request_id,
            existing.get("incident_id"),
            employee.get("email"),
        )
        chat_client.post_message(
            existing["space_name"],
            "A further camera compliance report was received for this "
            "employee.\n\n"
            f"Meeting: {result.get('meeting') or 'not stated'}\n"
            f"Date: {result.get('meeting_date') or 'not stated'}\n"
            f"Reported issue: {result.get('issue', '')}",
        )
        return existing, True

    incident_id = state.next_incident_id()
    display_name = SPACE_NAME_TEMPLATE.format(
        incident_id=incident_id, employee_name=employee.get("name", "")
    )[:SPACE_NAME_LIMIT]

    space = chat_client.create_space(display_name)
    space_name = space["name"]

    membership = chat_client.add_members(
        space_name,
        [employee.get("email", ""), manager.get("email", ""), hr.get("email", "")],
    )
    if membership["failed"]:
        log.error(
            "requestId=%s incident=%s could not add members: %s",
            request_id,
            incident_id,
            membership["failed"],
        )

    subscription_name = chat_client.subscribe_to_space(space_name)

    record = state.create_incident(
        {
            "incident_id": incident_id,
            "employee_name": employee.get("name", ""),
            "employee_email": employee.get("email", ""),
            "manager_name": manager.get("name", ""),
            "manager_email": manager.get("email", ""),
            "hr_name": hr.get("name", ""),
            "hr_email": hr.get("email", ""),
            "meeting": result.get("meeting", ""),
            "meeting_date": result.get("meeting_date", ""),
            "issue": result.get("issue", ""),
            "screenshot_attached": bool(result.get("screenshot_attached")),
            "space_name": space_name,
            "space_url": chat_client.space_url(space_name),
            "subscription_name": subscription_name or "",
            "source_message_id": message.get("message_id", ""),
            "reported_by": message.get("sender", ""),
            "members_failed": membership["failed"],
        }
    )

    header = (
        f"Camera compliance incident {incident_id}\n\n"
        f"Reported by: {manager.get('name') or message.get('sender', '')}\n"
        f"Meeting: {result.get('meeting') or 'not stated'}\n"
        f"Date: {result.get('meeting_date') or 'not stated'}\n"
        f"Reported issue: {result.get('issue', '')}"
    )
    chat_client.post_message(space_name, header)
    chat_client.post_message(space_name, result.get("message_to_post", ""))

    log.info(
        "requestId=%s incident=%s opened space=%s",
        request_id,
        incident_id,
        space_name,
    )
    return record, False


def _acknowledge_manager(
    message: dict[str, Any], incident: dict[str, Any], reused: bool
) -> None:
    """Reply in the manager's own thread as soon as the report is recorded.

    Deliberately states what happens next without hinting at an outcome - the
    justification is reviewed by a human, not by this system.
    """
    incident_id = incident.get("incident_id", "")
    opening = (
        f"Thank you. This has been added to the open incident {incident_id} "
        f"for {incident.get('employee_name', 'this employee')}."
        if reused
        else f"Thank you. Your report has been recorded as incident {incident_id}."
    )

    body = (
        f"{opening}\n\n"
        f"Employee: {incident.get('employee_name', '')}\n"
        f"Meeting:  {incident.get('meeting') or 'not stated'}\n"
        f"Date:     {incident.get('meeting_date') or 'not stated'}\n\n"
        "A Google Chat space has been created with the employee, you, and HR. "
        "The employee has been asked to provide their justification there, and "
        "will be reminded automatically if they do not respond.\n\n"
        f"{incident.get('space_url', '')}\n\n"
        "You or HR will review the response and decide the outcome. Reply "
        "'resolved' in the Chat space to close the incident.\n\n"
        f"- {AGENT_DISPLAY_NAME}"
    )
    gmail_client.send_reply(message, body)


def _ask_manager_for_detail(message: dict[str, Any], result: dict[str, Any], request_id: str) -> None:
    """CLARIFY: go back to the reporting manager by email, open nothing."""
    missing = result.get("missing", [])
    readable = ", ".join(missing) if missing else "unspecified details"
    body = (
        "Thanks for the report. It could not be processed yet because the "
        f"following could not be confirmed: {readable}.\n\n"
        "Please reply with the missing details (the employee's full name or "
        "work email, and the meeting name and date) and the incident will be "
        "opened.\n\nNo incident has been created and no one has been "
        "contacted."
    )
    gmail_client.send_reply(message, body)
    log.info("requestId=%s clarification requested: %s", request_id, readable)


def _process_email(message: dict[str, Any], request_id: str) -> None:
    result = agent_client.invoke(
        {
            "mode": "INTAKE",
            "raw_text": message["raw_text"],
            "reported_by": message["sender"],
            "received_at": message["received_at"],
            "screenshot_attached": message["screenshot_attached"],
        },
        request_id,
    )

    if result.get("status") != "OK":
        _ask_manager_for_detail(message, result, request_id)
        return

    incident, reused = _open_incident(message, result, request_id)
    if incident:
        # Acknowledged after the space exists, so the reply can carry its link
        # and never promises something that failed to happen.
        _acknowledge_manager(message, incident, reused)


def _intake_one(gmail_message_id: str, request_id: str) -> None:
    """Fetch one email and turn it into an incident. Raises on failure."""
    if state.find_incident_by_source_message(gmail_message_id):
        log.info(
            "requestId=%s gmail message %s already has an incident",
            request_id,
            gmail_message_id,
        )
        return

    message = gmail_client.get_message(gmail_message_id)
    if not message or not message.get("raw_text"):
        log.info("requestId=%s gmail message %s has no body", request_id, gmail_message_id)
        return
    if message["sender"] == COMPLIANCE_MAILBOX.lower():
        return  # our own clarification replies

    _process_email(message, request_id)


@router.post("/mail")
async def mail(request: Request) -> Response:
    """Parse the envelope on the event loop, then hand off.

    Everything below this point is blocking Google client work. Running it
    inline would stall the event loop for every other request this instance is
    serving, so it goes to the threadpool.
    """
    message_id, data, _ = await _pubsub_envelope(request)
    return await run_in_threadpool(_handle_mail, message_id, data)


def _handle_mail(message_id: str, data: dict[str, Any]) -> Response:
    request_id = message_id or uuid.uuid4().hex

    if message_id and not state.claim_event(f"pubsub:{message_id}"):
        log.info("requestId=%s duplicate mail notification ignored", request_id)
        return Response(status_code=204)

    watch = state.get_watch_state()
    stored_history_id = watch.get("history_id")
    incoming_history_id = str(data.get("historyId", "")) or None

    if not stored_history_id:
        # First notification after the watch was armed: nothing to back-fill,
        # just take the cursor.
        state.set_watch_state({"history_id": incoming_history_id})
        log.info("requestId=%s stored initial historyId=%s", request_id, incoming_history_id)
        return Response(status_code=204)

    message_ids, new_history_id = gmail_client.messages_since(stored_history_id)

    if new_history_id is None:
        # Cursor expired. Re-arm and skip the gap rather than replay the inbox.
        armed = gmail_client.start_watch()
        state.set_watch_state(
            {
                "history_id": str(armed.get("historyId", "")),
                "expiration_ms": int(armed.get("expiration", 0)),
                "re_armed_at": state.now(),
            }
        )
        log.warning("requestId=%s history cursor expired; watch re-armed", request_id)
        return Response(status_code=204)

    for gmail_message_id in message_ids:
        if not state.claim_event(f"gmail:{gmail_message_id}"):
            continue
        try:
            _intake_one(gmail_message_id, request_id)
        except Exception as exc:  # noqa: BLE001 - one bad email must not stall the cursor
            # The claim is released and the report parked, because the cursor
            # is about to move past this message. Without this the report
            # would be lost with no trace and no retry.
            state.release_event(f"gmail:{gmail_message_id}")
            attempts = state.queue_failed_intake(gmail_message_id, repr(exc))
            log.exception(
                "requestId=%s intake failed for gmail message %s (attempt %d)",
                request_id,
                gmail_message_id,
                attempts,
            )

    state.set_watch_state({"history_id": str(new_history_id or stored_history_id)})
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Chat replies
# ---------------------------------------------------------------------------


def _close_incident(incident: dict[str, Any], event: dict[str, Any], role: str) -> None:
    incident_id = incident["incident_id"]
    state.record_closure(incident_id, event["sender_email"], event["text"])

    if incident.get("subscription_name"):
        chat_client.delete_subscription(incident["subscription_name"])

    chat_client.post_message(
        incident["space_name"],
        f"Incident {incident_id} has been marked resolved by "
        f"{event.get('sender_display_name') or event['sender_email']} ({role}). "
        "It has been recorded in the HR audit log and no further reminders "
        "will be sent.",
    )

    closed = state.get_incident(incident_id) or {}
    if audit.append_incident(closed):
        state.update_incident(incident_id, {"audit_written": True})


@router.post("/chat")
async def chat(request: Request) -> Response:
    message_id, data, attributes = await _pubsub_envelope(request)
    return await run_in_threadpool(_handle_chat, message_id, data, attributes)


def _handle_chat(
    message_id: str, data: dict[str, Any], attributes: dict[str, str]
) -> Response:
    request_id = message_id or uuid.uuid4().hex

    if message_id and not state.claim_event(f"pubsub:{message_id}"):
        return Response(status_code=204)

    event = chat_client.parse_chat_event(data, attributes)
    if not event:
        return Response(status_code=204)

    if event["message_name"] and not state.claim_event(f"chat:{event['message_name']}"):
        return Response(status_code=204)

    incident = state.find_incident_by_space(event["space_name"])
    if not incident:
        log.info("requestId=%s message in unknown space %s", request_id, event["space_name"])
        return Response(status_code=204)

    if incident.get("state") == state.RESOLVED:
        return Response(status_code=204)

    # Computed here, not taken from the model: it decides what the message is
    # allowed to do.
    role = _role_for(incident, event["sender_email"])
    incident_id = incident["incident_id"]

    result = agent_client.invoke(
        {
            "mode": "MESSAGE",
            "message_text": event["text"],
            "sender_email": event["sender_email"],
            "sender_role": role,
            "incident_state": incident.get("state", ""),
        },
        request_id,
    )
    classification = result.get("classification", "OTHER")
    log.info(
        "requestId=%s incident=%s role=%s classification=%s",
        request_id,
        incident_id,
        role,
        classification,
    )

    if classification == "EMPLOYEE_JUSTIFICATION" and role == "employee":
        state.record_justification(incident_id, event["text"], event["sender_email"])
        chat_client.post_message(
            incident["space_name"],
            "Response recorded. The manager or HR will review it and decide "
            "the outcome.",
        )
    elif classification == "FOLLOWUP_QUESTION" and role in ("manager", "HR"):
        state.record_followup(incident_id, event["sender_email"])
    elif classification == "CLOSURE_COMMAND" and role in ("manager", "HR"):
        _close_incident(incident, event, role)
    else:
        log.info(
            "requestId=%s incident=%s no action for %s from %s",
            request_id,
            incident_id,
            classification,
            role,
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Scheduled sweep
# ---------------------------------------------------------------------------


def _renew_watch_if_needed() -> dict[str, Any]:
    watch = state.get_watch_state()
    expiration_ms = int(watch.get("expiration_ms") or 0)
    renew_at = datetime.now(timezone.utc) + timedelta(days=WATCH_RENEW_WITHIN_DAYS)

    if expiration_ms and datetime.fromtimestamp(
        expiration_ms / 1000, tz=timezone.utc
    ) > renew_at:
        return {"renewed": False}

    armed = gmail_client.start_watch()
    fields: dict[str, Any] = {"expiration_ms": int(armed.get("expiration", 0))}
    if not watch.get("history_id"):
        fields["history_id"] = str(armed.get("historyId", ""))
    state.set_watch_state(fields)
    return {"renewed": True}


def _send_reminders() -> dict[str, int]:
    sent = 0
    exhausted = 0

    for incident in state.overdue_incidents():
        incident_id = incident["incident_id"]
        reminders_sent = int(incident.get("reminders_sent", 0))

        if reminders_sent >= MAX_REMINDERS:
            state.stop_reminding(incident_id)
            exhausted += 1
            log.warning(
                "incident=%s reminder budget exhausted after %d reminders",
                incident_id,
                reminders_sent,
            )
            continue

        waiting_on = incident.get("employee_name") or "the employee"
        text = (
            f"{REMINDER_TEMPLATE}\n\n"
            f"Incident {incident_id} - awaiting a response from {waiting_on}."
        )
        try:
            chat_client.post_message(incident["space_name"], text)
            state.record_reminder(incident_id, reminders_sent)
            sent += 1
        except Exception:  # noqa: BLE001
            log.exception("incident=%s reminder failed", incident_id)

    return {"reminders_sent": sent, "budget_exhausted": exhausted}


def _renew_subscriptions() -> dict[str, int]:
    """Chat event subscriptions expire; an expired one silently stops replies."""
    renewed = 0
    resubscribed = 0

    for incident in state.open_incidents():
        subscription_name = incident.get("subscription_name") or ""
        if subscription_name and chat_client.renew_subscription(subscription_name):
            renewed += 1
            continue

        # No subscription, or the renewal failed because it had already
        # lapsed. Create a fresh one so the space is watched again.
        new_name = chat_client.subscribe_to_space(incident["space_name"])
        if new_name:
            state.update_incident(
                incident["incident_id"], {"subscription_name": new_name}
            )
            resubscribed += 1
            log.warning(
                "incident=%s subscription re-created as %s",
                incident["incident_id"],
                new_name,
            )

    return {"subscriptions_renewed": renewed, "subscriptions_recreated": resubscribed}


def _retry_failed_intakes() -> dict[str, int]:
    """Re-attempt reports that failed intake, so none is silently lost."""
    recovered = 0
    abandoned = 0

    for parked in state.pending_intakes():
        message_id = parked["message_id"]
        try:
            _intake_one(message_id, f"retry-{message_id}")
        except Exception as exc:  # noqa: BLE001
            attempts = state.queue_failed_intake(message_id, repr(exc))
            if attempts >= state.MAX_INTAKE_ATTEMPTS:
                abandoned += 1
                log.error(
                    "gmail message %s abandoned after %d intake attempts - "
                    "needs a human; last error: %r",
                    message_id,
                    attempts,
                    exc,
                )
            continue

        state.clear_failed_intake(message_id)
        recovered += 1
        log.info("gmail message %s recovered on retry", message_id)

    return {"intakes_recovered": recovered, "intakes_abandoned": abandoned}


def _retry_audit_writes() -> int:
    written = 0
    for incident in state.incidents_needing_audit():
        if audit.append_incident(incident):
            state.update_incident(incident["incident_id"], {"audit_written": True})
            written += 1
    return written


@router.post("/sweep")
def sweep() -> dict[str, Any]:
    summary: dict[str, Any] = {}
    try:
        summary["watch"] = _renew_watch_if_needed()
    except Exception:  # noqa: BLE001 - each stage is independent
        log.exception("watch renewal failed")
        summary["watch"] = {"error": True}

    try:
        summary.update(_send_reminders())
    except Exception:  # noqa: BLE001
        log.exception("reminder sweep failed")
        summary["reminders_error"] = True

    try:
        summary.update(_retry_failed_intakes())
    except Exception:  # noqa: BLE001
        log.exception("intake retry failed")
        summary["intake_retry_error"] = True

    try:
        summary.update(_renew_subscriptions())
    except Exception:  # noqa: BLE001
        log.exception("subscription renewal failed")
        summary["subscription_error"] = True

    try:
        summary["audit_rows_written"] = _retry_audit_writes()
    except Exception:  # noqa: BLE001
        log.exception("audit retry failed")
        summary["audit_error"] = True

    log.info("sweep complete %s", summary)
    return summary


@router.post("/admin/start-watch")
def start_watch() -> dict[str, Any]:
    """Arm the Gmail watch and store its cursor. Safe to re-run."""
    armed = gmail_client.start_watch()
    state.set_watch_state(
        {
            "history_id": str(armed.get("historyId", "")),
            "expiration_ms": int(armed.get("expiration", 0)),
            "armed_at": state.now(),
        }
    )
    return {
        "history_id": armed.get("historyId"),
        "expiration_ms": armed.get("expiration"),
        "mailbox": COMPLIANCE_MAILBOX,
    }


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(router)
