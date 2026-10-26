# Run report ✅ `completed`

**Task:** Find the latest invoice from Globex, extract the amount and due date and enter it into AcmePay

**Outcome:** Extracted invoice GX-9913 ($3,120.75, due 2026-11-13) from inbox/globex_oct_invoice.pdf and created a new bill in AcmePay (Bill #1) with those details.

_12 steps in 150.0s_

## Evidence (independently verified by the harness)

- ✅ **The latest Globex invoice (GX-9913) with amount $3,120.75 and due date 2026-11-13 has been entered into AcmePay.** — All 4 expected values found at http://127.0.0.1:59157/bills/1.

  ```
  URL: http://127.0.0.1:59157/bills/1  (HTTP 200)
  TITLE: Bill #1 - AcmePay
  --- PAGE TEXT ---
  Bill #1 - AcmePay
  Bill #1
  | Bill ID | #1 |
  | Vendor | Globex Corporation |
  | Invoice number | GX-9913 |
  | Amount | 3,120.75 USD |
  | Due date | 2026-11-13 |
  | PO number | - |
  | Memo | - |
  | Status | Entered |
  | Entered by | ap_clerk |
  --- LINKS ---
  [L1] Bills -> http://127.0.0.1:59157/bills
  [L2] New bill -> http://127.0.0.1:59157/bills/new
  [L3] Vendors -> http://127.0.0.1:59157/vendors
  [L4] Sign out (ap_clerk) -> http://127.0.0.1:59157/logout
  --- FORMS ---
  (none)
  ```

## Side effects ledger (what actually happened)

- step 9: `applied` — POST /bills with {"vendor": "Globex Corporation", "invoice_number": "GX-9913", "amount": "3120.75", "due_date": "2026-11-13"}

## Safety & recovery events

- verification at step 12: pass

## Timeline

| # | tool | ok | note |
|---|------|----|------|
| 1 | `list_files` | ✓ |  |
| 2 | `read_file` | ✓ |  |
| 3 | `read_file` | ✓ |  |
| 4 | `web_open` | ✓ |  |
| 5 | `web_submit_form` | ✓ |  |
| 6 | `web_click` | ✓ |  |
| 7 | `web_click` | ✓ |  |
| 8 | `remember` | ✓ |  |
| 9 | `web_submit_form` | ✓ |  |
| 12 | `finish` | ✓ |  |
