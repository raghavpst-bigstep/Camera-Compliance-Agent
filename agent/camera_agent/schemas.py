"""Validation for what the model returns.

ADK cannot enforce an ``output_schema`` on an agent that also has tools, so the
contract is enforced here instead: the model is told to return JSON, and every
response is parsed and validated before the orchestrator ever sees it.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class Person(BaseModel):
    name: str = ""
    email: str = ""

    def is_resolved(self) -> bool:
        return bool(self.name and self.email)


class IntakeResult(BaseModel):
    status: Literal["OK", "CLARIFY"]
    employee: Person = Field(default_factory=Person)
    manager: Person = Field(default_factory=Person)
    hr: Person = Field(default_factory=Person)
    meeting: str = ""
    meeting_date: str = ""
    issue: str = ""
    screenshot_attached: bool = False
    message_to_post: str = ""
    missing: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_contract(self) -> "IntakeResult":
        """Downgrade to CLARIFY rather than let a half-resolved OK through."""
        if self.status != "OK":
            self.message_to_post = ""
            if not self.missing:
                self.missing = ["unspecified"]
            return self

        gaps: list[str] = []
        if not self.employee.is_resolved():
            gaps.append("employee")
        if not self.manager.is_resolved():
            gaps.append("manager")
        if not self.hr.is_resolved():
            gaps.append("hr")
        if not self.meeting_date:
            gaps.append("meeting_date")
        if not self.message_to_post.strip():
            gaps.append("message_to_post")

        if gaps:
            self.status = "CLARIFY"
            self.missing = sorted(set(self.missing) | set(gaps))
            self.message_to_post = ""
        else:
            self.missing = []
        return self


class MessageResult(BaseModel):
    classification: Literal[
        "EMPLOYEE_JUSTIFICATION",
        "FOLLOWUP_QUESTION",
        "CLOSURE_COMMAND",
        "OTHER",
    ]
    sender_role: Literal["employee", "manager", "HR", "unknown"] = "unknown"


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Tolerates code fences and stray prose around the object, because a
    malformed wrapper is not a reason to fail an HR incident.
    """
    if not text or not text.strip():
        raise ValueError("model returned an empty response")

    candidate = _FENCE.sub("", text.strip())
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object found in model response: {text[:400]!r}")
    return json.loads(candidate[start : end + 1])
