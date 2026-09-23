"""HTTP wrapper around the Camera Compliance agent.

The ADK ``Runner`` is what actually executes the agent: it drives the event
loop, sends each reasoning step to Gemini, pauses to run ``lookup_directory``
when the model asks for it, and resumes until a final response is produced.

One HTTP request = one invocation = one throwaway session. All durable
incident state lives in the orchestrator's Firestore, never here, so an
in-memory session service is the correct choice.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from camera_agent.agent import root_agent
from camera_agent.schemas import IntakeResult, MessageResult, extract_json

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format='{"severity":"%(levelname)s","message":"%(message)s","logger":"%(name)s"}',
)
log = logging.getLogger("camera_agent.server")

APP_NAME = "camera_compliance"
MAX_ATTEMPTS = 2

_REPAIR = (
    "Your previous response could not be used: {error}\n"
    "Return ONLY the JSON object required for this mode. No prose, no code "
    "fences, no explanation."
)


class IntakeRequest(BaseModel):
    mode: Literal["INTAKE"]
    raw_text: str
    reported_by: str = ""
    received_at: str = ""
    screenshot_attached: bool = False


class MessageRequest(BaseModel):
    mode: Literal["MESSAGE"]
    message_text: str
    sender_email: str = ""
    sender_role: Literal["employee", "manager", "HR", "unknown"] = "unknown"
    incident_state: str = ""


class InvokeRequest(BaseModel):
    payload: dict[str, Any] = Field(
        ...,
        description="The invocation payload; must carry a 'mode' of INTAKE or MESSAGE.",
    )
    request_id: str = ""


session_service = InMemorySessionService()
runner: Runner | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global runner
    runner = Runner(
        app_name=APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )
    log.info("runner ready for model=%s", os.environ.get("MODEL_ID", "gemini-2.5-flash"))
    yield


app = FastAPI(title="Camera Compliance Agent", lifespan=lifespan)


async def _run_once(text: str, request_id: str) -> str:
    """Drive one ADK invocation and return the final response text."""
    if runner is None:  # pragma: no cover - lifespan always sets this
        raise RuntimeError("runner not initialised")

    user_id = "orchestrator"
    session_id = f"{request_id or uuid.uuid4().hex}-{uuid.uuid4().hex[:8]}"
    await session_service.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(
                    part.text for part in event.content.parts if part.text
                )
    finally:
        try:
            await session_service.delete_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id
            )
        except Exception:  # noqa: BLE001 - cleanup must never mask the result
            log.warning("could not delete session %s", session_id)

    return final_text


def _validate(mode: str, raw: dict) -> dict:
    if mode == "INTAKE":
        return IntakeResult.model_validate(raw).model_dump()
    return MessageResult.model_validate(raw).model_dump()


@app.post("/invoke")
async def invoke(request: InvokeRequest) -> dict:
    payload = request.payload
    mode = payload.get("mode")

    if mode == "INTAKE":
        validated_input = IntakeRequest.model_validate(payload)
    elif mode == "MESSAGE":
        validated_input = MessageRequest.model_validate(payload)
    else:
        raise HTTPException(422, f"mode must be INTAKE or MESSAGE, got {mode!r}")

    prompt = json.dumps(validated_input.model_dump(), ensure_ascii=False, indent=2)
    request_id = request.request_id or uuid.uuid4().hex

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = prompt if attempt == 1 else f"{prompt}\n\n{_REPAIR.format(error=last_error)}"
        try:
            raw_response = await _run_once(text, request_id)
            result = _validate(mode, extract_json(raw_response))
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)
            log.warning(
                "requestId=%s attempt=%d invalid agent output: %s",
                request_id,
                attempt,
                last_error,
            )
            continue
        except Exception as exc:  # noqa: BLE001
            log.exception("requestId=%s agent invocation failed", request_id)
            raise HTTPException(502, f"agent invocation failed: {exc}") from exc

        log.info("requestId=%s mode=%s attempt=%d ok", request_id, mode, attempt)
        return {"request_id": request_id, "result": result}

    # Both attempts produced unusable output. For INTAKE this is a CLARIFY, not
    # a crash: a human still gets the incident, just without auto-resolution.
    log.error("requestId=%s mode=%s unusable after %d attempts", request_id, mode, MAX_ATTEMPTS)
    if mode == "INTAKE":
        return {
            "request_id": request_id,
            "result": IntakeResult(
                status="CLARIFY", missing=["agent_output_unparseable"]
            ).model_dump(),
            "degraded": True,
        }
    return {
        "request_id": request_id,
        "result": MessageResult(
            classification="OTHER", sender_role=validated_input.sender_role
        ).model_dump(),
        "degraded": True,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "runner": runner is not None}
