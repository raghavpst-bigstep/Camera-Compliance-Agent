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

# An explicit version, because this service runs on Vertex
# (GOOGLE_GENAI_USE_VERTEXAI=TRUE) and Vertex does not accept the short
# developer aliases. "gemini-flash" is an AI Studio form and 404s here:
#
#   Publisher model .../publishers/google/models/gemini-flash was not found
#
# Flash, not Flash-Lite: this agent calls a tool and must hold to a strict JSON
# contract, and the Lite tier trades instruction adherence for cost.
#
# Overridable at deploy time, so moving to a newer Flash is an env var, never a
# commit. See "Choosing the model" in the README.
MODEL_ID = os.environ.get("MODEL_ID", "gemini-2.5-flash")

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
