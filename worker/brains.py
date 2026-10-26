"""Decision engines. A Brain turns RunState into the next Decision (one tool call).

AnthropicBrain  - real LLM tool-use loop (Claude). General: works for any task the tools can express.
OfflineBrain    - deterministic policy (see offline.py) so the harness can be exercised/CI'd with no API key.
"""
from __future__ import annotations

import json
import os
import re
import time

from .agent import Decision, RunState, ToolCall
from .config import Config
from .tools import ToolRegistry

SYSTEM_PROMPT = """You are an autonomous AI worker operating a computer on behalf of a user inside a company sandbox.
You complete the user's END GOAL by taking actions with your tools, observing results, and adapting. You are judged on whether the
goal was actually achieved and independently verified, not on how convincing your explanation is.

OPERATING PRINCIPLES
1. Act, don't explain. Never tell the user how to do something you can do yourself with a tool.
2. Start with update_plan (3-7 short steps) and keep it current. Decide each next step from the latest observation; never assume page
   structure, field names or values - read them from snapshots. Form/link ids are only valid for the latest snapshot.
3. Remember what matters: observations older than a few steps are truncated. Save key facts (numbers, ids, dates, urls, decisions) with `remember`.
4. Be precise with data. Extract values exactly as written in source documents, convert formats only as the target system requires
   (e.g. dates to the form's expected format), and re-check numbers against the source before submitting. Distinguish document types
   (an invoice is not a credit note or statement). 'Latest' means by the date written in the document, not by file modified time.
5. Failures are information. Read the error text, diagnose, and change your approach. Reads are retried automatically by the harness.
   A WRITE that fails with an ambiguous outcome (5xx/timeout) may have been applied: READ the system state to check before retrying -
   the harness will block blind repeats. If a session expired, log in again (re-open the form, because tokens change). Don't repeat the same failing call.
6. Safety: if the request is ambiguous (several plausible vendors/files/records) or you lack information or permission, call ask_user rather than guess.
   Never invent values. Risky writes are gated by human approval automatically; if declined, stop and report. Never type secrets: use {{secret:NAME}}.
   Stay inside the systems listed in the environment; other hosts are blocked.
7. Check for existing work first (e.g. does the record already exist?) so you don't create duplicates.
8. Finish properly: call finish with a concise summary and CLAIMS. Each claim names a read-only check (web url or file path) and the literal strings that must
   appear there (copy the formatting the page uses, e.g. '3,120.75'). The harness verifies them with an independent read. If verification fails, diagnose and fix.
   If you cannot complete the task, finish with outcome=blocked and say precisely what is done, what is missing, and what you need.
Respond with exactly one tool call per turn, with at most one sentence of reasoning before it.

ENVIRONMENT
{manifest}

CURRENT STATE
{memory}
Steps used: {used}/{budget}"""


class AnthropicBrain:
    KEEP_FULL = 5          # most recent observations are sent in full; older ones are truncated (memory carries the facts)
    OLD_CHARS = 280

    def __init__(self, cfg: Config, registry: ToolRegistry, client=None):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client, self.cfg, self.registry = client, cfg, registry

    # -- prompt construction (stateless: rebuilt from RunState every turn) --
    def system(self, state: RunState) -> str:
        return SYSTEM_PROMPT.format(manifest=self.cfg.manifest(), memory=state.memory.render(),
                                    used=len(state.steps), budget=self.cfg.max_steps)

    def messages(self, state: RunState) -> list[dict]:
        msgs: list[dict] = [{"role": "user", "content": f"TASK: {state.task}"}]
        n = len(state.steps)
        for idx, s in enumerate(state.steps):
            blocks = []
            if s.text:
                blocks.append({"type": "text", "text": s.text})
            if s.call:
                blocks.append({"type": "tool_use", "id": s.call.id, "name": s.call.name, "input": s.call.args})
            if not blocks:
                blocks.append({"type": "text", "text": "(no action)"})
            msgs.append({"role": "assistant", "content": blocks})
            content = s.result.content if s.result else ""
            if idx < n - self.KEEP_FULL and len(content) > self.OLD_CHARS:
                content = content[: self.OLD_CHARS] + " ...[older observation truncated; see REMEMBERED FACTS]"
            if s.call:
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": s.call.id, "content": content,
                                                          "is_error": bool(s.result and not s.result.ok)}]})
            else:
                msgs.append({"role": "user", "content": content})
        return msgs

    def decide(self, state: RunState) -> Decision:
        last_err = None
        for attempt in range(3):   # the SDK also retries; this covers overload / transient API errors
            try:
                resp = self.client.messages.create(
                    model=self.cfg.model, max_tokens=2048, system=self.system(state),
                    tools=self.registry.specs(), tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                    messages=self.messages(state))
                break
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status_code", None)
                if status is not None and status < 500 and status not in (408, 429):
                    raise                      # auth / bad request: retrying cannot help
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(f"LLM API failed after retries: {last_err}")
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        tu = next((b for b in resp.content if b.type == "tool_use"), None)
        return Decision(text, ToolCall(tu.id, tu.name, dict(tu.input)) if tu else None)


