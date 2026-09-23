"""Environment configuration for the orchestrator.

Everything is read once at import so a missing variable fails the container at
startup rather than halfway through a live incident.
"""

from __future__ import annotations

import os


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


PROJECT_ID: str = _required("GOOGLE_CLOUD_PROJECT")

# The HR mailbox that receives manager reports. Gmail is watched here, and
# clarification replies are sent from here, both via domain-wide delegation.
# Chat does NOT use this identity - it runs as the Chat app itself.
COMPLIANCE_MAILBOX: str = _required("COMPLIANCE_MAILBOX")

# The service account running this container, used as the DWD issuer.
SERVICE_ACCOUNT_EMAIL: str = _required("SERVICE_ACCOUNT_EMAIL")

AGENT_URL: str = _required("AGENT_URL").rstrip("/")

AUDIT_SHEET_ID: str = _required("AUDIT_SHEET_ID")
AUDIT_SHEET_RANGE: str = os.environ.get("AUDIT_SHEET_RANGE", "Audit!A:N")

CHAT_EVENTS_TOPIC: str = _required("CHAT_EVENTS_TOPIC")

FIRESTORE_DATABASE: str = os.environ.get("FIRESTORE_DATABASE", "(default)")

# Half a day, per the workflow spec. RESPONSE_PERIOD_MINUTES overrides it when
# set, so a test run can use a 2-minute window without touching the workflow.
RESPONSE_PERIOD_HOURS: int = _int("RESPONSE_PERIOD_HOURS", 12)
RESPONSE_PERIOD_MINUTES: int = _int(
    "RESPONSE_PERIOD_MINUTES", RESPONSE_PERIOD_HOURS * 60
)
MAX_REMINDERS: int = _int("MAX_REMINDERS", 3)

# Re-arm the Gmail watch when it is within this many days of expiring.
# Gmail watches expire after 7 days.
WATCH_RENEW_WITHIN_DAYS: int = _int("WATCH_RENEW_WITHIN_DAYS", 2)
GMAIL_TOPIC: str = _required("GMAIL_TOPIC")

# Chat event subscriptions expire; the sweep renews them on the same pass.
EVENT_SUBSCRIPTION_TTL_HOURS: int = _int("EVENT_SUBSCRIPTION_TTL_HOURS", 168)

# Shown as the Chat space title. {incident_id} and {employee_name} are filled in.
SPACE_NAME_TEMPLATE: str = os.environ.get(
    "SPACE_NAME_TEMPLATE", "Camera Compliance {incident_id} - {employee_name}"
)

# Prefixed to messages the agent posts, so the HR mailbox's own display name is
# not what employees see acting on the incident.
AGENT_DISPLAY_NAME: str = os.environ.get(
    "AGENT_DISPLAY_NAME", "Camera Compliance Agent"
)

REMINDER_TEMPLATE: str = os.environ.get(
    "REMINDER_TEMPLATE",
    "Reminder: a response is still outstanding on this incident. "
    "Please reply in this space.",
)

LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# Two different authentication models are in play.
#
# Gmail has no app-authentication path: reading a person's mailbox always means
# acting as that person, so it needs domain-wide delegation. These are the only
# scopes a Workspace admin has to approve.
#
# Deliberately narrower than gmail.modify, which would also permit modifying,
# labelling and trashing mail. The agent only ever reads reports and sends
# clarification replies, so it asks for exactly those two.
DELEGATED_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)

# Chat, by contrast, runs as the app itself. The service account IS the Chat
# app configured on the project, so it authenticates directly and posts under
# the app's own name and avatar rather than borrowing a human identity.
CHAT_APP_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/chat.app.spaces.create",
    "https://www.googleapis.com/auth/chat.app.spaces",
    "https://www.googleapis.com/auth/chat.app.memberships",
)

SHEETS_SCOPE: str = "https://www.googleapis.com/auth/spreadsheets"
