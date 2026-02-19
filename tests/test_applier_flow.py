from dataclasses import dataclass

from autojobagent.core import applier
from autojobagent.core.simplify_helper import SimplifyResult, SimplifyState


@dataclass
class _MatchResult:
    selected_resume_path: str | None
    score: int
    reason: str


class _FakePage:
    def __init__(self, url: str):
        self.url = url

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.url = url

    def wait_for_timeout(self, _ms: int) -> None:
        return None


class _FakeSession:
    def __init__(self, page: _FakePage, simplify_loaded: bool = True):
        self.page = page
        self.simplify_loaded = simplify_loaded
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeManager:
    def __init__(self, log_fn):
        self.log_fn = log_fn
        self._session = _FakeSession(
            _FakePage("https://jobs.ashbyhq.com/foo/bar"), simplify_loaded=True
        )

    def launch(self):
        return self._session


class _Job:
    def __init__(self):
        self.id = 999
        self.title = "Role"
        self.company = "Company"
        self.link = "https://jobs.ashbyhq.com/foo/bar"
        self.resume_used = None


def test_apply_for_job_runs_pre_nav_then_simplify_then_main_agent(monkeypatch):
    call_trace: list[tuple[int, bool]] = []

    monkeypatch.setattr(applier, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(applier, "BrowserManager", _FakeManager)
    monkeypatch.setattr(
        applier, "_save_final_screenshot", lambda *_args, **_kwargs: "x.png"
    )
    monkeypatch.setattr(applier, "extract_jd_text_from_page", lambda _p: "jd text")
    monkeypatch.setattr(applier, "list_upload_candidates", lambda max_files=50: [])
    monkeypatch.setattr(
        applier,
        "choose_best_resume_for_jd",
        lambda **_kwargs: _MatchResult(
            selected_resume_path=None, score=0, reason="n/a"
        ),
    )
    monkeypatch.setattr(
        applier,
        "probe_simplify_state",
        lambda _p: SimplifyState(status="ready", message="ready", observations=[]),
    )
    monkeypatch.setattr(
        applier,
        "run_simplify",
        lambda _p: SimplifyResult(
            found=True, autofilled=True, message="ok", observations=[]
        ),
    )
    metrics = [
        {"required_total": 4, "required_filled": 1, "required_empty": 3},
        {"required_total": 4, "required_filled": 3, "required_empty": 1},
    ]
    monkeypatch.setattr(
        applier,
        "_collect_required_fill_metrics",
        lambda _p: metrics.pop(0),
    )

    observed_states: list[str] = []

    def _fake_run_browser_agent(page, job, max_steps=50, pre_nav_only=False):
        call_trace.append((max_steps, pre_nav_only))
        observed_states.append(getattr(job, "simplify_state", "missing"))
        if pre_nav_only:
            page.url = f"{job.link}/application"
        return True

    monkeypatch.setattr(applier, "run_browser_agent", _fake_run_browser_agent)

    result = applier.apply_for_job(_Job())

    assert result.success is True
    assert call_trace[0] == (8, True)
    assert call_trace[1] == (50, False)
    assert observed_states[-1] == "completed"


def test_apply_for_job_sets_unavailable_simplify_state_for_agent(monkeypatch):
    states_seen: list[str] = []

    monkeypatch.setattr(applier, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(applier, "BrowserManager", _FakeManager)
    monkeypatch.setattr(
        applier, "_save_final_screenshot", lambda *_args, **_kwargs: "x.png"
    )
    monkeypatch.setattr(applier, "extract_jd_text_from_page", lambda _p: "jd text")
    monkeypatch.setattr(applier, "list_upload_candidates", lambda max_files=50: [])
    monkeypatch.setattr(
        applier,
        "choose_best_resume_for_jd",
        lambda **_kwargs: _MatchResult(
            selected_resume_path=None, score=0, reason="n/a"
        ),
    )
    monkeypatch.setattr(
        applier,
        "probe_simplify_state",
        lambda _p: SimplifyState(
            status="unavailable",
            message="Simplify controls not detected",
            observations=[],
        ),
    )

    def _fake_run_browser_agent(page, job, max_steps=50, pre_nav_only=False):
        states_seen.append(getattr(job, "simplify_state", "missing"))
        if pre_nav_only:
            page.url = f"{job.link}/application"
        return True

    monkeypatch.setattr(applier, "run_browser_agent", _fake_run_browser_agent)

    result = applier.apply_for_job(_Job())

    assert result.success is True
    assert states_seen[-1] == "unavailable"


def test_apply_for_job_preserves_structured_manual_reason(monkeypatch):
    monkeypatch.setattr(applier, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(applier, "BrowserManager", _FakeManager)
    monkeypatch.setattr(
        applier, "_save_final_screenshot", lambda *_args, **_kwargs: "x.png"
    )
    monkeypatch.setattr(applier, "extract_jd_text_from_page", lambda _p: "jd text")
    monkeypatch.setattr(applier, "list_upload_candidates", lambda max_files=50: [])
    monkeypatch.setattr(
        applier,
        "choose_best_resume_for_jd",
        lambda **_kwargs: _MatchResult(
            selected_resume_path=None, score=0, reason="n/a"
        ),
    )
    monkeypatch.setattr(
        applier,
        "probe_simplify_state",
        lambda _p: SimplifyState(status="unavailable", message="n/a", observations=[]),
    )

    def _fake_run_browser_agent(page, job, max_steps=50, pre_nav_only=False):
        if pre_nav_only:
            page.url = f"{job.link}/application"
            return True
        job.manual_reason_hint = (
            "同一语义动作重复失败达到上限；动作=click:Yes；最近门控: "
            "检测到表单错误提示（错误容器/红色文本）"
        )
        job.failure_class_hint = "validation_error"
        job.failure_code_hint = "missing_required_field"
        job.retry_count_hint = 2
        job.last_error_snippet_hint = "Missing country required field"
        job.last_outcome_class_hint = "validation_error"
        return False

    monkeypatch.setattr(applier, "run_browser_agent", _fake_run_browser_agent)

    result = applier.apply_for_job(_Job())

    assert result.success is False
    assert result.manual_required is True
    assert result.manual_reason is not None
    assert "同一语义动作重复失败达到上限" in result.manual_reason
    assert result.failure_class == "validation_error"
    assert result.failure_code == "missing_required_field"
    assert result.retry_count == 2
    assert result.last_error_snippet == "Missing country required field"
    assert result.last_outcome_class == "validation_error"


def test_apply_for_job_degrades_when_simplify_no_effect_delta(monkeypatch):
    states_seen: list[str] = []
    verified_seen: list[bool] = []

    monkeypatch.setattr(applier, "_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(applier, "BrowserManager", _FakeManager)
    monkeypatch.setattr(
        applier, "_save_final_screenshot", lambda *_args, **_kwargs: "x.png"
    )
    monkeypatch.setattr(applier, "extract_jd_text_from_page", lambda _p: "jd text")
    monkeypatch.setattr(applier, "list_upload_candidates", lambda max_files=50: [])
    monkeypatch.setattr(
        applier,
        "choose_best_resume_for_jd",
        lambda **_kwargs: _MatchResult(
            selected_resume_path=None, score=0, reason="n/a"
        ),
    )
    monkeypatch.setattr(
        applier,
        "probe_simplify_state",
        lambda _p: SimplifyState(status="ready", message="ready", observations=[]),
    )
    monkeypatch.setattr(
        applier,
        "run_simplify",
        lambda _p: SimplifyResult(
            found=True, autofilled=True, message="ok", observations=[]
        ),
    )
    metrics = [
        {"required_total": 4, "required_filled": 2, "required_empty": 2},
        {"required_total": 4, "required_filled": 2, "required_empty": 2},
    ]
    monkeypatch.setattr(
        applier,
        "_collect_required_fill_metrics",
        lambda _p: metrics.pop(0),
    )

    def _fake_run_browser_agent(page, job, max_steps=50, pre_nav_only=False):
        states_seen.append(getattr(job, "simplify_state", "missing"))
        verified_seen.append(bool(getattr(job, "assist_prefill_verified", False)))
        if pre_nav_only:
            page.url = f"{job.link}/application"
        return True

    monkeypatch.setattr(applier, "run_browser_agent", _fake_run_browser_agent)

    result = applier.apply_for_job(_Job())

    assert result.success is True
    assert states_seen[-1] == "ready"
    assert verified_seen[-1] is False
