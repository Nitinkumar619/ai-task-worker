# 4-minute demo video script

Record the terminal (and a browser tab on the mock ERP for one shot). Everything below is a real command.

| time | show | say |
|------|------|-----|
| 0:00 | `python -m mock_erp` in one terminal; open http://127.0.0.1:5055 | "A simulated company AP system with real forms, CSRF, sessions and validation. The agent can only use it through a browser-like interface." |
| 0:20 | `ls workspace/inbox` (after `python -m scripts.make_data`) | "Inbox with traps: newest *file* is not the newest *invoice*, there's a credit note, three layouts." |
| 0:40 | `python -m worker demo happy` | "One sentence in. It plans, searches, reads PDFs, picks the right invoice by document date, logs in using secret placeholders, checks for duplicates, fills the form, then **verifies by reading the record back**." |
| 1:30 | `python -m worker demo resilient` | "Same task, but I inject three faults: flaky 503s, session expiry mid-submit, and the nasty one - the server saves the bill then returns 500. Watch: retry with backoff, re-login with a fresh CSRF token, and **no blind retry**: it reads the list, sees the bill exists, and does not duplicate it." |
| 2:15 | `python -m worker demo clarify` then `... approval_no` | "'Northwind' matches two vendors, so it asks instead of guessing. A high-value bill needs human approval; I say no and nothing is written." |
| 2:50 | `python -m worker run "Make a report listing all bills in AcmePay and save it to out/bills_report.md"` | "Different task, zero code changes in the harness." |
| 3:15 | open `runs/<id>/report.md` | "Evidence, side-effect ledger, recovery events." |
| 3:30 | `python -m worker eval` and `python -m pytest -q` | "Evals assert on the ERP database, not on what the agent claims." |
| 3:50 | (if you have a key) `ANTHROPIC_API_KEY=... python -m worker run "<any new phrasing>"` | "Same harness, Claude as the brain." |
