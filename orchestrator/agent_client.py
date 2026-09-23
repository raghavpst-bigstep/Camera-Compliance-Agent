"""Calls the agent service.

The agent runs as a separate private Cloud Run service, so every call carries
an OIDC identity token minted for that service's URL.
"""

from __future__ import annotations

import logging
from typing import Any

import google.auth.transport.requests
import requests
from google.oauth2 import id_token

from config import AGENT_URL

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 120


def _identity_token() -> str | None:
    try:
        request = google.auth.transport.requests.Request()
        return id_token.fetch_id_token(request, AGENT_URL)
    except Exception as exc:  # noqa: BLE001 - local runs have no metadata server
        log.warning("no OIDC token available (%s); calling agent unauthenticated", exc)
        return None


def invoke(payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    """Send one invocation to the agent and return its validated result."""
    headers = {"Content-Type": "application/json", "x-request-id": request_id}
    token = _identity_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.post(
        f"{AGENT_URL}/invoke",
        json={"payload": payload, "request_id": request_id},
        headers=headers,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()

    if body.get("degraded"):
        log.warning("requestId=%s agent returned a degraded result", request_id)

    return body.get("result", {})
