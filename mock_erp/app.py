"""AcmePay - a small simulated internal accounts-payable web app.

It is deliberately *not* friendly to automation:
  * server-side sessions that can expire mid-task
  * per-session CSRF tokens embedded in forms (forms must be re-read after login)
  * business-rule validation that the form does not advertise (PO required > 10k)
  * duplicate detection
  * injectable faults: flaky GETs, and "commit-then-500" on writes (the nastiest
    kind of failure: the write succeeded but the client is told it failed)

Faults are configured by name via create_app(faults={...}).
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime

from flask import Flask, abort, redirect, render_template_string, request, session, url_for

VENDORS = ["Globex Corporation", "Initech LLC", "Northwind Logistics", "Northwind Traders", "Umbrella Supplies"]
CURRENCIES = ["USD", "EUR", "GBP"]
USERS = {"ap_clerk": "sandbox-pass"}  # sandbox-only credentials

CSS = """
body{font-family:system-ui,sans-serif;margin:0;background:#f6f7f9;color:#1c2430}
header{background:#14213d;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center}
header a{color:#ffd166;text-decoration:none} main{max-width:920px;margin:24px auto;padding:0 16px}
table{border-collapse:collapse;width:100%;background:#fff} th,td{border:1px solid #dde;padding:8px;text-align:left}
th{background:#eef1f6} label{display:block;margin:12px 0 4px;font-weight:600}
input,select{padding:8px;width:340px} button{margin-top:16px;padding:10px 18px;background:#14213d;color:#fff;border:0;border-radius:4px}
.alert{padding:10px 14px;border-radius:4px;margin:12px 0} .error{background:#fde2e2;border:1px solid #e57373}
.ok{background:#e3f6e8;border:1px solid #66bb6a}
"""

LAYOUT = """<!doctype html><html><head><meta charset=utf-8><title>{{ title }} - AcmePay</title>
<style>{{ css }}</style></head><body>
<header><strong>AcmePay AP</strong><a href="/bills">Bills</a><a href="/bills/new">New bill</a>
<a href="/vendors">Vendors</a>{% if user %}<a href="/logout">Sign out ({{ user }})</a>{% endif %}</header>
<main><h1>{{ title }}</h1>
{% for kind, msg in alerts %}<div class="alert {{ kind }}">{{ msg }}</div>{% endfor %}
{{ body|safe }}</main></body></html>"""


def create_app(faults: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = "sandbox-not-a-secret"
    state = {
        "bills": [],                # list of dict
        "sessions": {},             # sid -> {"user", "csrf", "n"}
        "faults": dict(faults or {}),
        "counters": {"get_bills": 0, "post_bills": 0, "auth_requests": 0, "expired": False},
        "lock": threading.Lock(),
    }
    app.erp_state = state  # exposed for ground-truth assertions in evals/tests

    def render(title, body="", alerts=None, status=200):
        user = (current_session() or {}).get("user")
        html = render_template_string(LAYOUT, title=title, body=body, alerts=alerts or [], css=CSS, user=user)
        return html, status

    def current_session():
        return state["sessions"].get(session.get("sid"))

    def require_login(is_post=False):
        """Returns a response if the caller must authenticate, else None."""
        s = current_session()
        if s:
            with state["lock"]:
                state["counters"]["auth_requests"] += 1
                n = state["faults"].get("session_expiry")
                if n and not state["counters"]["expired"] and state["counters"]["auth_requests"] > int(n):
                    state["counters"]["expired"] = True       # one-shot expiry
                    state["sessions"].pop(session.get("sid"), None)
                    s = None
        if s:
            return None
        if is_post:  # a form POST must never silently succeed/redirect when unauthenticated
            return login_page(next_url=request.path, alerts=[("error", "Session expired. Please sign in again.")], status=401)
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    def login_page(next_url="/bills", alerts=None, status=200):
        body = f"""<form method="post" action="/login">
        <input type="hidden" name="next" value="{next_url}">
        <label>Username</label><input name="username" type="text">
        <label>Password</label><input name="password" type="password">
        <button type="submit">Sign in</button></form>"""
        return render("Sign in", body, alerts, status)

    def fmt(amount):
        return f"{amount:,.2f}"

    # ---------- auth ----------
    @app.get("/")
    def index():
        return redirect("/bills")

    @app.get("/login")
    def login():
        return login_page(request.args.get("next", "/bills"))

    @app.post("/login")
    def login_post():
        u, p = request.form.get("username", ""), request.form.get("password", "")
        if USERS.get(u) != p or not u:
            return login_page(alerts=[("error", "Invalid username or password.")], status=401)
        sid = uuid.uuid4().hex
        state["sessions"][sid] = {"user": u, "csrf": uuid.uuid4().hex[:16], "n": 0}
        session["sid"] = sid
        nxt = request.form.get("next") or "/bills"
        return redirect(nxt if nxt.startswith("/") else "/bills", code=303)

    @app.get("/logout")
    def logout():
        state["sessions"].pop(session.get("sid"), None)
        session.clear()
        return redirect("/login")

    # ---------- read pages ----------
    @app.get("/bills")
    def bills():
        if (r := require_login()):
            return r
        with state["lock"]:
            state["counters"]["get_bills"] += 1
            if state["counters"]["get_bills"] <= int(state["faults"].get("flaky_get", 0)):
                abort(503)
        rows = "".join(
            f"<tr><td><a href='/bills/{b['id']}'>#{b['id']}</a></td><td>{b['vendor']}</td><td>{b['invoice_number']}</td>"
            f"<td>{fmt(b['amount'])}</td><td>{b['currency']}</td><td>{b['due_date']}</td><td>{b['status']}</td></tr>"
            for b in state["bills"]
        ) or "<tr><td colspan=7>No bills yet</td></tr>"
        body = ("<table><tr><th>ID</th><th>Vendor</th><th>Invoice #</th><th>Amount</th><th>Currency</th>"
                f"<th>Due date</th><th>Status</th></tr>{rows}</table>")
        return render("Bills", body)

    @app.get("/bills/<int:bill_id>")
    def bill_detail(bill_id):
        if (r := require_login()):
            return r
        b = next((x for x in state["bills"] if x["id"] == bill_id), None)
        if not b:
            return render("Not found", "<p>No such bill.</p>", status=404)
        body = ("<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in [
            ("Bill ID", f"#{b['id']}"), ("Vendor", b["vendor"]), ("Invoice number", b["invoice_number"]),
            ("Amount", f"{fmt(b['amount'])} {b['currency']}"), ("Due date", b["due_date"]),
            ("PO number", b["po_number"] or "-"), ("Memo", b["memo"] or "-"), ("Status", b["status"]),
            ("Entered by", b["entered_by"])]) + "</table>")
        alerts = [("ok", m) for m in session.pop("flash", [])]
        return render(f"Bill #{b['id']}", body, alerts)

    @app.get("/vendors")
    def vendors():
        if (r := require_login()):
            return r
        return render("Vendors", "<ul>" + "".join(f"<li>{v}</li>" for v in VENDORS) + "</ul>")

    # ---------- write: create bill ----------
    def bill_form(values=None, alerts=None, status=200):
        v = values or {}
        csrf = current_session()["csrf"]
        opts = "<option value=''>-- select --</option>" + "".join(
            f"<option value=\"{x}\"{' selected' if v.get('vendor') == x else ''}>{x}</option>" for x in VENDORS)
        cur = "".join(f"<option value='{c}'>{c}</option>" for c in CURRENCIES)
        body = f"""<form method="post" action="/bills">
        <input type="hidden" name="csrf" value="{csrf}">
        <label>Vendor</label><select name="vendor">{opts}</select>
        <label>Invoice number</label><input name="invoice_number" type="text" value="{v.get('invoice_number', '')}">
        <label>Amount</label><input name="amount" type="text" value="{v.get('amount', '')}">
        <label>Currency</label><select name="currency">{cur}</select>
        <label>Due date (YYYY-MM-DD)</label><input name="due_date" type="text" value="{v.get('due_date', '')}">
        <label>PO number (optional)</label><input name="po_number" type="text" value="{v.get('po_number', '')}">
        <label>Memo</label><input name="memo" type="text" value="{v.get('memo', '')}">
        <button type="submit">Create bill</button></form>"""
        return render("New bill", body, alerts, status)

    @app.get("/bills/new")
    def bills_new():
        if (r := require_login()):
            return r
        return bill_form()

    @app.post("/bills")
    def bills_create():
        if (r := require_login(is_post=True)):
            return r
        s = current_session()
        f = request.form
        if f.get("csrf") != s["csrf"]:
            return bill_form(f, [("error", "Invalid or expired form token (CSRF). Reload the form and try again.")], 400)
        vendor, inv = f.get("vendor", ""), f.get("invoice_number", "").strip()
        err = None
        try:
            amount = float(f.get("amount", "").replace(",", ""))
        except ValueError:
            amount = None
        po = f.get("po_number", "").strip()
        if vendor not in VENDORS:
            err = "Vendor is required and must be one of the listed vendors."
        elif not inv:
            err = "Invoice number is required."
        elif amount is None or amount <= 0:
            err = "Amount must be a positive number."
        elif f.get("currency") not in CURRENCIES:
            err = "Unsupported currency."
        elif not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f.get("due_date", "")) or not _valid_date(f["due_date"]):
            err = "Due date must be a valid date in YYYY-MM-DD format."
        elif amount > 10000 and not re.fullmatch(r"PO-\d+", po):
            err = "A valid PO number (format PO-12345) is required for bills over 10,000."
        if err:
            return bill_form(f, [("error", err)], 422)
        with state["lock"]:
            dup = next((b for b in state["bills"] if b["vendor"] == vendor and b["invoice_number"] == inv), None)
            if dup:
                return bill_form(f, [("error", f"Duplicate: invoice {inv} for {vendor} already exists as bill #{dup['id']}.")], 409)
            state["counters"]["post_bills"] += 1
            bill = {"id": len(state["bills"]) + 1, "vendor": vendor, "invoice_number": inv, "amount": amount,
                    "currency": f["currency"], "due_date": f["due_date"], "po_number": po, "memo": f.get("memo", ""),
                    "status": "Entered", "entered_by": s["user"]}
            state["bills"].append(bill)
        if state["faults"].get("commit_then_500") and state["counters"]["post_bills"] == 1:
            abort(500)  # the write is committed, but the client never finds out
        session["flash"] = [f"Bill #{bill['id']} created."]
        return redirect(f"/bills/{bill['id']}", code=303)

    return app


def _valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def serve_in_thread(app: Flask, port: int = 0):
    """Start the app on a free port in a daemon thread. Returns (base_url, shutdown_fn)."""
    import logging
    from werkzeug.serving import make_server
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    srv = make_server("127.0.0.1", port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_port}", srv.shutdown


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--faults", default="", help="comma list, e.g. flaky_get=2,commit_then_500=1,session_expiry=4")
    a = ap.parse_args()
    fl = {}
    for item in filter(None, a.faults.split(",")):
        k, _, v = item.partition("=")
        fl[k] = int(v) if v else 1
    create_app(fl).run(port=a.port, debug=False)
