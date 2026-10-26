"""Independent verification of completion claims.

The agent does not get to say "done". It must submit *claims*: a read-only check (a web page or a file)
plus literal strings that must be present. The harness performs the check itself with a fresh read and
stores the observed page as evidence. This is the main defence against hallucinated success.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .tools import ToolError


@dataclass
class ClaimResult:
    statement: str
    ok: bool
    detail: str
    evidence: str = ""
    source: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_claims(claims: list[dict], ctx) -> list[ClaimResult]:
    results = []
    for c in claims:
        stmt, chk, expect = c.get("statement", "?"), c.get("check") or {}, c.get("expect") or []
        try:
            if chk.get("type") == "web":
                page = ctx.browser.fetch_fresh(chk["url"])
                if page.needs_login:
                    results.append(ClaimResult(stmt, False, "Check page is a login form (session expired). Log in again, then retry finish.", source=chk["url"]))
                    continue
                if page.status >= 400:
                    results.append(ClaimResult(stmt, False, f"Check page returned HTTP {page.status}.", page.render(), chk["url"]))
                    continue
                observed, source = page.render(), page.url
            elif chk.get("type") == "file":
                observed, source = ctx.files.read(chk["path"]), chk["path"]
            else:
                results.append(ClaimResult(stmt, False, "check.type must be 'web' or 'file'."))
                continue
        except ToolError as e:
            results.append(ClaimResult(stmt, False, f"Check could not be performed: {e}", source=str(chk)))
            continue
        hay = _norm(observed)
        missing = [e for e in expect if _norm(str(e)) not in hay]
        if not expect:
            results.append(ClaimResult(stmt, False, "Claim has no 'expect' strings, so it verifies nothing.", observed[:800], source))
        elif missing:
            results.append(ClaimResult(stmt, False, f"Expected text not found at {source}: {missing}", observed[:800], source))
        else:
            results.append(ClaimResult(stmt, True, f"All {len(expect)} expected values found at {source}.", observed[:1500], source))
    return results
