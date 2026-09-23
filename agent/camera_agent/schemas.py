"""Validation for what the model returns.

ADK cannot enforce an ``output_schema`` on an agent that also has tools, so the
contract is enforced here instead: the model is told to return JSON, and every
response is parsed and validated before the orchestrator ever sees it.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M%z",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def normalise_date(value: str) -> str:
    """Canonicalise a date the model produced.

    The model is asked for ISO 8601 and obliges, but not identically every
    time - the same email has produced both "2026-09-23" and
    "2026-09-23T00:00:00Z". That value ends up as a column in the HR audit
    sheet, where mixed formats do not sort, and a midnight timestamp asserts a
    time nobody stated. So the format is decided here rather than hoped for.

    A date with no meaningful time becomes YYYY-MM-DD; one with a real time
    becomes YYYY-MM-DDTHH:MM. Anything unparseable is returned untouched
    rather than discarded, so a human still sees what the manager wrote.
    """
    text = (value or "").strip()
    if not text:
        return ""

    candidate = text.replace("Z", "+0000")
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if (parsed.hour, parsed.minute) == (0, 0):
            return parsed.strftime("%Y-%m-%d")
        return parsed.strftime("%Y-%m-%dT%H:%M")
    return text


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
        """Decide status and gaps from the fields, not from the model's word.

        The model's own ``missing`` list varies run to run for identical
        input, and it is what the manager is shown when a report cannot be
        resolved. So the gaps are recomputed here and merged with whatever the
        model said, rather than taken on trust.
        """
        self.meeting_date = normalise_date(self.meeting_date)

        gaps: list[str] = []
        if not self.employee.is_resolved():
            gaps.append("employee")
        if not self.manager.is_resolved():
            gaps.append("manager")
        if not self.hr.is_resolved():
            gaps.append("hr")
        if not self.meeting_date:
            gaps.append("meeting_date")
        if self.status == "OK" and not self.message_to_post.strip():
            gaps.append("message_to_post")

        if gaps:
            # A half-resolved OK would open a space with the wrong people in
            # it, so it becomes a question to the manager instead.
            self.status = "CLARIFY"
            self.missing = sorted(set(self.missing) | set(gaps))
            self.message_to_post = ""
        elif self.status == "CLARIFY":
            # Everything resolved but the model still asked to clarify - it may
            # have a reason the fields do not show, such as the mail not being
            # a compliance report at all. Its judgement stands.
            self.message_to_post = ""
            if not self.missing:
                self.missing = ["unspecified"]
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
