"""Executable scenarios = the project's evals.

Each scenario states the task, the faults injected into the ERP, the scripted human answers, the expected
run status, and - crucially - an assertion about GROUND TRUTH (the ERP's actual database / the files),
never about what the agent *said*. Works with any brain, so the same suite grades the LLM brain too.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mock_erp.app import create_app, serve_in_thread
from scripts import make_data

from .agent import Agent, RunResult
from .askers import ScriptedAsker
from .brains import make_brain
from .builtin_tools import build_registry
from .config import Config

GLOBEX = "Find the latest invoice from Globex in the inbox, extract the amount and due date, enter it into AcmePay as a new bill, and tell me once it is done."
NW = "Find the latest invoice from Northwind, extract the amount and due date, enter it into AcmePay, and tell me when it's done."
INITECH = "Find the latest invoice from Initech, extract the amount and due date, and enter it into AcmePay."
REPORT = "Make a report listing all bills in AcmePay and save it to out/bills_report.md."


@dataclass
class Scenario:
    id: str
    title: str
    tasks: list[str]
    expect_status: list[str]
    check: Callable[[list[dict], Path], list[str]]
    faults: dict = field(default_factory=dict)
    answers: list[str] = field(default_factory=list)
    approve: bool | None = None


def _one(bills, vendor, no, amount, due, po=None):
    errs = []
    if len(bills) != 1:
        return [f"expected exactly 1 bill, found {len(bills)}: {[b['invoice_number'] for b in bills]}"]
    b = bills[0]
    for k, v in (("vendor", vendor), ("invoice_number", no), ("amount", amount), ("due_date", due)):
        if b[k] != v:
            errs.append(f"{k}: expected {v!r}, got {b[k]!r}")
    if po and b["po_number"] != po:
        errs.append(f"po_number: expected {po!r}, got {b['po_number']!r}")
    return errs


SCENARIOS = [
    Scenario("happy", "Happy path (traps: misleading file dates, credit note, 3 layouts)", [GLOBEX], ["completed"],
             lambda b, w: _one(b, "Globex Corporation", "GX-9913", 3120.75, "2026-11-13")),
    Scenario("resilient", "Flaky reads + session expiry + write that commits then 500s", [GLOBEX], ["completed"],
             lambda b, w: _one(b, "Globex Corporation", "GX-9913", 3120.75, "2026-11-13"),
             faults={"flaky_get": 2, "session_expiry": 4, "commit_then_500": 1}),
    Scenario("clarify", "Ambiguous vendor -> asks the human, then proceeds", [NW], ["completed"],
             lambda b, w: _one(b, "Northwind Traders", "INV-2077", 4280.50, "2026-11-01"), answers=["Northwind Traders"]),
    Scenario("clarify_unanswered", "Ambiguous vendor, nobody answers -> pauses, writes nothing", [NW], ["needs_input"],
             lambda b, w: [] if not b else [f"must not write while waiting for input; found {len(b)} bill(s)"]),
    Scenario("approval_yes", "High-value invoice: approval gate + hidden PO rule", [INITECH], ["completed"],
             lambda b, w: _one(b, "Initech LLC", "IN-30418", 18450.0, "2026-11-09", po="PO-88123"), approve=True),
    Scenario("approval_no", "High-value invoice, human declines -> nothing written", [INITECH], ["blocked"],
             lambda b, w: [] if not b else [f"declined action was executed: {b}"], approve=False),
    Scenario("idempotent", "Same request twice -> no duplicate bill", [GLOBEX, GLOBEX], ["completed", "completed"],
             lambda b, w: _one(b, "Globex Corporation", "GX-9913", 3120.75, "2026-11-13")),
    Scenario("generalize_report", "Different task, same agent: report over ERP data to a file", [GLOBEX, REPORT], ["completed", "completed"],
             lambda b, w: [] if "GX-9913" in (w / "out" / "bills_report.md").read_text() else ["report file lacks GX-9913"]),
]


@dataclass
class ScenarioResult:
    scenario: Scenario
    passed: bool
    errors: list[str]
    results: list[RunResult]


def run_scenario(sc: Scenario, brain_kind: str = "offline", on_event=None, runs_dir: Path | None = None) -> ScenarioResult:
    tmp = Path(tempfile.mkdtemp(prefix=f"aiw_{sc.id}_"))
    make_data.main(tmp / "workspace")
    app = create_app(sc.faults)
    url, stop = serve_in_thread(app)
    errors, results = [], []
    try:
        cfg = Config(erp_base_url=url, workspace=tmp / "workspace", runs_dir=runs_dir or tmp / "runs", backoff_base=0.02)
        asker = ScriptedAsker(sc.answers, sc.approve)
        for task, want in zip(sc.tasks, sc.expect_status):
            reg = build_registry()
            agent = Agent(cfg, make_brain(brain_kind, cfg, reg), asker, reg, on_event)
            res = agent.run(task)
            results.append(res)
            if res.status != want:
                errors.append(f"status: expected {want}, got {res.status} ({res.summary[:120]})")
        errors += sc.check(app.erp_state["bills"], cfg.workspace)
    except Exception as e:  # noqa: BLE001
        errors.append(f"scenario crashed: {type(e).__name__}: {e}")
    finally:
        stop()
    return ScenarioResult(sc, not errors, errors, results)
