"""The agent harness: observe -> decide -> (gate) -> act -> record -> repeat, until a *verified* finish.

The brain (LLM or offline policy) only proposes the next action. Everything that makes the system
trustworthy lives here and is therefore task-independent:

  * policy gate + human approval before risky writes
  * write ledger: true outcome of every side effect, independent of what the model believes
  * blind-retry protection: after an ambiguous write failure, state must be re-read before repeating it
  * loop detection and step budget
  * finish() is verified by the harness, not trusted
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .askers import ScriptedAsker
from .browser import Browser
from .builtin_tools import build_registry
from .config import Config
from .context import Ctx
from .files import Workspace
from .memory import Memory
from .policy import Policy
from .tools import AmbiguousWriteError, BlockedError, ToolError, ToolRegistry, ToolResult, TransientError, WriteAckedError
from .trace import Trace
from .verify import ClaimResult, verify_claims

MAX_VERIFY_FAILURES = 3


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Decision:
    text: str = ""
    call: ToolCall | None = None


@dataclass
class Step:
    n: int
    text: str
    call: ToolCall | None
    result: ToolResult | None = None


@dataclass
class RunState:
    task: str
    cfg: Config
    memory: Memory
    steps: list[Step] = field(default_factory=list)

    @property
    def last_result(self) -> ToolResult | None:
        return self.steps[-1].result if self.steps else None


@dataclass
class RunResult:
    status: str                      # completed | unverified | blocked | failed | needs_input
    summary: str
    evidence: list[ClaimResult]
    steps: int
    run_dir: Path
    pending_question: str | None = None
    duration_s: float = 0.0


class Agent:
    def __init__(self, cfg: Config, brain, asker=None, registry: ToolRegistry | None = None, on_event=None):
        self.cfg, self.brain = cfg, brain
        self.asker = asker or ScriptedAsker()
        self.registry = registry or build_registry()
        self.policy = Policy(cfg)
        self.on_event = on_event
        self.approved: set[str] = set()
        self.verify_failures = 0
        self.final: tuple | None = None

    # ------------------------------------------------------------------ run
    def run(self, task: str) -> RunResult:
        t0 = time.time()
        run_dir = self.cfg.runs_dir / time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while run_dir.exists():
            run_dir = self.cfg.runs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
            n += 1
        trace = Trace(run_dir, self.cfg.secrets)
        if self.on_event:
            trace.listeners.append(self.on_event)
        memory = Memory()
        browser = Browser(self.cfg.allowed_hosts, self.cfg.secrets, self.cfg.http_retries, self.cfg.backoff_base,
                          on_retry=lambda m: trace.event("retry", detail=m))
        ctx = Ctx(self.cfg, memory, browser, Workspace(self.cfg.workspace), self.asker, trace)
        self.ctx = ctx
        state = RunState(task, self.cfg, memory)
        trace.event("start", task=task, brain=type(self.brain).__name__)

        for n in range(1, self.cfg.max_steps + 1):
            ctx.step = n
            try:
                decision = self.brain.decide(state)
            except Exception as e:  # brain failure must not crash the run silently
                trace.event("brain_error", error=f"{type(e).__name__}: {e}")
                self.final = ("failed", f"The decision engine failed: {type(e).__name__}: {e}", [])
                break
            if decision.text:
                trace.event("thought", step=n, text=decision.text)
            if decision.call is None:
                state.steps.append(Step(n, decision.text, None, ToolResult(False, "[harness] Reply with a tool call. When finished, call finish.")))
                continue
            trace.event("action", step=n, tool=decision.call.name, args=decision.call.args)
            result = self._execute(decision.call, state, ctx)
            state.steps.append(Step(n, decision.text, decision.call, result))
            trace.event("observation", step=n, tool=decision.call.name, ok=result.ok, error_kind=result.error_kind,
                        content=result.content[:1200])
            if self.final:
                break
        else:
            self.final = ("failed", f"Step budget exhausted ({self.cfg.max_steps} steps) before the task was verified complete.", [])

        status, summary, evidence = self.final[0], self.final[1], self.final[2]
        pending = self.final[3] if len(self.final) > 3 else None
        res = RunResult(status, summary, evidence, len(state.steps), run_dir, pending, time.time() - t0)
        trace.event("final", status=status, summary=summary, steps=len(state.steps))
        from .report import write_report
        write_report(res, state, ctx)
        return res

    # -------------------------------------------------------------- execute
    def _execute(self, call: ToolCall, state: RunState, ctx: Ctx) -> ToolResult:
        tool = self.registry.get(call.name)
        if not tool:
            return ToolResult(False, f"Unknown tool '{call.name}'. Available: {self.registry.names()}", error_kind="permanent")
        if call.name == "finish":
            return self._finish(call, ctx)

        notes: list[str] = []
        # --- loop detection
        same = 0
        for s in reversed(state.steps):
            if s.call and s.call.name == call.name and s.call.args == call.args and s.result and not s.result.ok:
                same += 1
            else:
                break
        if same >= 4:
            self.final = ("failed", f"Aborted: repeated the identical failing call {call.name} {same + 1} times (stuck in a loop).", [])
            return ToolResult(False, "Aborted: stuck in a loop.", error_kind="permanent")
        if same >= 2:
            notes.append(f"You have made this exact call {same + 1} times and it keeps failing. Change something or finish with outcome=blocked.")

        # --- policy + idempotency guard for writes
        a = self.policy.assess(call.name, call.args, ctx)
        if a.is_write and a.fingerprint:
            prior = [l for l in ctx.ledger if l["fingerprint"] == a.fingerprint and l["status"] in ("applied", "ambiguous")]
            if prior and all(l["reads_since"] == 0 for l in prior):
                p = prior[-1]
                ctx.trace.event("guard", step=ctx.step, detail=f"blocked repeat of step {p['step']} ({p['status']})")
                if p["status"] == "ambiguous":
                    msg = (f"BLOCKED: your identical write at step {p['step']} failed with an ambiguous outcome and may have been applied. "
                           f"Do NOT retry blindly (risk of duplicates). First read the system state (e.g. the list/detail page) to check whether it exists.")
                else:
                    msg = (f"BLOCKED: an identical write already succeeded at step {p['step']}. Re-submitting would duplicate it. "
                           f"Verify its effect by reading state instead; if you believe it did not take effect, read first, then retry.")
                return ToolResult(False, msg, error_kind="blocked")
        if a.needs_approval and a.approval_key not in self.approved:
            prompt = f"The agent wants to perform: {a.summary}\nReason approval is required: {a.reason}.\nApprove? (yes / no / or type instructions)"
            ans = self.asker.ask("approval", prompt, None)
            ctx.trace.event("approval", step=ctx.step, summary=a.summary, reason=a.reason, answer=ans)
            if ans is None:
                self.final = ("needs_input", "Paused: approval is required and no human answered.", [], prompt)
                return ToolResult(False, "Approval pending.", error_kind="needs_input")
            if ans.strip().lower() in ("y", "yes", "approve", "approved", "ok"):
                self.approved.add(a.approval_key)
            else:
                return ToolResult(False, f"The user DECLINED this action (reply: {ans!r}). Do not attempt it again. "
                                         f"Unless their reply gives you an alternative, call finish with outcome=blocked.", error_kind="declined")

        origin = ctx.browser.page.url if ctx.browser.page else None
        # --- run the tool
        try:
            result = tool.fn(ctx, **call.args)
        except TypeError as e:
            result = ToolResult(False, f"Invalid arguments for {call.name}: {e}. Schema: {json.dumps(tool.input_schema)}", error_kind="permanent")
        except AmbiguousWriteError as e:
            result = ToolResult(False, f"AMBIGUOUS WRITE FAILURE: {e} Check the system state before retrying.", error_kind="ambiguous_write")
        except (WriteAckedError, TransientError, BlockedError, ToolError) as e:
            result = ToolResult(False, f"{type(e).__name__}: {e}", error_kind=e.kind)
        except Exception as e:  # never let a tool bug kill the run
            ctx.trace.event("tool_crash", tool=call.name, tb=traceback.format_exc()[-800:])
            result = ToolResult(False, f"Internal error in {call.name}: {type(e).__name__}: {e}", error_kind="permanent")

        if result.error_kind == "needs_input" and not self.final:
            q = result.data.get("question", "")
            self.final = ("needs_input", f"Paused: I need your input to continue: {q}", [], q)

        # --- ledger: record the TRUE outcome of side effects
        if a.is_write and a.fingerprint:
            status = ("ambiguous" if result.error_kind == "ambiguous_write" else
                      "applied" if (result.ok or result.error_kind == "applied_unconfirmed") else "rejected")
            ctx.ledger.append({"step": ctx.step, "tool": call.name, "action": a.action, "fingerprint": a.fingerprint,
                               "summary": a.summary, "status": status, "reads_since": 0, "origin": origin})
        elif a.is_write and result.ok:
            ctx.ledger.append({"step": ctx.step, "tool": call.name, "action": a.action, "fingerprint": "",
                               "summary": a.action, "status": "applied", "reads_since": 0, "origin": origin})
        # A re-read only counts as "checking state" if it is a DIFFERENT page than the form the write came from
        # (re-opening the same form proves nothing about whether the write landed).
        if call.name in ("web_open", "web_click") and result.ok and ctx.browser.page:
            for l in ctx.ledger:
                if ctx.browser.page.url != l.get("origin"):
                    l["reads_since"] += 1
        if notes:
            result.content += "\n[harness] " + " ".join(notes)
        return result

    # --------------------------------------------------------------- finish
    def _finish(self, call: ToolCall, ctx: Ctx) -> ToolResult:
        a = call.args
        outcome, summary, claims = a.get("outcome", "failed"), a.get("summary", ""), a.get("claims") or []
        if outcome != "success":
            self.final = (outcome if outcome in ("blocked", "failed") else "failed", summary, [])
            return ToolResult(True, "Run ended.")
        wrote = [l for l in ctx.ledger if l["status"] in ("applied", "ambiguous")]
        if wrote and not claims:
            return ToolResult(False, "REJECTED: you changed state but gave no claims. Provide claims with read-only checks (a page or file that "
                                     "shows the result) and the literal strings expected there.", error_kind="verification_failed")
        results = verify_claims(claims, ctx)
        ctx.trace.event("verify", step=ctx.step, results=[{"statement": r.statement, "ok": r.ok, "detail": r.detail} for r in results])
        failed = [r for r in results if not r.ok]
        if not failed:
            for l in ctx.ledger:   # independent verification settles any write whose outcome was unknown
                if l["status"] == "ambiguous":
                    l["status"] = "applied (confirmed by verification)"
            self.final = ("completed", summary, results)
            return ToolResult(True, "All claims verified independently. Run complete.")
        self.verify_failures += 1
        if self.verify_failures >= MAX_VERIFY_FAILURES:
            self.final = ("unverified", f"{summary}\n\nWARNING: the outcome could NOT be verified: " + "; ".join(r.detail for r in failed), results)
            return ToolResult(False, "Verification failed repeatedly; run ended as unverified.", error_kind="verification_failed")
        return ToolResult(False, "VERIFICATION FAILED - your claim was not confirmed by an independent read:\n- " +
                          "\n- ".join(f"{r.statement}: {r.detail}" for r in failed) +
                          "\nDiagnose (is it missing, or did you expect the wrong text/format?), fix it, then call finish again.",
                          error_kind="verification_failed")
