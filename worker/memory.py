"""Working memory: facts the agent decided to keep + the current plan.

Older tool observations get truncated before they are sent back to the LLM, so
anything that must survive (invoice numbers, amounts, URLs, decisions) lives here
and is re-injected into every prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fact:
    value: str
    source: str = ""
    step: int = 0


@dataclass
class Memory:
    facts: dict[str, Fact] = field(default_factory=dict)
    plan: list[str] = field(default_factory=list)

    def remember(self, key: str, value: str, source: str = "", step: int = 0) -> None:
        self.facts[key] = Fact(str(value), source, step)

    def get(self, key: str, default: str | None = None) -> str | None:
        f = self.facts.get(key)
        return f.value if f else default

    def render(self) -> str:
        facts = "\n".join(f"  - {k}: {f.value}" + (f"   (source: {f.source})" if f.source else "")
                          for k, f in self.facts.items()) or "  (nothing yet)"
        plan = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(self.plan)) or "  (no plan yet - call update_plan first)"
        return f"PLAN:\n{plan}\nREMEMBERED FACTS:\n{facts}"
