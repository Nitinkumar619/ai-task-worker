"""Human-in-the-loop channels. The agent core only sees ask(kind, prompt, options) -> str | None."""
from __future__ import annotations


class TerminalAsker:
    def ask(self, kind: str, prompt: str, options=None):
        print(f"\n\033[1;33m{'APPROVAL NEEDED' if kind == 'approval' else 'QUESTION FROM AGENT'}\033[0m\n{prompt}")
        if options:
            for i, o in enumerate(options, 1):
                print(f"   {i}) {o}")
        try:
            ans = input("> ").strip()
        except EOFError:
            return None
        if options and ans.isdigit() and 1 <= int(ans) <= len(options):
            ans = options[int(ans) - 1]
        return ans or None


class ScriptedAsker:
    """Non-interactive: for demos, evals, CI. Answers questions from a queue; approvals follow `approve`."""

    def __init__(self, answers: list[str] | None = None, approve: bool | None = None):
        self.answers, self.approve = list(answers or []), approve
        self.log: list[tuple[str, str, str | None]] = []

    def ask(self, kind: str, prompt: str, options=None):
        if kind == "approval":
            ans = None if self.approve is None else ("yes" if self.approve else "no")
        else:
            ans = self.answers.pop(0) if self.answers else None
        self.log.append((kind, prompt, ans))
        return ans
