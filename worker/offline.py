"""OfflineBrain: a reactive, rule-based stand-in for the LLM.

PURPOSE: let the *harness* (tools, guards, retries, approvals, verification) run end-to-end and be
regression-tested without an API key. It is NOT the generalist: it understands two task shapes
(enter-an-invoice, report-on-bills). The AnthropicBrain handles arbitrary tasks with the same harness.

It is reactive, not a replay: every branch is driven by the previous observation
(login walls, 5xx after commit, validation errors, user answers, approval refusals).
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime

from .agent import Decision, RunState, ToolCall
from .tools import ToolResult


# ----------------------------------------------------------------------------- parsing helpers
def parse_date(s: str) -> str | None:
    s = s.strip().rstrip(".")
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def parse_invoice(text: str) -> dict | None:
    low = text.lower()
    if "credit note" in low or "invoice" not in low:
        return None
    vm = re.search(r"^(?:from|billed by|vendor)\s*[:\-]\s*(.+)$", text, re.I | re.M)
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    vendor = vm.group(1).strip() if vm else (first if "invoice" not in first.lower() else None)
    number = next((m.group(1) for m in re.finditer(r"invoice\s*(?:no\.?|number|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{2,})", text, re.I)
                   if re.search(r"\d", m.group(1))), None)
    issued = re.search(r"^(?:invoice date|issued|date of issue|date)\s*[:\-]\s*(.+)$", text, re.I | re.M)
    due = re.search(r"^(?:due date|payment due by|due by|due)\s*[:\-]\s*(.+)$", text, re.I | re.M)
    amt = re.search(r"^(?:total due|amount due|total|amount)\s*[:\-]?\s*(?:USD|EUR|GBP)?\s*[$€£]?\s*([\d,]+\.\d{2})", text, re.I | re.M)
    cur = re.search(r"\b(USD|EUR|GBP)\b", text)
    po = re.search(r"PO(?: Number)?\s*[:#]\s*(PO-\d+)", text)
    if not (vendor and number and issued and due and amt):
        return None
    iss, du = parse_date(issued.group(1)), parse_date(due.group(1))
    if not (iss and du):
        return None
    return {"vendor": vendor, "number": number, "issued": iss, "due": du, "amount": float(amt.group(1).replace(",", "")),
            "currency": cur.group(1) if cur else "USD", "po": po.group(1) if po else None}


def vendor_query(task: str) -> str | None:
    m = re.search(r"(?:invoices?|bills?)\s+(?:from|by)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*)*)", task)
    return m.group(1).strip() if m else None


# ----------------------------------------------------------------------------- brain
def C(name: str, text: str = "", **args) -> Decision:
    C.n += 1
    return Decision(text, ToolCall(f"off_{C.n}", name, args))
C.n = 0  # noqa: E305


class OfflineBrain:
    def __init__(self):
        self.gen = None

    def decide(self, state: RunState) -> Decision:
        try:
            if self.gen is None:
                self.gen = self._route(state.task, state.cfg.erp_base_url)
                return next(self.gen)
            return self.gen.send(state.last_result)
        except StopIteration:
            return C("finish", outcome="failed", summary="Offline policy ended without a result.")

    def _route(self, task: str, base: str):
        t = task.lower()
        if "invoice" in t and re.search(r"enter|record|add|create|input|key in", t):
            yield from self._invoice(task, base)
        elif re.search(r"report|summar|list", t) and "bill" in t:
            yield from self._report(task, base)
        else:
            yield C("finish", outcome="blocked",
                    summary="The offline policy only supports 'enter latest invoice' and 'bills report' tasks. "
                            "Set ANTHROPIC_API_KEY to use the general LLM brain for arbitrary tasks.")

    # ---- shared sub-routines (generators; `yield from` gives the ToolResult back) ----
    def _login(self, r: ToolResult):
        form = next(f for f in r.data["forms"] if any(x["type"] == "password" for x in f["fields"]))
        r = yield C("web_submit_form", "The app wants a login; authenticating with the sandbox credentials.",
                    form_id=form["id"], values={"username": "{{secret:ERP_USERNAME}}", "password": "{{secret:ERP_PASSWORD}}"})
        return r

    def _open(self, url: str):
        for _ in range(3):
            r = yield C("web_open", url=url)
            if r.error_kind == "transient":
                continue                      # harness already retried; try the page again
            if r.data.get("needs_login"):
                r = yield from self._login(r)
                if r.ok and r.data.get("url", "").rstrip("/") != url.rstrip("/"):
                    r = yield C("web_open", url=url)
            return r
        return r

    @staticmethod
    def _bill_form(r: ToolResult):
        return next((f for f in r.data.get("forms", []) if f["action"].endswith("/bills") and f["method"] == "POST"), None)

    @staticmethod
    def _row(r: ToolResult, vendor: str, number: str) -> bool:
        return any(vendor.lower() in ln.lower() and number in ln for ln in r.content.splitlines() if ln.startswith("|"))

    # ---- task: enter latest invoice ----
    def _invoice(self, task: str, base: str):
        vq = vendor_query(task)
        if not vq:
            r = yield C("ask_user", question="Which vendor's invoice should I process?")
            if not r.ok:
                return
            vq = r.data["answer"]
        yield C("update_plan", "Planning the work.", steps=[
            f"Find invoices from '{vq}' in inbox/", "Read them, ignore non-invoices, pick the latest by invoice date",
            "Check AcmePay for an existing bill (avoid duplicates)", "Create the bill in AcmePay", "Verify the bill by reading it back", "Report"])
        r = yield C("search_files", query=vq, directory="inbox")
        files = sorted({m["path"] for m in r.data.get("matches", [])})
        if not files:
            yield C("finish", outcome="blocked", summary=f"No files in inbox/ mention '{vq}'.")
            return
        docs = {}
        for f in files:
            r = yield C("read_file", path=f)
            d = parse_invoice(r.data.get("text", ""))
            if d:
                d["file"] = f
                docs[f] = d
        if not docs:
            yield C("finish", outcome="blocked", summary=f"Found files for '{vq}' but none are invoices (credit notes/statements are ignored).")
            return
        vendors = sorted({d["vendor"] for d in docs.values()})
        if len(vendors) > 1:
            r = yield C("ask_user", f"'{vq}' matches several vendors; I won't guess.", question=f"'{vq}' matches invoices from {len(vendors)} different vendors. Which one do you mean?", options=vendors)
            if not r.ok:
                return
            ans = r.data["answer"].lower()
            chosen = next((v for v in vendors if ans in v.lower() or v.lower() in ans), None)
            if not chosen:
                yield C("finish", outcome="blocked", summary=f"Could not match your answer '{r.data['answer']}' to a vendor: {vendors}")
                return
            docs = {f: d for f, d in docs.items() if d["vendor"] == chosen}
        inv = max(docs.values(), key=lambda d: d["issued"])
        yield C("remember", key="invoice", source=inv["file"],
                value=f"vendor={inv['vendor']}; number={inv['number']}; issued={inv['issued']}; due={inv['due']}; amount={inv['amount']:.2f} {inv['currency']}; po={inv['po']}")

        # existing-work check
        r = yield from self._open(f"{base}/bills")
        if not r.ok:
            yield C("finish", outcome="failed", summary=f"Could not open the bills list: {r.content[:200]}")
            return
        vendor_opts = None
        claims_url = f"{base}/bills"
        bill_id = None
        if self._row(r, inv["vendor"], inv["number"]):
            summary = f"Invoice {inv['number']} from {inv['vendor']} was already entered in AcmePay; nothing to do."
        else:
            r = yield from self._open(f"{base}/bills/new")
            form = self._bill_form(r)
            if not form:
                yield C("finish", outcome="failed", summary="Could not find the new-bill form.")
                return
            vendor_opts = next(f for f in form["fields"] if f["name"] == "vendor")["options"]
            erp_vendor = (difflib.get_close_matches(inv["vendor"], vendor_opts, n=1, cutoff=0.5) or [None])[0]
            if not erp_vendor:
                r = yield C("ask_user", question=f"No vendor in AcmePay matches '{inv['vendor']}'. Which should I use?", options=vendor_opts)
                if not r.ok:
                    return
                erp_vendor = r.data["answer"]
            values = {"vendor": erp_vendor, "invoice_number": inv["number"], "amount": f"{inv['amount']:.2f}",
                      "currency": inv["currency"], "due_date": inv["due"], "memo": f"Entered by AI worker from {inv['file']}"}
            done, po = False, None
            for _attempt in range(6):
                vals = dict(values, **({"po_number": po} if po else {}))
                r = yield C("web_submit_form", form_id=form["id"], values=vals)
                if r.error_kind == "needs_input":
                    return
                if r.error_kind == "declined":
                    yield C("finish", outcome="blocked", summary=f"Not entered: you declined the approval for {inv['number']} ({inv['amount']:,.2f} {inv['currency']}).")
                    return
                if r.ok and r.data.get("title", "").startswith("Bill #"):
                    done, claims_url = True, r.data["url"]
                    bill_id = re.search(r"#(\d+)", r.data["title"]).group(1)
                    break
                if r.error_kind == "ambiguous_write":
                    # Server errored after maybe committing. Verify by reading state, don't resubmit blindly.
                    lst = yield from self._open(f"{base}/bills")
                    if lst.ok and self._row(lst, erp_vendor, inv["number"]):
                        done = True
                        break
                    r = yield from self._open(f"{base}/bills/new")
                    form = self._bill_form(r)
                    continue
                if r.data.get("needs_login") or r.data.get("status") == 401:
                    yield from self._login(r)
                    r = yield from self._open(f"{base}/bills/new")
                    form = self._bill_form(r)
                    continue
                if "PO number" in r.content and inv["po"] and not po:
                    po = inv["po"]
                    yield C("remember", key="po_rule", value=f"AcmePay requires a PO number for bills over 10,000; using {po} from the invoice", source="AcmePay validation")
                    form = self._bill_form(r) or form
                    continue
                if r.data.get("status") == 400 or "CSRF" in r.content:
                    r = yield from self._open(f"{base}/bills/new")
                    form = self._bill_form(r)
                    continue
                yield C("finish", outcome="failed", summary=f"AcmePay rejected the bill and I could not resolve it: {r.content[:300]}")
                return
            if not done:
                yield C("finish", outcome="failed", summary="Gave up after repeated attempts to submit the bill.")
                return
            summary = (f"Entered {inv['vendor']} invoice {inv['number']} into AcmePay"
                       f"{' as bill #' + bill_id if bill_id else ''}: {inv['amount']:,.2f} {inv['currency']}, due {inv['due']} "
                       f"(latest invoice by date, from {inv['file']}).")
        expect = [inv["vendor"] if not vendor_opts else (difflib.get_close_matches(inv["vendor"], vendor_opts, n=1, cutoff=0.5) or [inv["vendor"]])[0],
                  inv["number"], f"{inv['amount']:,.2f}", inv["due"]]
        r = yield C("finish", outcome="success", summary=summary, claims=[{
            "statement": f"AcmePay has a bill for invoice {inv['number']} with the right amount and due date",
            "check": {"type": "web", "url": claims_url}, "expect": expect}])
        if not r.ok:   # verification failed once: re-authenticate (most likely cause) and try again
            lg = yield from self._open(f"{base}/bills")
            claims_url = f"{base}/bills"
            yield C("finish", outcome="success", summary=summary, claims=[{
                "statement": f"AcmePay lists invoice {inv['number']}", "check": {"type": "web", "url": claims_url}, "expect": expect}])

    # ---- task: report ----
    def _report(self, task: str, base: str):
        yield C("update_plan", "Planning the report.", steps=["Open the bills list", "Write out/bills_report.md", "Verify the file"])
        r = yield from self._open(f"{base}/bills")
        rows = [[c.strip() for c in ln.strip("| ").split(" | ")] for ln in r.content.splitlines() if ln.startswith("| #")]
        if not rows:
            yield C("finish", outcome="blocked", summary="No bills found to report on.")
            return
        total = sum(float(x[3].replace(",", "")) for x in rows)
        md = ["# Open bills", "", "| Vendor | Invoice | Amount | Currency | Due | Status |", "|---|---|---|---|---|---|"]
        md += [f"| {x[1]} | {x[2]} | {x[3]} | {x[4]} | {x[5]} | {x[6]} |" for x in sorted(rows, key=lambda x: x[5])]
        md += ["", f"Total (mixed currencies not converted): {total:,.2f}"]
        yield C("write_file", path="out/bills_report.md", content="\n".join(md))
        yield C("finish", outcome="success", summary=f"Wrote out/bills_report.md covering {len(rows)} bill(s).",
                claims=[{"statement": "report lists every bill", "check": {"type": "file", "path": "out/bills_report.md"},
                         "expect": [x[2] for x in rows]}])
