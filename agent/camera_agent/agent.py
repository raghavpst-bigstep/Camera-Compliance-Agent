"""The Camera Compliance reasoning agent.

Defining the agent does not run it. This module builds the configuration;
``server.py`` wraps it in an ADK ``Runner``, which drives the event loop that
actually calls Gemini and executes the ``lookup_directory`` tool.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent

from .directory import lookup_directory
from .instruction import INSTRUCTION

# Flash, not Flash-Lite: this agent calls a tool and must hold to a strict
# JSON contract, and the Lite tier trades instruction adherence for cost.
# Overridable at deploy time, so moving to a newer model needs no code change.
MODEL_ID = os.environ.get("MODEL_ID", "gemini-3.8-flash")

root_agent = LlmAgent(
    name="camera_compliance",
    model=MODEL_ID,
    description=(
        "Reasoning component of the HR Camera Compliance Agent: resolves people "
        "through the employee directory, extracts incident details from a "
        "manager's report, and classifies incident-space messages."
    ),
    instruction=INSTRUCTION,
    tools=[lookup_directory],
)
