"""LLM-brain plumbing (stubbed client - no network) and offline-policy parsing."""
from types import SimpleNamespace as NS

from worker.agent import Decision, RunState, Step, ToolCall
from worker.brains import AnthropicBrain
from worker.builtin_tools import build_registry
from worker.config import Config
from worker.memory import Memory
from worker.offline import parse_invoice, vendor_query
from worker.tools import ToolResult


class StubClient:
    def __init__(self):
        self.calls = []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return NS(content=[NS(type="text", text="Opening the page."),
                           NS(type="tool_use", id="toolu_X", name="web_open", input={"url": "http://x/bills"})])


def make_state(n_steps):
    cfg, mem = Config(), Memory()
    mem.remember("invoice_no", "GX-9913")
    mem.plan = ["a", "b"]
    st = RunState("do the thing", cfg, mem)
    for i in range(n_steps):
        st.steps.append(Step(i + 1, "thinking", ToolCall(f"toolu_{i}", "read_file", {"path": f"f{i}"}),
                             ToolResult(i % 2 == 0, "X" * 1000)))
    return cfg, st


def test_anthropic_brain_request_shape_and_parse():
    cfg, st = make_state(8)
    client = StubClient()
    brain = AnthropicBrain(cfg, build_registry(), client=client)
    d = brain.decide(st)
    assert d.call == ToolCall("toolu_X", "web_open", {"url": "http://x/bills"}) and d.text == "Opening the page."
    kw = client.calls[0]
    assert kw["tool_choice"]["disable_parallel_tool_use"] is True
    assert {t["name"] for t in kw["tools"]} >= {"web_open", "web_submit_form", "finish", "ask_user", "remember"}
    assert "GX-9913" in kw["system"] and "1. a" in kw["system"]          # memory + plan re-injected every turn
    msgs = kw["messages"]
    assert msgs[0]["role"] == "user" and msgs[-1]["role"] == "user"
    assert all(m["role"] == ("assistant" if i % 2 else "user") for i, m in enumerate(msgs))
    # tool_use ids pair with tool_result ids
    uses = [b["id"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
    results = [b["tool_use_id"] for m in msgs if m["role"] == "user" and isinstance(m["content"], list) for b in m["content"]]
    assert uses == results
    # compaction: old observations truncated, recent ones kept whole
    first_result = msgs[2]["content"][0]["content"]
    last_result = msgs[-1]["content"][0]["content"]
    assert "truncated" in first_result and len(first_result) < 400 and len(last_result) == 1000


def test_text_only_reply_becomes_no_call_decision():
    cfg, st = make_state(0)
    client = StubClient()
    client.messages = NS(create=lambda **kw: NS(content=[NS(type="text", text="I think...")]))
    assert AnthropicBrain(cfg, build_registry(), client=client).decide(st) == Decision("I think...", None)


def test_parse_invoice_layouts_and_traps():
    a = parse_invoice("Northwind Traders\nInvoice No: INV-2077\nInvoice Date: 2026-10-02\nDue Date: 2026-11-01\nTotal: USD 4,280.50")
    assert (a["vendor"], a["number"], a["amount"], a["due"]) == ("Northwind Traders", "INV-2077", 4280.50, "2026-11-01")
    b = parse_invoice("INVOICE #GX-9913\nFrom: Globex Corporation\nIssued: 14 Oct 2026\nPayment due by: 13 Nov 2026\nAmount Due: $3,120.75")
    assert (b["number"], b["due"], b["amount"], b["currency"]) == ("GX-9913", "2026-11-13", 3120.75, "USD")
    c = parse_invoice("Initech LLC\nInvoice #: IN-30418\nInvoice date: 2026-10-10\nDue: 2026-11-09\nPO Number: PO-88123\nTotal Due: USD 18,450.00")
    assert c["po"] == "PO-88123" and c["amount"] == 18450.0
    assert parse_invoice("CREDIT NOTE #CN-0007\nFrom: Globex Corporation\nIssued: 20 Oct 2026") is None


def test_vendor_query():
    assert vendor_query("Find the latest invoice from Globex, extract the amount") == "Globex"
    assert vendor_query("get the latest invoice from Northwind Traders and enter it") == "Northwind Traders"


# ---- OpenAI-compatible brain (free providers) ----
import json as _json
from worker.brains import OpenAICompatBrain, _compat_schema


def _fake_post(resp_message, status=200):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return NS(status_code=status, headers={}, text="err", json=lambda: {"choices": [{"message": resp_message}]})
    post.calls = calls
    return post


def test_compat_schema_strips_unsupported_and_stringifies_free_objects():
    reg = build_registry()
    sub = next(t for t in reg.specs() if t["name"] == "web_submit_form")
    s = _compat_schema(sub["input_schema"])
    assert s["properties"]["values"]["type"] == "string"
    assert "additionalProperties" not in _json.dumps(s) and '"default"' not in _json.dumps(_compat_schema(next(t for t in reg.specs() if t["name"] == "list_files")["input_schema"]))


def test_openai_brain_roundtrip_and_arg_decoding():
    cfg, st = make_state(8)
    post = _fake_post({"content": "Submitting.", "tool_calls": [{"id": "", "type": "function", "function": {
        "name": "web_submit_form", "arguments": _json.dumps({"form_id": "F1", "values": '{"amount": "10.00"}'})}}]})
    brain = OpenAICompatBrain(cfg, build_registry(), base_url="http://x/v1", api_key="k", model="m", post=post)
    d = brain.decide(st)
    assert d.call.name == "web_submit_form" and d.call.args["values"] == {"amount": "10.00"} and d.call.id
    url, headers, body = post.calls[0]
    assert url == "http://x/v1/chat/completions" and headers["Authorization"] == "Bearer k"
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "system" and roles.count("tool") == 8 and "GX-9913" in body["messages"][0]["content"]
    ids = [tc["id"] for m in body["messages"] if m.get("tool_calls") for tc in m["tool_calls"]]
    assert ids == [m["tool_call_id"] for m in body["messages"] if m["role"] == "tool"]


def test_openai_brain_text_only_and_bad_json():
    cfg, st = make_state(0)
    b = OpenAICompatBrain(cfg, build_registry(), base_url="http://x", api_key="k", model="m", post=_fake_post({"content": "hmm"}))
    assert b.decide(st) == Decision("hmm", None)
    bad = _fake_post({"content": "", "tool_calls": [{"id": "a", "function": {"name": "web_open", "arguments": "{oops"}}]})
    assert "_invalid_json" in OpenAICompatBrain(cfg, build_registry(), base_url="http://x", api_key="k", model="m", post=bad).decide(st).call.args


def test_openai_brain_waits_out_429_then_succeeds(monkeypatch):
    import worker.brains as wb
    sleeps = []
    monkeypatch.setattr(wb.time, "sleep", lambda s: sleeps.append(s))
    cfg, st = make_state(0)
    seq = [NS(status_code=429, headers={}, text='quota... "retryDelay": "23s"'),
           NS(status_code=200, headers={}, text="", json=lambda: {"choices": [{"message": {"content": "ok"}}]})]
    b = OpenAICompatBrain(cfg, build_registry(), base_url="http://x", api_key="k", model="m",
                          post=lambda *a, **k: seq.pop(0))
    assert b.decide(st).text == "ok" and any(s >= 23 for s in sleeps)


def test_openai_brain_daily_quota_fails_fast_with_advice():
    import pytest
    cfg, st = make_state(0)
    b = OpenAICompatBrain(cfg, build_registry(), base_url="http://x", api_key="k", model="m",
                          post=lambda *a, **k: NS(status_code=429, headers={}, text="Quota exceeded for metric ... PerDay"))
    with pytest.raises(RuntimeError, match="Daily free quota"):
        b.decide(st)