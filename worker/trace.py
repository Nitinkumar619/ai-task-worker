"""Append-only JSONL trace with secret redaction. Every decision/observation is auditable."""
from __future__ import annotations

import json
import time
from pathlib import Path


class Trace:
    def __init__(self, run_dir: Path, secrets: dict[str, str]):
        self.run_dir = run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        self.path = run_dir / "trace.jsonl"
        self._secrets = [v for v in secrets.values() if v]
        self.events: list[dict] = []
        self.listeners: list = []     # e.g. live console printer

    def redact(self, s: str) -> str:
        for sec in self._secrets:
            s = s.replace(sec, "***")
        return s

    def event(self, kind: str, **kw) -> dict:
        ev = {"t": round(time.time(), 3), "kind": kind, **kw}
        ev = json.loads(self.redact(json.dumps(ev, default=str)))
        self.events.append(ev)
        with self.path.open("a") as f:
            f.write(json.dumps(ev) + "\n")
        for fn in self.listeners:
            fn(ev)
        return ev
