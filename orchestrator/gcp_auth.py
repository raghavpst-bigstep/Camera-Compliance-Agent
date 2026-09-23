"""Credential helpers.

Two identities are in play:

* The *service account* itself, used for Firestore, Pub/Sub, the audit Sheet,
  and every Google Chat call - in Chat it is the app. Application Default
  Credentials on Cloud Run give this for free.
* The *HR compliance mailbox*, impersonated through domain-wide delegation.
  Gmail only: reading a mailbox means acting as its owner, and unlike Chat
  there is no app-authentication path.

The delegation is done keylessly: the container asks the IAM Credentials API to
sign a JWT asserting ``sub = <mailbox>``, then exchanges that assertion for an
access token. No service-account key file is ever downloaded or stored, which
is what keeps this compliant with the "no signing material in the repo" rule.

This requires the service account to hold roles/iam.serviceAccountTokenCreator
on itself.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import google.auth
import google_auth_httplib2
import httplib2
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config import SERVICE_ACCOUNT_EMAIL

log = logging.getLogger(__name__)

_CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_SKEW_SECONDS = 120

# httplib2, which the Google API client uses, has no per-request timeout.
# It has to be set on the transport or a hung upstream hangs the handler until
# Cloud Run times the request out - long enough to burn the Pub/Sub ack
# deadline and trigger a redelivery.
HTTP_TIMEOUT_SECONDS = int(os.environ.get("GOOGLE_HTTP_TIMEOUT_SECONDS", "30"))

_lock = threading.Lock()
_cache: dict[tuple[str, tuple[str, ...]], tuple[str, float]] = {}
_iam_service = None


def default_credentials(scopes: list[str] | None = None):
    """ADC for the running service account."""
    credentials, _ = google.auth.default(scopes=scopes or [_CLOUD_PLATFORM])
    return credentials


def build_service(name: str, version: str, credentials):
    """A Google API client whose transport actually times out."""
    authorized = google_auth_httplib2.AuthorizedHttp(
        credentials, http=httplib2.Http(timeout=HTTP_TIMEOUT_SECONDS)
    )
    return build(name, version, http=authorized, cache_discovery=False)


def _iam():
    global _iam_service
    if _iam_service is None:
        _iam_service = build_service("iamcredentials", "v1", default_credentials())
    return _iam_service


def _mint_token(subject: str, scopes: tuple[str, ...]) -> tuple[str, float]:
    now = int(time.time())
    claims = {
        "iss": SERVICE_ACCOUNT_EMAIL,
        "sub": subject,
        "scope": " ".join(scopes),
        "aud": _TOKEN_URL,
        "iat": now,
        "exp": now + 3600,
    }
    signed = (
        _iam()
        .projects()
        .serviceAccounts()
        .signJwt(
            name=f"projects/-/serviceAccounts/{SERVICE_ACCOUNT_EMAIL}",
            body={"payload": json.dumps(claims)},
        )
        .execute()
    )["signedJwt"]

    response = requests.post(
        _TOKEN_URL,
        data={"grant_type": _JWT_BEARER, "assertion": signed},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "domain-wide delegation token exchange failed "
            f"({response.status_code}). Check that {SERVICE_ACCOUNT_EMAIL} is "
            "authorised in the Admin console for these scopes: "
            f"{' '.join(scopes)}. Response: {response.text[:300]}"
        )

    body = response.json()
    return body["access_token"], now + int(body.get("expires_in", 3600))


def delegated_credentials(subject: str, scopes: tuple[str, ...]) -> Credentials:
    """Access-token credentials acting as ``subject`` (the HR mailbox)."""
    key = (subject, tuple(sorted(scopes)))
    with _lock:
        cached = _cache.get(key)
        if cached and cached[1] - _SKEW_SECONDS > time.time():
            return Credentials(token=cached[0])

        token, expires_at = _mint_token(subject, tuple(sorted(scopes)))
        _cache[key] = (token, expires_at)
        log.info("minted delegated token for %s", subject)
        return Credentials(token=token)
