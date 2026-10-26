"""The tool set. Deliberately small and generic: nothing here knows about invoices."""
from __future__ import annotations

from .browser import Page
from .context import Ctx
from .tools import ToolRegistry, ToolResult


def page_result(page: Page) -> ToolResult:
    data = {
        "url": page.url, "status": page.status, "title": page.title, "needs_login": page.needs_login,
        "links": [{"id": i, "text": t, "href": h} for i, t, h in page.links],
        "forms": [{"id": f.id, "method": f.method, "action": f.action,
                   "fields": [{"name": x.name, "type": x.type, "options": [o[0] for o in x.options], "value": x.value}
                              for x in f.fields]} for f in page.forms],
        "text": page.text,
    }
    return ToolResult(page.status < 400, page.render(), data,
                      None if page.status < 400 else "permanent")


def build_registry() -> ToolRegistry:
    r = ToolRegistry()

    # ---------- meta ----------
    @r.register("update_plan", "Write or revise your plan as an ordered list of short steps. Call this first, and again when the plan changes.",
                {"steps": {"type": "array", "items": {"type": "string"}}}, ["steps"], effect="meta")
    def update_plan(ctx: Ctx, steps):
        ctx.memory.plan = [str(s) for s in steps]
        return ToolResult(True, "Plan recorded.", {"steps": ctx.memory.plan})

    @r.register("remember", "Store a fact you will need later (invoice number, amount, ids, decisions). Old observations are truncated, memory is not.",
                {"key": {"type": "string"}, "value": {"type": "string"}, "source": {"type": "string", "description": "where you learned it"}},
                ["key", "value"], effect="meta")
    def remember(ctx: Ctx, key, value, source=""):
        ctx.memory.remember(key, value, source, ctx.step)
        return ToolResult(True, f"Remembered {key}.")

    @r.register("ask_user", "Ask the human a question when the request is ambiguous or you lack information/permission. Never guess instead.",
                {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}, "description": "optional choices"}},
                ["question"], effect="meta")
    def ask_user(ctx: Ctx, question, options=None):
        ans = ctx.asker.ask("question", question, options)
        if ans is None:
            return ToolResult(False, "No answer available (non-interactive run). The run will pause as needs_input.",
                              {"question": question}, "needs_input")
        ctx.memory.remember(f"user_answer_{ctx.step}", f"Q: {question} A: {ans}", "ask_user", ctx.step)
        return ToolResult(True, f"User answered: {ans}", {"answer": ans})

    @r.register("finish",
                "End the run. Claims are verified INDEPENDENTLY by the harness: for each claim give a read-only check (web url or file path) "
                "and the literal strings that must appear there. outcome=success is only accepted if all claims verify.",
                {"outcome": {"type": "string", "enum": ["success", "blocked", "failed"]},
                 "summary": {"type": "string", "description": "concise report for the user: what was done, key values, anything notable"},
                 "claims": {"type": "array", "items": {"type": "object", "properties": {
                     "statement": {"type": "string"},
                     "check": {"type": "object", "properties": {"type": {"type": "string", "enum": ["web", "file"]},
                                                              "url": {"type": "string"}, "path": {"type": "string"}}, "required": ["type"]},
                     "expect": {"type": "array", "items": {"type": "string"}}},
                     "required": ["statement", "check", "expect"]}}},
                ["outcome", "summary"], effect="meta")
    def finish(ctx, **kw):  # executed by the Agent itself (needs verifier + run state)
        raise NotImplementedError

    # ---------- files ----------
    @r.register("list_files", "List files in a workspace directory (path, size, modified date). Modified date is the FILE date, not a document date.",
                {"directory": {"type": "string", "default": "."}})
    def list_files(ctx: Ctx, directory="."):
        items = ctx.files.list(directory)
        lines = [f"{i['path']}  ({i['size']} B, modified {i['modified']})" for i in items]
        return ToolResult(True, "\n".join(lines) or "(empty)", {"files": items})

    @r.register("read_file", "Read a text or PDF file from the workspace and return its text.",
                {"path": {"type": "string"}}, ["path"])
    def read_file(ctx: Ctx, path):
        text = ctx.files.read(path)
        return ToolResult(True, text[:6000] + ("\n...[truncated]" if len(text) > 6000 else ""), {"text": text, "path": path})

    @r.register("search_files", "Case-insensitive search of file names and contents (PDF included). Returns matching files with one matching line each.",
                {"query": {"type": "string"}, "directory": {"type": "string", "default": "."}}, ["query"])
    def search_files(ctx: Ctx, query, directory="."):
        m = ctx.files.search(query, directory)
        return ToolResult(True, "\n".join(f"{x['path']}: {x['line']}" for x in m) or f"No files match '{query}'.", {"matches": m})

    @r.register("write_file", "Create/overwrite a file under out/ (e.g. out/report.md).",
                {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"], effect="write")
    def write_file(ctx: Ctx, path, content):
        p = ctx.files.write(path, content)
        return ToolResult(True, f"Wrote {len(content)} chars to {ctx.files.rel(p)}", {"path": ctx.files.rel(p)})

    # ---------- web ----------
    @r.register("web_open", "Open a URL in the browser and get a text snapshot: page text, numbered links [L#], and forms [F#] with typed fields. "
                            "IDs are only valid for the most recent snapshot.", {"url": {"type": "string"}}, ["url"])
    def web_open(ctx: Ctx, url):
        return page_result(ctx.browser.open(url))

    @r.register("web_click", "Follow a link from the current snapshot by id, e.g. 'L3'.", {"link_id": {"type": "string"}}, ["link_id"])
    def web_click(ctx: Ctx, link_id):
        return page_result(ctx.browser.click(link_id))

    @r.register("web_submit_form", "Fill and submit a form from the current snapshot. 'values' maps field name -> value. Omitted fields keep defaults; hidden "
                                   "fields (CSRF) are handled for you. Use {{secret:NAME}} for credentials. Submitting POST forms is a WRITE: it may need user approval.",
                {"form_id": {"type": "string"}, "values": {"type": "object", "additionalProperties": {"type": "string"}}},
                ["form_id", "values"], effect="write")
    def web_submit_form(ctx: Ctx, form_id, values):
        return page_result(ctx.browser.submit(form_id, values))

    return r
