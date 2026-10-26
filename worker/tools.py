"""Tool abstractions: results, error taxonomy, registry."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolResult:
    ok: bool
    content: str                      # what the LLM sees
    data: dict = field(default_factory=dict)   # structured payload (for harness / offline brain)
    error_kind: str | None = None     # transient | ambiguous_write | permanent | blocked | declined | needs_input


class ToolError(Exception):
    kind = "permanent"


class TransientError(ToolError):
    """Safe-to-retry failure of a READ (network blip, 502/503)."""
    kind = "transient"


class AmbiguousWriteError(ToolError):
    """A WRITE failed after it may have reached the server. Outcome unknown => verify, don't blind-retry."""
    kind = "ambiguous_write"

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class WriteAckedError(ToolError):
    """The server ACKNOWLEDGED a write (e.g. 303) but the follow-up confirmation page could not be loaded."""
    kind = "applied_unconfirmed"


class BlockedError(ToolError):
    kind = "blocked"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., ToolResult]
    effect: str = "read"              # read | write | meta

    def anthropic_spec(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, properties: dict, required: list[str] | None = None,
                 effect: str = "read"):
        def deco(fn):
            schema = {"type": "object", "properties": properties, "required": required or []}
            self._tools[name] = Tool(name, description, schema, fn, effect)
            return fn
        return deco

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        return [t.anthropic_spec() for t in self._tools.values()]
