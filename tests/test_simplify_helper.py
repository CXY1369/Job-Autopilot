from autojobagent.core import simplify_helper
from autojobagent.core.simplify_helper import (
    SimplifyResult,
    SimplifyState,
    probe_simplify_state,
    run_simplify,
)


class _FakeFrame:
    def __init__(self, text: str):
        self._text = text

    def inner_text(self, _selector: str) -> str:
        return self._text


class _FakePage:
    def __init__(self, texts: list[str]):
        self.main_frame = _FakeFrame(texts[0] if texts else "")
        self.frames = [_FakeFrame(t) for t in texts[1:]]


def test_probe_simplify_state_ready():
    page = _FakePage(["Autofill this page"])
    state = probe_simplify_state(page)
    assert state.status == "ready"


def test_probe_simplify_state_running():
    page = _FakePage(["Filling 3 of 10 unique questions..."])
    state = probe_simplify_state(page)
    assert state.status == "running"


def test_probe_simplify_state_completed():
    page = _FakePage(["Autofill complete!"])
    state = probe_simplify_state(page)
    assert state.status == "completed"


def test_probe_simplify_state_unavailable():
    page = _FakePage(["No simplify widgets here"])
    state = probe_simplify_state(page)
    assert state.status == "unavailable"


def test_run_simplify_returns_immediately_when_already_completed(monkeypatch):
    page = _FakePage(["any text"])

    monkeypatch.setattr(
        simplify_helper,
        "probe_simplify_state",
        lambda _p: SimplifyState(
            status="completed",
            message="already done",
            observations=["probe:complete_text"],
        ),
    )

    result = run_simplify(page)
    assert isinstance(result, SimplifyResult)
    assert result.found is True
    assert result.autofilled is True
    assert "already done" in (result.message or "")
