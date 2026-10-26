"""Generate the sandbox workspace: an inbox of synthetic vendor invoices.

Deliberate traps (so 'latest invoice' is not trivially solvable):
  * file modification times do NOT follow invoice dates
  * a CREDIT NOTE is the newest Globex document but is not an invoice
  * three different layouts / date formats / currency formats
  * 'Northwind' matches two different vendors (needs clarification)
  * one invoice is large (>5k approval threshold, >10k needs a PO in the ERP)
All data is fictional.
"""
from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "workspace" / "inbox"


def pdf(path: Path, lines: list[str]):
    c = canvas.Canvas(str(path), pagesize=letter)
    y = 740
    for ln in lines:
        c.setFont("Helvetica-Bold" if ln.isupper() and len(ln) < 40 else "Helvetica", 12)
        c.drawString(60, y, ln)
        y -= 20
    c.save()


def touch(path: Path, when: str):
    ts = time.mktime(datetime.strptime(when, "%Y-%m-%d").timetuple())
    os.utime(path, (ts, ts))


def main(dest: Path | None = None):
    """(Re)create the sandbox workspace at `dest` (default: ./workspace)."""
    global INBOX
    base = Path(dest) if dest else ROOT / "workspace"
    if base.exists():
        shutil.rmtree(base)
    INBOX = base / "inbox"
    INBOX.mkdir(parents=True)
    (base / "out").mkdir()

    # --- Northwind Traders: layout A (ISO dates) ---
    for no, issued, due, total, mtime in [("INV-2041", "2026-09-02", "2026-10-02", "3,980.00", "2026-10-20"),   # older, but newest mtime
                                          ("INV-2077", "2026-10-02", "2026-11-01", "4,280.50", "2026-10-03")]:
        p = INBOX / f"northwind_traders_{no}.pdf"
        pdf(p, ["Northwind Traders", "123 Harbor Road, Springfield", "", f"Invoice No: {no}", f"Invoice Date: {issued}",
                f"Due Date: {due}", "", "Item: Warehouse pallets x 40", "Item: Shrink wrap x 100", "",
                f"Total: USD {total}"])
        touch(p, mtime)

    # --- Northwind Logistics: second 'Northwind' (ambiguity) ---
    p = INBOX / "nw_logistics_inv_551.pdf"
    pdf(p, ["Northwind Logistics Ltd", "Invoice Number: NL-551", "Date of issue: 20 October 2026",
            "Payment due by: 19 November 2026", "", "Freight Oct 2026", "Amount Due: USD 1,915.00"])
    touch(p, "2026-10-21")

    # --- Globex: layout B (text, 'From:' header, d Mon yyyy, $) + credit note trap ---
    g1 = INBOX / "globex_sept.txt"
    g1.write_text("INVOICE #GX-9871\nFrom: Globex Corporation\nIssued: 09 Sep 2026\nPayment due by: 09 Oct 2026\n"
                  "\nConsulting - September\nAmount Due: $2,640.00\n")
    touch(g1, "2026-10-25")  # misleading: newest mtime of the invoices
    g2 = INBOX / "globex_oct_invoice.pdf"
    pdf(g2, ["INVOICE #GX-9913", "From: Globex Corporation", "Issued: 14 Oct 2026", "Payment due by: 13 Nov 2026", "",
             "Consulting - October", "Support retainer", "", "Amount Due: $3,120.75"])
    touch(g2, "2026-10-14")
    g3 = INBOX / "globex_credit_note.txt"
    g3.write_text("CREDIT NOTE #CN-0007\nFrom: Globex Corporation\nIssued: 20 Oct 2026\n"
                  "Credit against GX-9871 for duplicate billing.\nCredit Amount: $-400.00\n")
    touch(g3, "2026-10-22")

    # --- Initech: layout C, large amount with PO ---
    p = INBOX / "initech_2026_q4.pdf"
    pdf(p, ["Initech LLC", "BILL TO: Acme Corp Accounts Payable", "Invoice #: IN-30418", "Invoice date: 2026-10-10",
            "Terms: Net 30", "Due: 2026-11-09", "PO Number: PO-88123", "", "Server hardware refresh (Q4)",
            "Total Due: USD 18,450.00"])
    touch(p, "2026-10-11")

    (INBOX / "README_not_an_invoice.txt").write_text("Shared inbox for vendor invoices. Process via AP. Questions: ap@acme.example\n")
    return len(list(INBOX.iterdir()))


if __name__ == "__main__":
    print(f"Generated {main()} files in {INBOX}")
