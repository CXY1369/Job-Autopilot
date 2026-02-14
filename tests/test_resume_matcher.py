from __future__ import annotations

from autojobagent.core import resume_matcher


def test_choose_best_resume_empty_candidates():
    result = resume_matcher.choose_best_resume_for_jd(
        jd_text="backend engineer",
        candidates=[],
    )
    assert result.selected_resume_path is None
    assert result.score == 0
    assert result.reason == "no resume candidates"
    assert result.candidates_count == 0


def test_choose_best_resume_single_candidate():
    only = "/tmp/resume_backend.pdf"
    result = resume_matcher.choose_best_resume_for_jd(
        jd_text="python backend engineer role",
        candidates=[only],
    )
    assert result.selected_resume_path == only
    assert result.score == 100
    assert result.reason == "single candidate only"
    assert result.candidates_count == 1


def test_choose_best_resume_llm_error_falls_back_to_heuristic(monkeypatch):
    class _BrokenCompletions:
        @staticmethod
        def create(**_kwargs):
            raise RuntimeError("llm unavailable")

    class _BrokenChat:
        completions = _BrokenCompletions()

    class _BrokenOpenAI:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.chat = _BrokenChat()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(resume_matcher, "OpenAI", _BrokenOpenAI, raising=True)

    candidates = [
        "/tmp/alex_backend_engineer_resume.pdf",
        "/tmp/alex_sales_resume.pdf",
    ]
    result = resume_matcher.choose_best_resume_for_jd(
        jd_text="We are hiring a backend engineer with Python experience.",
        candidates=candidates,
    )
    assert result.selected_resume_path == candidates[0]
    assert result.reason == "heuristic filename keyword overlap"
    assert result.score >= 55
    assert result.candidates_count == 2
