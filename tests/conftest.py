"""Test setup: both services on the path, Google clients stubbed.

Nothing here talks to Google. These tests cover the logic that decides what
happens to a real person's incident, which is exactly the part that should not
need a live project to verify.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "orchestrator"))

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("COMPLIANCE_MAILBOX", "hr-compliance@co.com")
os.environ.setdefault("SERVICE_ACCOUNT_EMAIL", "sa@test-project.iam.gserviceaccount.com")
os.environ.setdefault("AGENT_URL", "https://agent.example")
os.environ.setdefault("AUDIT_SHEET_ID", "audit-sheet")
os.environ.setdefault("CHAT_EVENTS_TOPIC", "projects/test-project/topics/chat-events")
os.environ.setdefault("GMAIL_TOPIC", "projects/test-project/topics/incoming-mail")
os.environ.setdefault("EMPLOYEE_SHEET_ID", "employee-sheet")


def _stub(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_stub("google")
_stub("google.auth", default=lambda scopes=None: (None, "test-project"))
_stub("google.auth.transport")
_stub("google.auth.transport.requests", Request=object)
_stub("google.oauth2")
_stub("google.oauth2.credentials", Credentials=object)
_stub("google.oauth2.id_token", fetch_id_token=lambda *args: None)
_stub("google.cloud")
_stub(
    "google.cloud.firestore",
    Client=object,
    FieldFilter=object,
    Transaction=object,
    transactional=lambda fn: fn,
)
_stub("googleapiclient")
_stub("googleapiclient.discovery", build=lambda *args, **kwargs: None)
_stub("googleapiclient.errors", HttpError=type("HttpError", (Exception,), {}))
