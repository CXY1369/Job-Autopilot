from autojobagent.core.browser_manager import BrowserManager
from autojobagent.core.vision_agent import (
    BrowserAgent,
    AgentAction,
    evaluate_progression_block_reason,
)
from autojobagent.core.ui_snapshot import SnapshotItem


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
        "required_empty_count": 1,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 0,
        "error_snippets": [],
        "invalid_field_samples": [{"type": "file", "name": "Resume"}],
        "required_empty_samples": [{"type": "file", "name": "Resume"}],
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
    assert evidence.get("gate_decision") == "allow"
    assert evidence.get("allowed_by") == "file_only_invalid_with_upload_ready"


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
    assert evidence.get("gate_decision") == "block"
    assert evidence.get("blocked_by") == "invalid_field_count"


def test_progression_gate_blocks_when_non_file_required_empty_exists():
    evidence = {
        "invalid_field_count": 1,
        "required_empty_count": 2,
        "error_container_hits": 0,
        "local_error_keyword_hits": 0,
        "red_error_hits": 0,
        "global_error_keyword_hits": 0,
        "error_snippets": [],
        "invalid_field_samples": [{"type": "file", "name": "Resume"}],
        "required_empty_samples": [
            {"type": "file", "name": "Resume"},
            {"type": "text", "name": "Email"},
        ],
        "submit_candidates": [
            {
                "text": "Submit Application",
                "disabled": False,
                "aria_disabled": "",
                "type": "",
            }
        ],
        "file_upload_state_samples": [
            {"has_replace_text": True, "has_uploaded_file_name": True}
        ],
    }
    reason = evaluate_progression_block_reason(
        evidence, llm_confirms_context_error=False
    )
    assert reason is not None
    assert evidence.get("gate_decision") == "block"


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


def test_build_alternate_action_selects_other_submit_button(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o", "gpt-4o-mini"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent._last_snapshot_map = {
        "e8": SnapshotItem(ref="e8", role="button", name="Submit Application", nth=0),
        "e9": SnapshotItem(ref="e9", role="button", name="Submit", nth=1),
    }
    action = AgentAction(action="click", ref="e8", selector="Submit Application")
    alt = agent._build_alternate_action(action)
    assert alt is not None
    assert alt.ref == "e9"


def test_sanitize_simplify_claims_when_unavailable(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    job = _DummyJob()
    job.simplify_state = "unavailable"
    agent = BrowserAgent(page=object(), job=job)
    text = "页面为申请表单，Simplify 已自动填写完成。"
    sanitized = agent._sanitize_simplify_claims(text)
    assert sanitized is not None
    assert "Simplify" not in sanitized


def test_fingerprint_changes_when_checkbox_toggled(monkeypatch):
    """Fingerprint must differ when a checkbox changes from unchecked to checked."""
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    base_items = {
        "e1": SnapshotItem(
            ref="e1", role="checkbox", name="Boston", nth=0, checked=False
        ),
        "e2": SnapshotItem(
            ref="e2", role="textbox", name="Name", nth=0, value_hint="Xingyu"
        ),
    }
    fp_unchecked = agent._build_page_fingerprint("https://example.com", base_items)

    toggled_items = {
        "e1": SnapshotItem(
            ref="e1", role="checkbox", name="Boston", nth=0, checked=True
        ),
        "e2": SnapshotItem(
            ref="e2", role="textbox", name="Name", nth=0, value_hint="Xingyu"
        ),
    }
    fp_checked = agent._build_page_fingerprint("https://example.com", toggled_items)

    assert fp_unchecked != fp_checked, "Fingerprints must differ after checkbox toggle"


def test_answer_binding_click_prefers_question_context(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    payload = {"ok": True, "reason": "clicked_in_question_container"}
    monkeypatch.setattr(
        agent,
        "_click_answer_with_question_binding",
        lambda question, answer: payload,
    )
    monkeypatch.setattr(
        agent,
        "_verify_question_answer_state",
        lambda question, expected: True,
    )
    action = AgentAction(
        action="click",
        selector="Yes",
        target_question="Are you legally authorized to work in the United States?",
    )
    assert agent._try_answer_binding_click(action) is True


def test_answer_click_verification_fails_without_state_change(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    monkeypatch.setattr(
        agent,
        "_verify_question_answer_state",
        lambda question, expected: False,
    )
    action = AgentAction(
        action="click",
        selector="Yes",
        target_question="Are you legally authorized to work in the United States?",
    )
    item = SnapshotItem(ref="e1", role="button", name="Yes", nth=0)
    assert agent._verify_ref_action_effect(action, locator=object(), item=item) is False


def test_semantic_loop_guard_escalates_replan_alternate_stop(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    action = AgentAction(
        action="click",
        selector="Yes",
        target_question="Are you legally authorized to work in the United States?",
    )
    key = agent._semantic_action_key("fp1", action)
    assert key
    agent._semantic_fail_counts[key] = 1
    assert agent._semantic_loop_guard_decision("fp1", action) == "replan"
    agent._semantic_fail_counts[key] = 2
    assert agent._semantic_loop_guard_decision("fp1", action) == "alternate"
    agent._semantic_fail_counts[key] = 3
    assert agent._semantic_loop_guard_decision("fp1", action) == "stop"


def test_fingerprint_changes_when_input_value_changes(monkeypatch):
    """Fingerprint must differ when an input value changes."""
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    items_empty = {
        "e1": SnapshotItem(
            ref="e1", role="textbox", name="Email", nth=0, value_hint=""
        ),
    }
    items_filled = {
        "e1": SnapshotItem(
            ref="e1", role="textbox", name="Email", nth=0, value_hint="user@example.com"
        ),
    }
    fp_empty = agent._build_page_fingerprint("https://example.com", items_empty)
    fp_filled = agent._build_page_fingerprint("https://example.com", items_filled)
    assert fp_empty != fp_filled, "Fingerprints must differ when value changes"


def test_fingerprint_stable_for_same_state(monkeypatch):
    """Fingerprint must be identical for same element state."""
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    items = {
        "e1": SnapshotItem(
            ref="e1", role="checkbox", name="Boston", nth=0, checked=True
        ),
    }
    fp1 = agent._build_page_fingerprint("https://example.com", items)
    fp2 = agent._build_page_fingerprint("https://example.com", items)
    assert fp1 == fp2, "Same state must produce same fingerprint"
