"""Shared mutable run context handed to every tool."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .browser import Browser
from .config import Config
from .files import Workspace
from .memory import Memory
from .trace import Trace


@dataclass
class Ctx:
    cfg: Config
    memory: Memory
    browser: Browser
    files: Workspace
    asker: Any
    trace: Trace
    step: int = 0
    ledger: list[dict] = field(default_factory=list)   # record of side-effecting actions and their true outcome
