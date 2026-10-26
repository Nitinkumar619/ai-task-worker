"""A text-mode browser.

Why not Playwright/pixels? For a prototype whose job is to *reliably* operate web
apps, a DOM-derived snapshot (numbered links, typed form fields with options) is
cheaper, deterministic, testable, and far less brittle than screenshot clicking.
The same Browser interface could be backed by Playwright for JS-heavy sites.

Safety properties implemented here:
  * host allow-list (the agent cannot wander off to arbitrary sites)
  * secrets are substituted at the last moment ({{secret:NAME}}); the LLM never sees them
  * GETs are retried with backoff on transient failure; POSTs are NEVER blindly retried
  * form submission validates field names / select options and returns actionable errors
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString

from .tools import AmbiguousWriteError, BlockedError, ToolError, TransientError, WriteAckedError

SECRET_RE = re.compile(r"\{\{secret:([A-Z0-9_]+)\}\}")
MAX_TEXT = 3500


@dataclass
class Field:
    name: str
    type: str
    label: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)  # (value, label)
    value: str = ""


@dataclass
class Form:
    id: str
    method: str
    action: str
    fields: list[Field]
    hidden: dict[str, str]


@dataclass
class Page:
    url: str
    status: int
    title: str
    text: str
    links: list[tuple[str, str, str]]   # (id, text, absolute href)
    forms: list[Form]
    html: str = ""

    @property
    def needs_login(self) -> bool:
        return any(f.type == "password" for form in self.forms for f in form.fields)

    def render(self) -> str:
        out = [f"URL: {self.url}  (HTTP {self.status})", f"TITLE: {self.title}"]
        if self.needs_login:
            out.append("NOTE: this page is a LOGIN form. Authenticate with the secret placeholders.")
        out += ["--- PAGE TEXT ---", self.text, "--- LINKS ---"]
        out += [f"[{i}] {t} -> {h}" for i, t, h in self.links] or ["(none)"]
        out.append("--- FORMS ---")
        for f in self.forms:
            out.append(f"[{f.id}] {f.method} {f.action}")
            for fl in f.fields:
                extra = ""
                if fl.options:
                    extra = " options=" + "|".join(v if v == l else f"{v}" for v, l in fl.options)
                dflt = f" default={fl.value!r}" if fl.value else ""
                out.append(f"    - {fl.name} ({fl.type}){' label=' + repr(fl.label) if fl.label else ''}{dflt}{extra}")
            if f.hidden:
                out.append(f"    (+{len(f.hidden)} hidden field(s) are included automatically)")
        if not self.forms:
            out.append("(none)")
        return "\n".join(out)


def parse_page(url: str, status: int, html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    title = (soup.title.string or "").strip() if soup.title else ""
    # links first (nav included), then strip nav chrome from the text
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        key = (a.get_text(strip=True), href)
        if key in seen or not key[0]:
            continue
        seen.add(key)
        links.append((f"L{len(links) + 1}", key[0], href))
    forms: list[Form] = []
    for i, fm in enumerate(soup.find_all("form"), 1):
        fields, hidden = [], {}
        for el in fm.find_all(["input", "select", "textarea"]):
            name, typ = el.get("name"), (el.get("type") or el.name).lower()
            if not name or typ in ("submit", "button"):
                continue
            if typ == "hidden":
                hidden[name] = el.get("value", "")
                continue
            lab = el.find_previous("label")
            label = lab.get_text(strip=True) if lab and lab.find_next(["input", "select", "textarea"]) is el else ""
            if el.name == "select":
                opts = [(o.get("value", o.get_text(strip=True)), o.get_text(strip=True)) for o in el.find_all("option")]
                sel = el.find("option", selected=True)
                val = sel.get("value", "") if sel else (opts[0][0] if opts else "")
                fields.append(Field(name, "select", label, [o for o in opts if o[0]], val))
            else:
                fields.append(Field(name, typ, label, [], el.get("value", "")))
        forms.append(Form(f"F{i}", (fm.get("method") or "get").upper(), urljoin(url, fm.get("action") or url), fields, hidden))
        fm.replace_with(NavigableString(f"\n[form F{i} - see FORMS]\n"))
    for t in soup.find_all(["header", "nav"]):
        t.decompose()
    for tb in soup.find_all("table"):
        rows = ["| " + " | ".join(c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])) + " |"
                for tr in tb.find_all("tr")]
        tb.replace_with(NavigableString("\n" + "\n".join(rows) + "\n"))
    text = re.sub(r"\n\s*\n+", "\n", soup.get_text("\n", strip=True))
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n...[truncated {len(text) - MAX_TEXT} chars]"
    return Page(url, status, title, text, links, forms, html)


class Browser:
    def __init__(self, allowed_hosts: set[str], secrets: dict[str, str], retries: int = 3,
                 backoff: float = 0.2, timeout: float = 10, on_retry=None):
        self.session = requests.Session()
        self.allowed, self.secrets = allowed_hosts, secrets
        self.retries, self.backoff, self.timeout = retries, backoff, timeout
        self.page: Page | None = None
        self.on_retry = on_retry or (lambda msg: None)

    # ---- helpers ----
    def _check(self, url: str) -> None:
        if urlparse(url).netloc not in self.allowed:
            raise BlockedError(f"Host not allowed: {urlparse(url).netloc}. Allowed: {sorted(self.allowed)}")

    def resolve_secrets(self, s: str) -> str:
        def sub(m):
            if m.group(1) not in self.secrets:
                raise ToolError(f"Unknown secret {m.group(1)}. Available: {sorted(self.secrets)}")
            return self.secrets[m.group(1)]
        return SECRET_RE.sub(sub, s)

    def _build(self, resp: requests.Response) -> Page:
        self.page = parse_page(resp.url, resp.status_code, resp.text)
        return self.page

    # ---- public API ----
    def open(self, url: str) -> Page:
        url = urljoin(self.page.url, url) if self.page else url
        self._check(url)
        last = ""
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                if resp.status_code in (500, 502, 503, 504):
                    last = f"HTTP {resp.status_code}"
                else:
                    self._check(resp.url)
                    return self._build(resp)
            except requests.RequestException as e:
                last = type(e).__name__
            if attempt < self.retries:
                self.on_retry(f"GET {url} failed ({last}); retry {attempt + 1}/{self.retries}")
                time.sleep(self.backoff * (2 ** attempt))
        raise TransientError(f"GET {url} kept failing after {self.retries} retries ({last}).")

    def click(self, link_id: str) -> Page:
        if not self.page:
            raise ToolError("No page is open. Use web_open first.")
        for i, _t, href in self.page.links:
            if i.lower() == link_id.lower().strip("[]"):
                return self.open(href)
        raise ToolError(f"No link {link_id} on the current page. Links: {[l[0] for l in self.page.links]}")

    def submit(self, form_id: str, values: dict[str, str]) -> Page:
        if not self.page:
            raise ToolError("No page is open. Use web_open first.")
        form = next((f for f in self.page.forms if f.id.lower() == form_id.lower().strip("[]")), None)
        if not form:
            raise ToolError(f"No form {form_id} on current page (page changes IDs on every load). Forms: {[f.id for f in self.page.forms]}")
        payload = dict(form.hidden)
        by_name = {f.name: f for f in form.fields}
        for f in form.fields:
            payload[f.name] = f.value
        for k, v in values.items():
            if k not in by_name:
                raise ToolError(f"Form {form.id} has no field '{k}'. Fields: {list(by_name)}")
            v = self.resolve_secrets(str(v))
            fld = by_name[k]
            if fld.type == "select":
                m = next((o for o in fld.options if v == o[0]), None) or \
                    next((o for o in fld.options if v.strip().lower() in (o[0].lower(), o[1].lower())), None)
                if not m:
                    raise ToolError(f"'{v}' is not a valid option for '{k}'. Options: {[o[0] for o in fld.options]}")
                v = m[0]
            payload[k] = v
        self._check(form.action)
        try:
            if form.method == "GET":
                resp = self.session.get(form.action, params=payload, timeout=self.timeout)
            else:
                # Do not auto-follow: the POST itself must be classified from ITS OWN response (post/redirect/get).
                resp = self.session.post(form.action, data=payload, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise AmbiguousWriteError(f"{type(e).__name__} while submitting to {form.action}: the request may or may not have been processed.")
        if resp.status_code >= 500:
            self._build(resp)
            raise AmbiguousWriteError(f"Server returned HTTP {resp.status_code} for {form.method} {form.action}. "
                                      f"The write may or may not have been applied.", resp.status_code)
        if 300 <= resp.status_code < 400 and resp.headers.get("Location"):
            target = urljoin(resp.url, resp.headers["Location"])
            try:
                return self.open(target)       # retrying read path
            except TransientError as e:
                raise WriteAckedError(f"The server acknowledged the submission (HTTP {resp.status_code}) but the follow-up page "
                                      f"{target} failed to load ({e}). The write most likely succeeded - verify by reading state.")
        self._check(resp.url)
        return self._build(resp)

    def fetch_fresh(self, url: str) -> Page:
        """Independent read used by the verifier. Same session (needs auth) but never mutates self.page."""
        self._check(url)
        saved = self.page
        try:
            return self.open(url)
        finally:
            self.page = saved
