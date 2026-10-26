import pytest

from mock_erp.app import create_app, serve_in_thread
from scripts import make_data
from worker.agent import Decision, ToolCall
from worker.config import Config


class GenBrain:
    """Test brain driven by a generator: `r = yield call(...)` returns the previous ToolResult."""
    def __init__(self, fn):
        self.fn, self.gen = fn, None

    def decide(self, state):
        if self.gen is None:
            self.gen = self.fn()
            return next(self.gen)
        try:
            return self.gen.send(state.last_result)
        except StopIteration:
            return Decision(call=ToolCall("end", "finish", {"outcome": "failed", "summary": "script ended"}))


_n = [0]


def call(name, **args):
    _n[0] += 1
    return Decision(call=ToolCall(f"t{_n[0]}", name, args))


@pytest.fixture
def env(tmp_path):
    def make(faults=None):
        make_data.main(tmp_path / "workspace")
        app = create_app(faults or {})
        url, stop = serve_in_thread(app)
        cfg = Config(erp_base_url=url, workspace=tmp_path / "workspace", runs_dir=tmp_path / "runs", backoff_base=0.01, max_steps=40)
        env.stops.append(stop)
        return app, cfg
    env.stops = []
    yield make
    for s in env.stops:
        s()