# ----------------------------------------------------------------------------- OpenAI-compatible (free providers)
PRESETS = {   # base_url, default model, key env var.  Model names/limits change often: override with LLM_MODEL.
    "gemini":     ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "groq":       ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "", "OPENROUTER_API_KEY"),
    "ollama":     ("http://localhost:11434/v1", "qwen2.5:7b", ""),
}


FREE_INTERVAL = {"gemini": 7.0, "groq": 3.0, "openrouter": 4.0}   # seconds between calls; override with LLM_MIN_INTERVAL


def _compat_schema(node):
    """Strip JSON-schema features that several free providers reject, and turn free-form objects into JSON strings."""
    if isinstance(node, dict):
        if node.get("type") == "object" and not node.get("properties"):
            return {"type": "string", "description": (node.get("description", "") + " JSON object encoded as a string, "
                    'e.g. {"field": "value"}').strip()}
        return {k: _compat_schema(v) for k, v in node.items() if k not in ("default", "additionalProperties")}
    if isinstance(node, list):
        return [_compat_schema(v) for v in node]
    return node


def _retry_delay(r) -> float:
    """Server-suggested wait: Retry-After header, or 'retry in 23.5s' / retryDelay: '23s' in the body."""
    try:
        h = float(r.headers.get("retry-after", 0))
        if h:
            return h
    except (ValueError, AttributeError, TypeError):
        pass
    m = re.search(r"retry(?:Delay)?\D{0,12}?(\d+(?:\.\d+)?)\s*s", r.text or "", re.I)
    return float(m.group(1)) if m else 0.0


