"""Harness-level guarantees, tested with deliberately misbehaving brains."""
import json
from pathlib import Path

import pytest

from tests.conftest import GenBrain, call
from worker.agent import Agent
from worker.askers import ScriptedAsker
from worker.builtin_tools import build_registry
from worker.files import Workspace
from worker.policy import Policy
from worker.tools import BlockedError

LOGIN = {"username": "{{secret:ERP_USERNAME}}", "password": "{{secret:ERP_PASSWORD}}"}


def login_and_open_form(base):
    r = yield call("web_open", url=f"{base}/bills/new")
    r = yield call("web_submit_form", form_id="F1", values=LOGIN)
    r = yield call("web_open", url=f"{base}/bills/new")
    return r


FORM = {"vendor": "Globex Corporation", "invoice_number": "GX-1", "amount": "100.00", "currency": "USD", "due_date": "2026-12-01"}


def test_blind_retry_after_ambiguous_write_is_blocked(env):
    app, cfg = env({"commit_then_500": 1})
    seen = {}

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        r1 = yield call("web_submit_form", form_id="F1", values=FORM)           # commits, then 500
        seen["first"] = r1.error_kind
        yield call("web_open", url=f"{cfg.erp_base_url}/bills/new")             # re-opening the SAME form proves nothing
        r2 = yield call("web_submit_form", form_id="F1", values=FORM)           # naive retry
        seen["second"] = r2.error_kind
        yield call("web_open", url=f"{cfg.erp_base_url}/bills")                 # now it reads state
        yield call("finish", outcome="success", summary="ok", claims=[{"statement": "bill exists",
                   "check": {"type": "web", "url": f"{cfg.erp_base_url}/bills"}, "expect": ["GX-1", "100.00"]}])

    res = Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    assert seen == {"first": "ambiguous_write", "second": "blocked"}
    assert len(app.erp_state["bills"]) == 1 and res.status == "completed"


def test_hallucinated_success_is_not_accepted(env):
    app, cfg = env()

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        for _ in range(5):   # claims a bill exists that was never created
            yield call("finish", outcome="success", summary="Done!", claims=[{"statement": "bill exists",
                       "check": {"type": "web", "url": f"{cfg.erp_base_url}/bills"}, "expect": ["GX-9913"]}])

    res = Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    assert res.status == "unverified" and "could NOT be verified" in res.summary


def test_write_without_claims_rejected(env):
    app, cfg = env()
    out = {}

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        yield call("web_submit_form", form_id="F1", values=FORM)
        r = yield call("finish", outcome="success", summary="done")
        out["kind"] = r.error_kind
        yield call("finish", outcome="blocked", summary="stop")

    Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    assert out["kind"] == "verification_failed"


def test_repeated_failing_call_aborts_loop(env):
    _app, cfg = env()

    def script():
        while True:
            yield call("read_file", path="inbox/does_not_exist.pdf")

    res = Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    assert res.status == "failed" and "loop" in res.summary


def test_step_budget(env):
    _app, cfg = env()
    cfg.max_steps = 5

    def script():
        i = 0
        while True:
            i += 1
            yield call("list_files", directory="inbox" if i % 2 else ".")

    assert Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t").status == "failed"


def test_secrets_never_reach_trace_or_report(env):
    app, cfg = env()

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        yield call("finish", outcome="blocked", summary="x")

    res = Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    blob = (res.run_dir / "trace.jsonl").read_text() + (res.run_dir / "report.md").read_text()
    assert "sandbox-pass" not in blob and "{{secret:ERP_PASSWORD}}" in blob


def test_host_allowlist_blocks_other_sites(env):
    _app, cfg = env()
    out = {}

    def script():
        r = yield call("web_open", url="http://evil.example/steal")
        out["k"] = r.error_kind
        yield call("finish", outcome="blocked", summary="x")

    Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    assert out["k"] == "blocked"


def test_workspace_sandbox(tmp_path):
    ws = Workspace(tmp_path)
    (tmp_path / "out").mkdir()
    with pytest.raises(BlockedError):
        ws.resolve("../etc/passwd")
    with pytest.raises(BlockedError):
        ws.write("inbox/x.txt", "no")
    assert ws.write("out/ok.txt", "yes").read_text() == "yes"


def test_approval_threshold_and_decline(env):
    app, cfg = env()
    big = dict(FORM, amount="9000.00")
    out = {}

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        r = yield call("web_submit_form", form_id="F1", values=big)
        out["k"] = r.error_kind
        yield call("finish", outcome="blocked", summary="declined")

    asker = ScriptedAsker(approve=False)
    res = Agent(cfg, GenBrain(script), asker, build_registry()).run("t")
    assert out["k"] == "declined" and app.erp_state["bills"] == [] and asker.log and res.status == "blocked"


def test_policy_small_amount_needs_no_approval(env):
    _app, cfg = env()
    out = {}

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        r = yield call("web_submit_form", form_id="F1", values=FORM)
        out["ok"] = r.ok
        yield call("finish", outcome="blocked", summary="x")

    asker = ScriptedAsker()
    Agent(cfg, GenBrain(script), asker, build_registry()).run("t")
    assert out["ok"] and asker.log == []


def test_invalid_select_value_gives_actionable_error(env):
    _app, cfg = env()
    out = {}

    def script():
        yield from login_and_open_form(cfg.erp_base_url)
        r = yield call("web_submit_form", form_id="F1", values=dict(FORM, vendor="Nonexistent Co"))
        out["msg"] = r.content
        yield call("finish", outcome="blocked", summary="x")

    Agent(cfg, GenBrain(script), ScriptedAsker(), build_registry()).run("t")
    assert "Options" in out["msg"] and "Globex Corporation" in out["msg"]
