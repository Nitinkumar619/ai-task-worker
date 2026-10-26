"""Sandboxed file workspace. Reads anywhere under workspace/, writes only under workspace/out/."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader

from .tools import BlockedError, ToolError

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".html"}


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, rel: str, write: bool = False) -> Path:
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root):
            raise BlockedError(f"Path escapes the workspace: {rel}")
        if write and not p.is_relative_to(self.root / "out"):
            raise BlockedError("Writes are only allowed under out/ (e.g. out/report.md).")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root))

    def list(self, directory: str = ".") -> list[dict]:
        d = self.resolve(directory)
        if not d.is_dir():
            raise ToolError(f"Not a directory: {directory}")
        return [{"path": self.rel(f), "size": f.stat().st_size,
                 "modified": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")}
                for f in sorted(d.iterdir()) if f.is_file()]

    def read(self, path: str) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"No such file: {path}")
        if p.suffix.lower() == ".pdf":
            try:
                return "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages).strip()
            except Exception as e:  # corrupt / scanned PDFs
                raise ToolError(f"Could not extract text from {path}: {e}")
        if p.suffix.lower() in TEXT_EXT or p.stat().st_size < 200_000:
            return p.read_text(errors="replace")
        raise ToolError(f"Unsupported or too large file type: {p.suffix}")

    def search(self, query: str, directory: str = ".") -> list[dict]:
        q, out = query.lower(), []
        for root, _d, files in __import__("os").walk(self.resolve(directory)):
            for fn in sorted(files):
                p = Path(root) / fn
                try:
                    text = self.read(self.rel(p))
                except ToolError:
                    continue
                if q in fn.lower():
                    out.append({"path": self.rel(p), "line": f"(filename match) {fn}"})
                for line in text.splitlines():
                    if q in line.lower():
                        out.append({"path": self.rel(p), "line": line.strip()[:160]})
                        break
        return out

    def write(self, path: str, content: str) -> Path:
        p = self.resolve(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p
