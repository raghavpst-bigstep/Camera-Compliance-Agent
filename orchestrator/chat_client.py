"""Google Chat incident spaces, and the event subscriptions that watch them.

Every Chat action is performed by the Chat app itself, not by a borrowed human
identity. The service account running this service is the app configured on
the project, so it authenticates directly with its own credentials and posts
under the app's own name and avatar.

That keeps each HR agent a distinct identity in Chat, rather than all of them
appearing as the shared automation mailbox, and it means Chat needs no
domain-wide delegation - only Gmail does.

It does require, in the Workspace admin console, that Chat apps be allowed to
create spaces and add members. Without it, space creation returns 403.
"""

from __future__ import annotations

import logging
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    CHAT_APP_SCOPES,
    CHAT_EVENTS_TOPIC,
    EVENT_SUBSCRIPTION_TTL_HOURS,
)
from gcp_auth import default_credentials

log = logging.getLogger(__name__)

MESSAGE_CREATED = "google.workspace.chat.message.v1.created"


def _credentials():
    """The app's own identity. No impersonation, no delegation."""
    return default_credentials(scopes=list(CHAT_APP_SCOPES))


def _chat():
    return build("chat", "v1", credentials=_credentials(), cache_discovery=False)


def _events():
    return build(
        "workspaceevents", "v1", credentials=_credentials(), cache_discovery=False
    )


def _unique(emails: list[str]) -> list[str]:
    """De-duplicate case-insensitively, preserving order, dropping blanks."""
    seen: set[str] = set()
    ordered: list[str] = []
    for email in emails:
        key = (email or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(email.strip())
    return ordered


def space_url(space_name: str) -> str:
    """Deep link a human can click, derived from ``spaces/AAAA``."""
    space_id = space_name.split("/", 1)[-1]
    return f"https://chat.google.com/room/{space_id}"


def create_space(display_name: str) -> dict[str, Any]:
    space = (
        _chat()
        .spaces()
        .create(
            body={
                "spaceType": "SPACE",
                "displayName": display_name,
                "spaceDetails": {
                    "description": "HR camera compliance incident",
                },
            }
        )
        .execute()
    )
    log.info("created space %s", space.get("name"))
    return space


def add_members(space_name: str, emails: list[str]) -> dict[str, list[str]]:
    """Add each email to the space. Returns which ones landed and which failed."""
    added: list[str] = []
    failed: list[str] = []
    service = _chat()

    # The app created the space, so every human named here still has to be
    # invited - there is no implicit creator to skip.
    for email in _unique(emails):
        try:
            service.spaces().members().create(
                parent=space_name,
                body={"member": {"name": f"users/{email}", "type": "HUMAN"}},
            ).execute()
            added.append(email)
        except HttpError as exc:
            if exc.resp.status == 409:  # already a member
                added.append(email)
                continue
            log.warning("could not add %s to %s: %s", email, space_name, exc)
            failed.append(email)

    return {"added": added, "failed": failed}


def post_message(space_name: str, text: str) -> dict[str, Any]:
    message = (
        _chat()
        .spaces()
        .messages()
        .create(parent=space_name, body={"text": text})
        .execute()
    )
    log.info("posted message %s", message.get("name"))
    return message


def _subscription_body(space_name: str, with_ttl: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "targetResource": f"//chat.googleapis.com/{space_name}",
        "eventTypes": [MESSAGE_CREATED],
        "notificationEndpoint": {"pubsubTopic": CHAT_EVENTS_TOPIC},
        "payloadOptions": {"includeResource": True},
    }
    if with_ttl:
        body["ttl"] = {"seconds": EVENT_SUBSCRIPTION_TTL_HOURS * 3600}
    return body


def subscribe_to_space(space_name: str) -> str | None:
    """Subscribe to new messages in a space. Returns the subscription name.

    The maximum TTL a tenant accepts varies, so a rejected TTL falls back to
    the server default rather than leaving the space unwatched.
    """
    for with_ttl in (True, False):
        try:
            subscription = (
                _events()
                .subscriptions()
                .create(body=_subscription_body(space_name, with_ttl))
                .execute()
            )
            name = subscription.get("name") or subscription.get("response", {}).get(
                "name"
            )
            log.info("subscribed to %s as %s (ttl=%s)", space_name, name, with_ttl)
            return name
        except HttpError as exc:
            if with_ttl and exc.resp.status == 400:
                log.warning(
                    "subscription TTL rejected for %s, retrying with the "
                    "server default: %s",
                    space_name,
                    exc,
                )
                continue
            log.error("could not subscribe to %s: %s", space_name, exc)
            return None
    return None


def renew_subscription(subscription_name: str) -> bool:
    try:
        _events().subscriptions().patch(
            name=subscription_name,
            updateMask="ttl",
            body={"ttl": {"seconds": EVENT_SUBSCRIPTION_TTL_HOURS * 3600}},
        ).execute()
        return True
    except HttpError as exc:
        log.warning("could not renew subscription %s: %s", subscription_name, exc)
        return False


def delete_subscription(subscription_name: str) -> bool:
    try:
        _events().subscriptions().delete(name=subscription_name).execute()
        log.info("deleted subscription %s", subscription_name)
        return True
    except HttpError as exc:
        if exc.resp.status == 404:
            return True
        log.warning("could not delete subscription %s: %s", subscription_name, exc)
        return False


def parse_chat_event(envelope_data: dict[str, Any], attributes: dict[str, str]) -> dict[str, Any] | None:
    """Flatten a Workspace Events message-created payload.

    Returns None for any event that is not a new message, and for messages the
    HR mailbox itself posted (otherwise the agent would react to its own
    prompts and reminders).
    """
    event_type = attributes.get("ce-type") or envelope_data.get("eventType", "")
    if event_type != MESSAGE_CREATED:
        return None

    message = envelope_data.get("message") or {}
    if not message:
        return None

    space_name = (message.get("space") or {}).get("name", "")
    if not space_name:
        target = envelope_data.get("targetResource", "")
        space_name = target.split("//chat.googleapis.com/", 1)[-1] if target else ""

    sender = message.get("sender") or {}
    sender_email = (sender.get("email") or "").lower()
    sender_type = sender.get("type", "")

    # Our own posts come back as BOT. Reacting to them would have the agent
    # answering its own prompts and reminders.
    if sender_type == "BOT":
        return None

    return {
        "message_name": message.get("name", ""),
        "space_name": space_name,
        "sender_email": sender_email,
        "sender_display_name": sender.get("displayName", ""),
        "text": message.get("text", "") or "",
        "create_time": message.get("createTime", ""),
    }
