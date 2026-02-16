from autojobagent.core.browser_manager import BrowserManager
from autojobagent.core.vision_agent import (
    BrowserAgent,
    evaluate_progression_block_reason,
)


class _DummyJob:
    id = 999
    resume_used = None


def test_progression_gate_does_not_block_job_description_keywords_only():
    evidence = {
        "invalid_field_count": 0,
        "required_empty_count": 0,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 1,  # 例如“required skills”在岗位描述中出现
        "error_snippets": [],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=False
    )
    assert reason is None


def test_progression_gate_blocks_invalid_field():
    evidence = {
        "invalid_field_count": 2,
        "required_empty_count": 0,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 0,
        "error_snippets": [],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=False
    )
    assert reason is not None


def test_progression_gate_allows_file_invalid_with_uploaded_signal():
    evidence = {
        "invalid_field_count": 1,
        "required_empty_count": 0,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 0,
        "error_snippets": [],
        "invalid_field_samples": [{"type": "file", "name": "Resume"}],
        "submit_candidates": [
            {
                "text": "Submit Application",
                "disabled": False,
                "aria_disabled": "",
                "type": "",
            }
        ],
        "file_upload_state_samples": [
            {"has_replace_text": True, "has_uploaded_file_name": False}
        ],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=False
    )
    assert reason is None
    assert evidence.get("allowed_by_file_upload_state") is True


def test_progression_gate_blocks_file_invalid_without_uploaded_signal():
    evidence = {
        "invalid_field_count": 1,
        "required_empty_count": 0,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 0,
        "error_snippets": [],
        "invalid_field_samples": [{"type": "file", "name": "Resume"}],
        "submit_candidates": [
            {
                "text": "Submit Application",
                "disabled": False,
                "aria_disabled": "",
                "type": "",
            }
        ],
        "file_upload_state_samples": [
            {"has_replace_text": False, "has_uploaded_file_name": False}
        ],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=False
    )
    assert reason is not None


def test_progression_gate_blocks_error_container_with_red_signal():
    evidence = {
        "invalid_field_count": 0,
        "required_empty_count": 0,
        "error_container_hits": 1,
        "local_error_keyword_hits": 0,
        "red_error_hits": 1,
        "global_error_keyword_hits": 1,
        "error_snippets": ["Please complete this required field."],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=False
    )
    assert reason is not None


def test_progression_gate_uses_llm_for_ambiguous_global_keywords():
    evidence = {
        "invalid_field_count": 0,
        "required_empty_count": 0,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 2,
        "error_snippets": [],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=True
    )
    assert reason is not None


def test_intent_model_follows_fallback_order(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o", "gpt-4o-mini"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    assert agent.intent_model == "gpt-4o"


def test_intent_model_respects_explicit_override(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {
            "llm": {
                "fallback_models": ["gpt-4o", "gpt-4o-mini"],
                "intent_model": "gpt-4o-mini",
            }
        },
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    assert agent.intent_model == "gpt-4o-mini"
