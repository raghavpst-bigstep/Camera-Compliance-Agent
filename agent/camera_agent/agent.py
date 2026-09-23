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

# The auto-updating Flash alias, so a new Flash release needs no commit.
# Note the Vertex spelling: the bare "gemini-flash" is the AI Studio form, and
# this service runs with GOOGLE_GENAI_USE_VERTEXAI=TRUE.
#
# Flash, not Flash-Lite: this agent calls a tool and must hold to a strict JSON
# contract, and the Lite tier trades instruction adherence for cost.
#
# Pin to an exact version (e.g. gemini-3.8-flash) before this handles real
# incidents - see "Choosing the model" in the README for why.
MODEL_ID = os.environ.get("MODEL_ID", "gemini-flash-latest")

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
