"""Runtime configuration + the 'environment manifest' handed to the agent.

Nothing in the agent core is task-specific. What the agent knows about the world
(which systems exist, where files live, which secrets it may *reference*) comes
from here, so pointing it at a different environment is a config change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    erp_base_url: str = "http://127.0.0.1:5055"
    workspace: Path = ROOT / "workspace"
    runs_dir: Path = ROOT / "runs"
    max_steps: int = 30
    approval_threshold: float = 5000.0      # writes with an amount >= this need a human OK
    model: str = field(default_factory=lambda: os.getenv("WORKER_MODEL", "claude-sonnet-5-5"))
    # Sandbox-only credentials. The LLM only ever sees the placeholder {{secret:NAME}}.
    secrets: dict = field(default_factory=lambda: {
        "ERP_USERNAME": os.getenv("ERP_USERNAME", "ap_clerk"),
        "ERP_PASSWORD": os.getenv("ERP_PASSWORD", "sandbox-pass"),
    })
    http_retries: int = 3
    backoff_base: float = 0.2

    @property
    def allowed_hosts(self) -> set[str]:
        u = urlparse(self.erp_base_url)
        return {u.netloc}

    def manifest(self) -> str:
        """Human/LLM readable description of the environment."""
        return (
            f"Web systems you may use (all other hosts are blocked):\n"
            f"  - AcmePay, internal accounts-payable app: {self.erp_base_url}\n"
            f"    pages: /bills (list), /bills/new (create form), /bills/<id>, /vendors. Requires login.\n"
            f"    credentials are referenced as {{{{secret:ERP_USERNAME}}}} and {{{{secret:ERP_PASSWORD}}}} inside form values.\n"
            f"File workspace (read-only except out/): inbox/ holds vendor invoices (PDF/txt); write results to out/.\n"
            f"Human approval is automatically required for writes with an amount >= {self.approval_threshold:,.0f}."
        )
