"""Safety policy: decides when a *write* needs a human, independent of what the LLM wants.

The LLM cannot bypass this: it sits in the harness between decision and execution, and the
human is shown the exact payload that would be submitted ("what you approve is what runs").
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Config

IDEMPOTENT_PATHS = ("/login", "/logout")          # POSTs that are safe to repeat
RISKY_PATH_WORDS = re.compile(r"delete|remove|cancel|void|pay(?!ables)|approve|refund", re.I)
AMOUNT_KEYS = ("amount", "total", "price", "value")
MATERIAL_KEYS = ("vendor", "invoice_number", "amount", "currency", "total")   # identity of an approval


@dataclass
class Assessment:
    is_write: bool = False
    needs_approval: bool = False
    reason: str = ""
    approval_key: str = ""
    fingerprint: str = ""
    action: str = ""
    summary: str = ""


def _num(s: str) -> float | None:
    try:
        return float(re.sub(r"[^\d.\-]", "", s))
    except ValueError:
        return None


class Policy:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def assess(self, tool: str, args: dict, ctx) -> Assessment:
        if tool == "web_submit_form":
            page = ctx.browser.page
            form = next((f for f in (page.forms if page else []) if f.id.lower() == str(args.get("form_id", "")).lower()), None)
            if not form:
                return Assessment()  # the tool itself will produce a helpful error
            path = urlparse(form.action).path
            values = {k: str(v) for k, v in (args.get("values") or {}).items() if "{{secret:" not in str(v)}
            if form.method == "GET" or path in IDEMPOTENT_PATHS:
                return Assessment(action=f"{form.method} {path}")
            a = Assessment(is_write=True, action=f"{form.method} {path}")
            a.fingerprint = hashlib.sha1(json.dumps([a.action, sorted(values.items())]).encode()).hexdigest()[:12]
            a.approval_key = json.dumps([a.action, sorted((k, v) for k, v in values.items() if k in MATERIAL_KEYS)])
            a.summary = f"{a.action} with {json.dumps(values)}"
            amt = next((_num(values[k]) for k in AMOUNT_KEYS if k in values), None)
            if amt is not None and amt >= self.cfg.approval_threshold:
                a.needs_approval, a.reason = True, f"amount {amt:,.2f} >= approval threshold {self.cfg.approval_threshold:,.0f}"
            elif RISKY_PATH_WORDS.search(path):
                a.needs_approval, a.reason = True, "destructive/financial action"
            return a
        if tool == "write_file":
            return Assessment(is_write=True, action=f"write_file {args.get('path')}")
        return Assessment()
