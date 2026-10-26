"""Human-readable run report: outcome, evidence, timeline, safety events."""
from __future__ import annotations


def write_report(res, state, ctx) -> None:
    icon = {"completed": "✅", "unverified": "⚠️", "blocked": "⛔", "failed": "❌", "needs_input": "⏸️"}.get(res.status, "")
    L = [f"# Run report {icon} `{res.status}`", "", f"**Task:** {state.task}", "", f"**Outcome:** {res.summary}", ""]
    if res.pending_question:
        L += [f"**Waiting on you:** {res.pending_question}", ""]
    L += [f"_{res.steps} steps in {res.duration_s:.1f}s_", "", "## Evidence (independently verified by the harness)", ""]
    if res.evidence:
        for e in res.evidence:
            L += [f"- {'✅' if e.ok else '❌'} **{e.statement}** — {e.detail}"]
            if e.evidence:
                L += ["", "  ```", *("  " + ln for ln in e.evidence.splitlines()[:25]), "  ```"]
    else:
        L += ["_none_"]
    L += ["", "## Side effects ledger (what actually happened)", ""]
    L += [f"- step {l['step']}: `{l['status']}` — {l['summary']}" for l in ctx.ledger] or ["_no side effects_"]
    L += ["", "## Safety & recovery events", ""]
    ev = [e for e in ctx.trace.events if e["kind"] in ("approval", "retry", "guard", "verify")]
    for e in ev:
        if e["kind"] == "verify":
            L.append(f"- verification at step {e['step']}: " + ", ".join("pass" if r["ok"] else "FAIL" for r in e["results"]))
        else:
            L.append(f"- {e['kind']}: " + ", ".join(f"{k}={v}" for k, v in e.items() if k not in ('t', 'kind')))
    if not ev:
        L.append("_none_")
    L += ["", "## Timeline", "", "| # | tool | ok | note |", "|---|------|----|------|"]
    for s in state.steps:
        if s.call:
            note = (s.result.error_kind or "") if s.result and not s.result.ok else ""
            L.append(f"| {s.n} | `{s.call.name}` | {'✓' if s.result and s.result.ok else '✗'} | {note} |")
    (res.run_dir / "report.md").write_text("\n".join(L) + "\n")
