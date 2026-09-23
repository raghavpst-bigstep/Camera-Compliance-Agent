"""Camera Compliance reasoning agent.

``root_agent`` is resolved lazily so that ``schemas`` and ``directory`` can be
imported (by tests, linters, or the orchestrator) without pulling in the whole
ADK stack.
"""

__all__ = ["root_agent"]


def __getattr__(name: str):
    if name == "root_agent":
        from .agent import root_agent

        return root_agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
