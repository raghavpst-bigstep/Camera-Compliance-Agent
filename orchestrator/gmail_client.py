"""Gmail intake for the HR compliance mailbox.

Gmail's push notification carries only a historyId, never the message itself,
so every notification turns into a history walk from the last id we stored.
"""

from __future__ import annotations

import base64
import binascii
import html
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import COMPLIANCE_MAILBOX, DELEGATED_SCOPES, GMAIL_TOPIC
from gcp_auth import delegated_credentials

log = logging.getLogger(__name__)

_IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp")
_TAG = re.compile(r"<[^>]+>")


def _service():
    credentials = delegated_credentials(COMPLIANCE_MAILBOX, DELEGATED_SCOPES)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def start_watch() -> dict[str, Any]:
    """(Re-)arm the Gmail push watch. Safe to call repeatedly."""
    response = (
        _service()
        .users()
        .watch(
            userId="me",
            body={
                "topicName": GMAIL_TOPIC,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE",
            },
        )
        .execute()
    )
    expiration_ms = int(response.get("expiration", 0))
    log.info(
        "gmail watch armed historyId=%s expires=%s",
        response.get("historyId"),
        datetime.fromtimestamp(expiration_ms / 1000, tz=timezone.utc).isoformat()
        if expiration_ms
        else "unknown",
    )
    return response


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
    except (binascii.Error, ValueError):
        return ""


def _walk(part: dict[str, Any], plain: list[str], rich: list[str], has_image: list[bool]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body", {}) or {}
    filename = part.get("filename", "") or ""

    if filename and (mime in _IMAGE_TYPES or body.get("attachmentId")):
        if mime in _IMAGE_TYPES:
            has_image[0] = True

    data = body.get("data")
    if data:
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            rich.append(_decode(data))

    for child in part.get("parts", []) or []:
        _walk(child, plain, rich, has_image)


def _body_text(payload: dict[str, Any]) -> tuple[str, bool]:
    plain: list[str] = []
    rich: list[str] = []
    has_image = [False]
    _walk(payload, plain, rich, has_image)

    if plain:
        text = "\n".join(plain)
    elif rich:
        text = html.unescape(_TAG.sub(" ", "\n".join(rich)))
    else:
        text = ""
    return re.sub(r"[ \t]+", " ", text).strip(), has_image[0]


def get_message(message_id: str) -> dict[str, Any] | None:
    """Fetch one message and flatten it into what the agent needs."""
    try:
        message = (
            _service()
            .users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except HttpError as exc:
        log.warning("could not fetch gmail message %s: %s", message_id, exc)
        return None

    payload = message.get("payload", {}) or {}
    headers = {
        header.get("name", "").lower(): header.get("value", "")
        for header in payload.get("headers", []) or []
    }

    body, has_image = _body_text(payload)
    subject = headers.get("subject", "")
    _, sender = parseaddr(headers.get("from", ""))

    try:
        received_at = parsedate_to_datetime(headers["date"]).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        received_at = datetime.fromtimestamp(
            int(message.get("internalDate", "0")) / 1000, tz=timezone.utc
        )

    raw_text = f"Subject: {subject}\n\n{body}".strip() if subject else body

    return {
        "message_id": message_id,
        "thread_id": message.get("threadId", ""),
        "rfc_message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        "sender": sender.lower(),
        "subject": subject,
        "raw_text": raw_text,
        "received_at": received_at.isoformat(),
        "screenshot_attached": has_image,
    }


def messages_since(start_history_id: str) -> tuple[list[str], str | None]:
    """Return message ids added since ``start_history_id``.

    The second element is the new historyId to store, or None when Gmail has
    expired the cursor and a full re-sync is needed.
    """
    service = _service()
    message_ids: list[str] = []
    latest_history_id: str | None = None
    page_token: str | None = None

    while True:
        try:
            response = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                    pageToken=page_token,
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                # Cursor too old. Caller must re-arm the watch and skip the gap.
                log.warning("gmail historyId %s expired", start_history_id)
                return [], None
            raise

        latest_history_id = response.get("historyId", latest_history_id)
        for record in response.get("history", []) or []:
            for added in record.get("messagesAdded", []) or []:
                message = added.get("message", {}) or {}
                labels = message.get("labelIds", []) or []
                if "DRAFT" in labels or "SENT" in labels or "TRASH" in labels:
                    continue
                if message.get("id"):
                    message_ids.append(message["id"])

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    # Preserve arrival order, drop duplicates across history records.
    seen: set[str] = set()
    ordered = [mid for mid in message_ids if not (mid in seen or seen.add(mid))]
    return ordered, latest_history_id


def send_reply(original: dict[str, Any], body: str) -> bool:
    """Reply to the reporting manager, in the original thread.

    Used only for CLARIFY: the report could not be resolved, so the manager is
    asked for the missing details and no incident is opened.
    """
    recipient = original.get("sender", "")
    if not recipient:
        log.warning("cannot reply: original message has no sender")
        return False

    reply = EmailMessage()
    reply["To"] = recipient
    reply["From"] = COMPLIANCE_MAILBOX
    subject = original.get("subject", "")
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    rfc_id = original.get("rfc_message_id", "")
    if rfc_id:
        reply["In-Reply-To"] = rfc_id
        references = original.get("references", "")
        reply["References"] = f"{references} {rfc_id}".strip()

    reply.set_content(body)

    payload = {"raw": base64.urlsafe_b64encode(reply.as_bytes()).decode("utf-8")}
    if original.get("thread_id"):
        payload["threadId"] = original["thread_id"]

    try:
        _service().users().messages().send(userId="me", body=payload).execute()
        log.info("sent clarification reply to %s", recipient)
        return True
    except HttpError as exc:
        log.error("could not send clarification reply to %s: %s", recipient, exc)
        return False