class OpenAICompatBrain(AnthropicBrain):
    """Same prompt/memory/compaction logic, but speaks the OpenAI chat-completions protocol, so it works with
    Gemini (AI Studio), Groq, OpenRouter, a local Ollama model, or any compatible endpoint."""

    def __init__(self, cfg: Config, registry: ToolRegistry, base_url=None, api_key=None, model=None, post=None):
        provider = os.getenv("LLM_PROVIDER", "").lower()
        p_url, p_model, p_key_env = PRESETS.get(provider, ("", "", ""))
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or p_url).rstrip("/")
        self.api_key = api_key or os.getenv("LLM_API_KEY") or (os.getenv(p_key_env) if p_key_env else None) or "none"
        self.model = model or os.getenv("LLM_MODEL") or p_model
        if not self.base_url or not self.model:
            raise RuntimeError("Set LLM_PROVIDER (gemini|groq|openrouter|ollama) or LLM_BASE_URL, and LLM_MODEL.")
        if post is None:
            import requests
            post = requests.post
        self._post, self.cfg, self.registry = post, cfg, registry
        # Free tiers cap requests/minute: pace our own calls so we rarely hit the limit at all.
        self.min_interval = float(os.getenv("LLM_MIN_INTERVAL", FREE_INTERVAL.get(provider, 0)))
        self._last_call = 0.0
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": _compat_schema(t["input_schema"])}} for t in registry.specs()]

    def oa_messages(self, state: RunState) -> list[dict]:
        msgs = [{"role": "system", "content": self.system(state)}, {"role": "user", "content": f"TASK: {state.task}"}]
        n = len(state.steps)
        for idx, s in enumerate(state.steps):
            content = s.result.content if s.result else ""
            if idx < n - self.KEEP_FULL and len(content) > self.OLD_CHARS:
                content = content[: self.OLD_CHARS] + " ...[older observation truncated; see REMEMBERED FACTS]"
            if s.call:
                msgs.append({"role": "assistant", "content": s.text or None, "tool_calls": [
                    {"id": s.call.id, "type": "function", "function": {"name": s.call.name, "arguments": json.dumps(s.call.args)}}]})
                msgs.append({"role": "tool", "tool_call_id": s.call.id, "content": content})
            else:
                msgs.append({"role": "assistant", "content": s.text or "(no action)"})
                msgs.append({"role": "user", "content": content})
        return msgs

    def _args(self, name: str, raw) -> dict:
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except json.JSONDecodeError:
            return {"_invalid_json": str(raw)[:200]}       # tool layer returns a helpful "invalid arguments" error
        props = (self.registry.get(name).input_schema.get("properties", {}) if self.registry.get(name) else {})
        for k, v in list(args.items()):                     # decode params we exposed as JSON strings
            if isinstance(v, str) and props.get(k, {}).get("type") in ("object", "array"):
                try:
                    args[k] = json.loads(v)
                except json.JSONDecodeError:
                    pass
        return args

    def decide(self, state: RunState) -> Decision:
        body = {"model": self.model, "messages": self.oa_messages(state), "tools": self.tools, "tool_choice": "auto", "temperature": 0.2}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = None
        for attempt in range(6):                            # free tiers rate-limit aggressively: pace, back off, retry
            wait_for = self.min_interval - (time.time() - self._last_call)
            if wait_for > 0:
                time.sleep(wait_for)
            self._last_call = time.time()
            try:
                r = self._post(f"{self.base_url}/chat/completions", headers=headers, json=body, timeout=90)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200:
                break
            last = f"HTTP {r.status_code}: {r.text[:400]}"
            if r.status_code == 429 and re.search(r"per ?day|daily", r.text, re.I):
                raise RuntimeError("Daily free quota exhausted for this model/key. Wait for the reset, set LLM_MODEL to a different "
                                   "model, or switch provider (e.g. LLM_PROVIDER=groq). Details: " + r.text[:300])
            if r.status_code in (429, 500, 502, 503, 504):
                wait = _retry_delay(r)
                wait = wait if wait else 5 * (attempt + 1)
                print(f"       (rate limited by provider; waiting {wait:.0f}s, attempt {attempt + 1}/6)", flush=True)
                time.sleep(min(wait + 1, 90))
                continue
            raise RuntimeError(f"LLM API error {last}")
        else:
            raise RuntimeError(f"LLM API failed after retries: {last}")
        msg = r.json()["choices"][0]["message"]
        text = (msg.get("content") or "").strip()
        tcs = msg.get("tool_calls") or []
        if not tcs:
            return Decision(text, None)
        tc = tcs[0]
        return Decision(text, ToolCall(tc.get("id") or f"call_{len(state.steps) + 1}", tc["function"]["name"],
                                       self._args(tc["function"]["name"], tc["function"].get("arguments"))))


def make_brain(kind: str, cfg: Config, registry: ToolRegistry):
    if kind == "auto":
        if os.getenv("ANTHROPIC_API_KEY"):
            kind = "anthropic"
        elif os.getenv("LLM_PROVIDER") or os.getenv("LLM_BASE_URL"):
            kind = "openai"
        else:
            kind = "offline"
    if kind == "anthropic":
        return AnthropicBrain(cfg, registry)
    if kind == "openai":
        return OpenAICompatBrain(cfg, registry)
    from .offline import OfflineBrain
    return OfflineBrain()