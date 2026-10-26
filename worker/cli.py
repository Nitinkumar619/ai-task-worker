"""CLI: run a task, run a canned scenario with live narration, or run the whole eval suite."""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from mock_erp.app import create_app, serve_in_thread
from scripts import make_data

from .agent import Agent
from .askers import ScriptedAsker, TerminalAsker
from .brains import make_brain
from .builtin_tools import build_registry
from .config import Config, ROOT
from .scenarios import SCENARIOS, run_scenario

D, B, G, R, Y, CY, X = "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[0m"


def printer(ev: dict) -> None:
    k = ev["kind"]
    if k == "start":
        print(f"{B}▶ Task:{X} {ev['task']}\n{D}  brain={ev['brain']}{X}")
    elif k == "thought":
        print(f"{D}  💭 {ev['text']}{X}")
    elif k == "action":
        a = {kk: (str(v)[:70] + "…" if len(str(v)) > 70 else v) for kk, v in ev["args"].items()}
        print(f"{CY}  [{ev['step']:>2}] {ev['tool']}{X} {D}{a}{X}")
    elif k == "observation":
        lines = [l for l in ev["content"].strip().splitlines() if l.strip()]
        title = next((l for l in lines if l.startswith("TITLE:")), None)
        first = (lines[0][:110] if lines else "") if not title else f"{lines[0].split('  ')[0][5:].strip()[-40:]} | {title}"
        c = G if ev["ok"] else R
        print(f"       {c}{'✓' if ev['ok'] else '✗'}{X} {D}{ev.get('error_kind') or ''} {first}{X}")
    elif k == "retry":
        print(f"       {Y}↻ {ev['detail']}{X}")
    elif k == "guard":
        print(f"       {Y}🛡  {ev['detail']}{X}")
    elif k == "approval":
        print(f"       {Y}🔐 approval: {ev['reason']} -> {ev['answer']}{X}")
    elif k == "verify":
        for r in ev["results"]:
            print(f"       {G if r['ok'] else R}🔎 verify: {'PASS' if r['ok'] else 'FAIL'}{X} {D}{r['detail'][:100]}{X}")
    elif k == "final":
        pass


def show(res) -> None:
    c = {"completed": G, "unverified": Y, "needs_input": Y}.get(res.status, R)
    print(f"\n{B}{c}■ {res.status.upper()}{X}  {D}({res.steps} steps, {res.duration_s:.1f}s){X}\n  {res.summary}")
    if res.pending_question:
        print(f"  {Y}Waiting on: {res.pending_question}{X}")
    print(f"  {D}report: {res.run_dir / 'report.md'}{X}\n")


def _notice(brain) -> None:
    if type(brain).__name__ == "OfflineBrain":
        print(f"{Y}ℹ offline policy brain in use (no ANTHROPIC_API_KEY). It handles only the demo task shapes; "
              f"set the key for the general LLM brain.{X}")


def cmd_run(a) -> int:
    cfg = Config(runs_dir=ROOT / "runs", workspace=ROOT / "workspace")
    stop = None
    if a.erp_url:
        cfg.erp_base_url = a.erp_url
    else:
        faults = {}
        for item in filter(None, a.faults.split(",")):
            k, _, v = item.partition("=")
            faults[k] = int(v) if v else 1
        if not (cfg.workspace / "inbox").exists():
            make_data.main(cfg.workspace)
        url, stop = serve_in_thread(create_app(faults))
        cfg.erp_base_url = url
        print(f"{D}(started sandbox AcmePay at {url}, faults={faults}){X}")
    asker = TerminalAsker() if not (a.yes or a.answer) else ScriptedAsker(a.answer, True if a.yes else None)
    reg = build_registry()
    brain = make_brain(a.brain, cfg, reg)
    _notice(brain)
    res = Agent(cfg, brain, asker, reg, printer).run(a.task)
    show(res)
    return 0 if res.status == "completed" else 1


def cmd_demo(a) -> int:
    sc = next((s for s in SCENARIOS if s.id == a.scenario), None)
    if not sc:
        print("Unknown scenario. Available:", ", ".join(s.id for s in SCENARIOS))
        return 2
    print(f"{B}Scenario: {sc.title}{X}  {D}faults={sc.faults}{X}\n")
    r = run_scenario(sc, a.brain, printer, runs_dir=ROOT / "runs")
    for res in r.results:
        show(res)
    print(f"{G + '✔ ground-truth check passed' if r.passed else R + '✘ ground-truth check FAILED: ' + '; '.join(r.errors)}{X}")
    return 0 if r.passed else 1


def cmd_eval(a) -> int:
    rows, ok = [], True
    for sc in SCENARIOS:
        if a.ids and sc.id not in a.ids:
            continue
        r = run_scenario(sc, a.brain)
        ok &= r.passed
        rows.append((sc.id, "PASS" if r.passed else "FAIL", sum(x.steps for x in r.results), "; ".join(r.errors)[:90]))
    print(f"\n{'scenario':<20}{'result':<8}{'steps':<7}notes")
    for sid, st, steps, note in rows:
        print(f"{sid:<20}{(G if st == 'PASS' else R)}{st:<8}{X}{steps:<7}{note}")
    print(f"\n{G + 'ALL PASSED' if ok else R + 'FAILURES'}{X}")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(prog="worker", description="Autonomous AI task worker (sandbox prototype)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a natural-language task")
    r.add_argument("task")
    r.add_argument("--brain", default="auto", choices=["auto", "anthropic", "openai", "offline"])
    r.add_argument("--faults", default="", help="inject sandbox faults, e.g. flaky_get=2,commit_then_500=1,session_expiry=4")
    r.add_argument("--erp-url", help="use an already-running AcmePay instead of starting one")
    r.add_argument("--yes", action="store_true", help="auto-approve gated actions (non-interactive)")
    r.add_argument("--answer", action="append", default=[], help="pre-supply an answer to a clarification question (repeatable)")
    d = sub.add_parser("demo", help="run one canned scenario with live narration")
    d.add_argument("scenario", help=", ".join(s.id for s in SCENARIOS))
    d.add_argument("--brain", default="auto", choices=["auto", "anthropic", "openai", "offline"])
    e = sub.add_parser("eval", help="run all scenarios and check ground truth")
    e.add_argument("ids", nargs="*")
    e.add_argument("--brain", default="auto", choices=["auto", "anthropic", "openai", "offline"])
    a = ap.parse_args()
    sys.exit({"run": cmd_run, "demo": cmd_demo, "eval": cmd_eval}[a.cmd](a))
