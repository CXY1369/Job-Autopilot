from autojobagent.core.browser_manager import BrowserManager
from autojobagent.core.vision_agent import (
    BrowserAgent,
    AgentAction,
    AgentState,
    SubmissionOutcome,
    VisualAuditResult,
    evaluate_progression_block_reason,
)
from autojobagent.core.macro_tasks import MacroTask
from autojobagent.core.semantic_tree import OptionNode, QuestionBlock
from autojobagent.core.ui_snapshot import SnapshotItem
from autojobagent.core.llm_runtime import LLMCallResult
from autojobagent.core.failure_memory import FailureMemoryStore


class _DummyJob:
    id = 999
    resume_used = None


class _DummyKeyboard:
    def press(self, _key: str) -> None:
        return None


class _OutcomePage:
    def __init__(
        self, text: str, url: str = "https://jobs.ashbyhq.com/company/role/application"
    ):
        self._text = text
        self.url = url
        self.keyboard = _DummyKeyboard()

    def inner_text(self, _selector: str) -> str:
        return self._text

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _script: str):
        return None


class _ObservePage:
    def __init__(self, text: str, url: str):
        self._text = text
        self.url = url
        self.keyboard = _DummyKeyboard()

    def inner_text(self, _selector: str) -> str:
        return self._text

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _script: str):
        return None


class _QuestionStatePage:
    def __init__(self, selected_by_question: dict[str, list[str]]):
        self._selected_by_question = selected_by_question
        self.url = "https://jobs.ashbyhq.com/suno/role/application"
        self.keyboard = _DummyKeyboard()

    def inner_text(self, _selector: str) -> str:
        return "application page"

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def evaluate(self, _script: str, payload: dict | None = None):
        if not isinstance(payload, dict):
            return None
        question = str(payload.get("question") or "")
        expected = str(payload.get("expected") or "")
        selected = list(self._selected_by_question.get(question, []))
        expected_lower = expected.lower().strip()
        option_found = True if not expected_lower else any(
            expected_lower in s.lower() or s.lower() in expected_lower for s in selected
        )
        return {
            "matched": bool(question),
            "option_found": option_found,
            "option_selected": option_found,
            "selected": selected,
        }


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
    agent = BrowserAgent(
        page=_ObservePage(
            "application page", "https://jobs.ashbyhq.com/suno/role/application"
        ),
        job=_DummyJob(),
    )
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
    agent = BrowserAgent(
        page=_ObservePage(
            "application page", "https://jobs.ashbyhq.com/suno/role/application"
        ),
        job=_DummyJob(),
    )
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


def test_submission_outcome_classifier_external_blocked(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage(
        "We couldn't submit your application. Your application submission was flagged as possible spam."
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    outcome = agent._classify_submission_outcome(
        AgentAction(action="click", selector="Submit"), action_success=False
    )
    assert outcome.classification == "external_blocked"


def test_submission_outcome_classifier_validation_error(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage("Please complete required fields")
    agent = BrowserAgent(page=page, job=_DummyJob())
    monkeypatch.setattr(
        agent,
        "_get_progression_block_reason",
        lambda: "检测到 1 个必填字段为空",
    )
    agent._last_progression_block_snippets = ["Missing country required field"]
    outcome = agent._classify_submission_outcome(
        AgentAction(action="click", selector="Submit"), action_success=False
    )
    assert outcome.classification == "validation_error"


def test_submission_retry_policy_stops_at_third_attempt(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage("flagged as possible spam")
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(action="click", selector="Submit Application")
    monkeypatch.setattr(
        agent,
        "_classify_submission_outcome",
        lambda _action, _ok: SubmissionOutcome(
            classification="external_blocked",
            reason_code="anti_spam_flagged",
            evidence_snippet="flagged as possible spam",
        ),
    )
    r1 = agent._handle_submission_outcome(action, False)
    r2 = agent._handle_submission_outcome(action, False)
    r3 = agent._handle_submission_outcome(action, False)
    assert r1 == (False, False)
    assert r2 == (False, False)
    assert r3 == (False, True)


def test_submission_external_blocked_immediately_refreshes(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage("flagged as possible spam")
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(action="click", selector="Submit Application")
    monkeypatch.setattr(
        agent,
        "_classify_submission_outcome",
        lambda _action, _ok: SubmissionOutcome(
            classification="external_blocked",
            reason_code="anti_spam_or_risk_blocked",
            evidence_snippet="flagged as possible spam",
        ),
    )
    refresh_triggers: list[str] = []
    monkeypatch.setattr(
        agent,
        "_do_refresh",
        lambda trigger="unknown": refresh_triggers.append(trigger) or True,
    )
    paced: list[str] = []
    monkeypatch.setattr(agent, "_apply_humanized_retry_pacing", lambda: paced.append("p"))
    result = agent._handle_submission_outcome(action, False)
    assert result == (False, False)
    assert refresh_triggers == ["external_blocked_immediate_restart"]
    assert paced == []


def test_progression_queue_prefers_submit_over_apply(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    monkeypatch.setattr(agent, "_get_progression_block_reason", lambda: None)
    snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="button", name="Apply Now", nth=0),
        "e2": SnapshotItem(ref="e2", role="button", name="Submit Application", nth=0),
    }
    action = agent._maybe_get_progression_queue_action(snapshot_map)
    assert action is not None
    assert action.ref == "e2"


def test_task_execution_key_supports_non_binary_question_option(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage("body text")
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(
        action="click",
        selector="B",
        target_question="What is your security clearance level?",
    )
    key = agent._task_execution_key("fp-any", action)
    assert "answer::what is your security clearance level?::b" in key


def test_submission_action_skips_semantic_guard(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage("body text")
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(action="click", selector="Submit Application")
    # 即使已有 fail_count，也不应对提交动作触发语义熔断决策
    key = agent._semantic_action_key("fp-one", action)
    agent._semantic_fail_counts[key] = 3
    assert agent._is_submission_click_action(action, item=None) is True


def test_macro_task_identity_key_for_question_multi(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    task = MacroTask(
        task_id="t1",
        task_type="question_multi",
        title="Answer required question",
        question_text="Which office are you willing to work out of?",
        expected_options=["San Francisco", "New York City (Chelsea)"],
    )
    key = agent._macro_task_identity_key(task)
    assert "which office" in key
    assert "san francisco" in key


def test_question_multi_build_action_skips_already_selected_option(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    question = "Which office are you willing to work out of?"
    page = _QuestionStatePage({question: ["San Francisco"]})
    agent = BrowserAgent(page=page, job=_DummyJob())
    block = QuestionBlock(
        question_id="q1",
        question_text=question,
        control_type="choice_group",
        required=True,
        has_error=False,
        options=[
            OptionNode(
                text="San Francisco",
                role="checkbox",
                selected=True,
                ref_id="e1",
            ),
            OptionNode(
                text="New York City (Chelsea)",
                role="checkbox",
                selected=False,
                ref_id="e2",
            ),
        ],
        selected_options=["San Francisco"],
    )
    agent._last_question_blocks = [block]
    task = MacroTask(
        task_id="t6",
        task_type="question_multi",
        title="Answer required question",
        question_text=question,
        expected_options=["San Francisco", "New York City (Chelsea)"],
        status="in_progress",
    )
    action = agent._build_macro_action_for_task(task, snapshot_map={})
    assert action is not None
    assert action.selector == "New York City (Chelsea)"
    assert "San Francisco" in task.completed_options


def test_macro_task_completed_question_single_requires_target_option_state(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=_ObservePage("app", "https://jobs.ashbyhq.com/suno"), job=_DummyJob())
    task = MacroTask(
        task_id="t8",
        task_type="question_single",
        title="Answer required question",
        question_text="Do you require visa sponsorship?",
        expected_options=["Yes"],
    )
    monkeypatch.setattr(
        agent,
        "_find_question_block",
        lambda _task: QuestionBlock(
            question_id="q4",
            question_text="Do you require visa sponsorship?",
            control_type="choice_group",
            required=True,
            has_error=False,
            options=[],
            selected_options=["Yes"],
        ),
    )
    # 即使 selected_options 内有 "Yes"，只要目标选项节点状态未命中，也不得判定完成
    monkeypatch.setattr(agent, "_verify_question_option_state", lambda _q, _o: False)
    assert agent._macro_task_completed(task, snapshot_map={}) is False


def test_verify_question_option_state_uses_option_level_signal(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    question = "Do you require visa sponsorship?"
    page = _QuestionStatePage({question: ["No"]})
    agent = BrowserAgent(page=page, job=_DummyJob())
    assert agent._verify_question_option_state(question, "No") is True
    assert agent._verify_question_option_state(question, "Yes") is False


def test_file_upload_task_is_locked_done_after_success(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("application page", "https://jobs.ashbyhq.com/suno/role/application"),
        job=_DummyJob(),
    )
    task = MacroTask(
        task_id="t5",
        task_type="file_upload",
        title="Upload required resume",
        field_selector="Resume",
        status="in_progress",
    )
    agent._macro_tasks = [task]
    action = AgentAction(
        action="upload",
        selector="Resume",
        reason="[macro:t5] upload required file",
    )
    agent._on_macro_action_result(action, True)
    assert task.status == "done"
    assert agent._macro_upload_completed(task) is True


def test_execute_ref_upload_logs_action_verified(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("application page", "https://jobs.ashbyhq.com/suno/role/application"),
        job=_DummyJob(),
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(agent, "_step_log", lambda event, payload: events.append((event, payload)))
    agent._last_snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="file_input", name="Resume", nth=0, input_type="file")
    }
    monkeypatch.setattr(agent, "_locator_from_snapshot_item", lambda _item: object())
    monkeypatch.setattr(agent, "_do_upload", lambda _action, locator=None: True)
    ok = agent._execute_ref_action(AgentAction(action="upload", ref="e1", selector="Resume"))
    assert ok is True
    assert any(event == "action_verified" and payload.get("action") == "upload" for event, payload in events)


def test_visual_augmentation_dedup_drops_duplicate_optional_prompt(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("app", "https://jobs.ashbyhq.com/suno"),
        job=_DummyJob(),
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent,
        "_step_log",
        lambda event, payload: events.append((event, payload)),
    )
    tasks = [
        MacroTask(
            task_id="t3",
            task_type="field_fill_optional",
            title="Fill optional prompt field from profile",
            field_selector="Why are you interested in working at Suno?",
            target_value="reason",
            mapping_reason="common_answers.why_this_company",
        )
    ]
    audit = VisualAuditResult(
        visual_summary="required question exists",
        required_fields=[],
        required_questions=["Why are you interested in working at Suno?"],
        required_uploads=[],
        source="vision+heuristic",
    )
    merged = agent._augment_macro_tasks_with_visual_audit(
        tasks=tasks,
        audit=audit,
        snapshot_map={},
        question_blocks=[],
    )
    assert len(merged) == 1
    assert all(task.task_type != "inference_required" for task in merged)
    assert any(
        event == "execution_queue_augmented_by_visual_audit"
        and payload.get("dedup_dropped_count") == 1
        for event, payload in events
    )


def test_visual_augmentation_skips_unmatched_required_question(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("app", "https://jobs.ashbyhq.com/suno"),
        job=_DummyJob(),
    )
    tasks: list[MacroTask] = []
    audit = VisualAuditResult(
        visual_summary="required question exists",
        required_fields=[],
        required_questions=["Have you worked on a data engineering initiative 0-1?"],
        required_uploads=[],
        source="vision+heuristic",
    )
    merged = agent._augment_macro_tasks_with_visual_audit(
        tasks=tasks,
        audit=audit,
        snapshot_map={},
        question_blocks=[],
    )
    assert merged == []


def test_macro_task_precondition_timeout_blocks_inference_task(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("app", "https://jobs.ashbyhq.com/suno/role/application"),
        job=_DummyJob(),
    )
    agent._macro_scope = agent._stable_page_scope()
    agent._macro_tasks = [
        MacroTask(
            task_id="t9",
            task_type="inference_required",
            title="Infer answer for unmapped question",
            question_text="Have you worked on a data engineering initiative 0-1?",
            expected_options=["Yes", "No"],
            precondition="question_block_present",
            postcondition="required_question_answered",
        )
    ]
    agent._last_question_blocks = []
    for _ in range(agent._macro_precondition_wait_limit):
        action = agent._maybe_get_macro_action(
            snapshot_map={},
            page_fingerprint="fp",
        )
        assert action is None
    assert agent._macro_tasks[0].status == "blocked"
    assert agent._macro_tasks[0].wait_count == agent._macro_precondition_wait_limit


def test_semantic_key_not_reset_by_page_fingerprint(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage(
        "body text", url="https://jobs.ashbyhq.com/suno/jobs/123/application"
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(action="click", selector="Submit Application")
    k1 = agent._semantic_action_key("fp-one", action)
    k2 = agent._semantic_action_key("fp-two", action)
    assert k1 == k2


def test_semantic_guard_promote_advances_stage(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage(
        "body text", url="https://jobs.ashbyhq.com/suno/jobs/123/application"
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(action="click", selector="Submit Application")
    key = agent._semantic_action_key("fp-one", action)
    assert key
    agent._semantic_fail_counts[key] = 1
    agent._promote_semantic_guard("fp-one", action, stage="replan")
    assert agent._semantic_fail_counts[key] == 2
    assert agent._semantic_loop_guard_decision("fp-one", action) == "alternate"


def test_build_semantic_snapshot_extracts_required_and_submit(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    snapshot_map = {
        "e1": SnapshotItem(
            ref="e1",
            role="textbox",
            name="Email",
            nth=0,
            required=True,
            value_hint="",
        ),
        "e2": SnapshotItem(
            ref="e2",
            role="button",
            name="Submit Application",
            nth=0,
        ),
    }
    semantic = agent._build_semantic_snapshot(
        "https://jobs.ashbyhq.com/suno/1e23/application",
        snapshot_map,
        "Please complete this required field before submit.",
    )
    assert semantic.domain == "jobs.ashbyhq.com"
    assert semantic.normalized_path.startswith("/suno")
    assert len(semantic.required_unfilled) == 1
    assert semantic.submit_candidates
    assert semantic.errors


def test_selector_action_logs_action_verified(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        agent,
        "_step_log",
        lambda event, payload: events.append((event, payload)),
    )
    monkeypatch.setattr(
        agent,
        "_smart_fill",
        lambda selector, value: True,
    )
    ok = agent._execute_action(
        AgentAction(action="fill", selector="Email", value="cxy1368@gmail.com")
    )
    assert ok is True
    assert any(event == "action_executed" for event, _ in events)
    assert any(
        event == "action_verified" and bool(payload.get("ok")) is True
        for event, payload in events
    )


def test_visual_fallback_budget_exhausted(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent.visual_fallback_budget = 1
    agent.visual_fallback_used = 1
    use_vision, reason = agent._should_use_vision_fallback(
        page_state="application_or_form_page",
        snapshot_map={},
        visible_text="",
    )
    assert use_vision is False
    assert reason == "budget_exhausted"


def test_visual_fallback_semantic_only_when_stable(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent.step_count = 6
    snapshot_map = {
        f"e{i}": SnapshotItem(ref=f"e{i}", role="textbox", name=f"Field {i}", nth=i)
        for i in range(1, 9)
    }
    use_vision, reason = agent._should_use_vision_fallback(
        page_state="application_or_form_page",
        snapshot_map=snapshot_map,
        visible_text="normal application form text",
    )
    assert use_vision is False
    assert reason == "semantic_only"


def test_step_screenshot_mode_vision_only(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent.step_screenshot_mode = "vision_only"
    assert agent._should_capture_step_screenshot(use_vision=False) is False
    assert agent._should_capture_step_screenshot(use_vision=True) is True


def test_step_screenshot_mode_off(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent.step_screenshot_mode = "off"
    assert agent._should_capture_step_screenshot(use_vision=True) is False


def test_replay_stuut_external_blocked_stops_with_structured_reason(monkeypatch):
    """回放场景：提交被 anti-spam 阻断，最多 3 次后停止并保留结构化证据。"""
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage(
        "We couldn't submit your application. Your submission was flagged as possible spam."
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(action="click", selector="Submit Application")
    monkeypatch.setattr(
        agent,
        "_classify_submission_outcome",
        lambda _action, _ok: SubmissionOutcome(
            classification="external_blocked",
            reason_code="anti_spam_flagged",
            evidence_snippet="flagged as possible spam",
        ),
    )
    assert agent._handle_submission_outcome(action, False) == (False, False)
    assert agent._handle_submission_outcome(action, False) == (False, False)
    assert agent._handle_submission_outcome(action, False) == (False, True)
    reason = agent._build_submission_manual_reason(action)
    assert "classification=external_blocked" in reason
    assert "code=anti_spam_flagged" in reason


def test_replay_suno_yes_no_oscillation_escalates_to_stop(monkeypatch):
    """回放场景：同语义 Yes/No 动作反复失败，需从 replan 升级到 stop。"""
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _OutcomePage(
        "application form",
        url="https://jobs.ashbyhq.com/suno/1e23d125-d72c-49b6-891d-77d62c96cd13/application",
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    action = AgentAction(
        action="click",
        selector="Yes",
        target_question="Are you legally authorized to work in the United States?",
    )
    key = agent._semantic_action_key("ignored-fp", action)
    assert key
    agent._semantic_fail_counts[key] = 1
    assert agent._semantic_loop_guard_decision("ignored-fp", action) == "replan"
    agent._promote_semantic_guard("ignored-fp", action, stage="replan")
    assert agent._semantic_loop_guard_decision("ignored-fp", action) == "alternate"
    agent._promote_semantic_guard("ignored-fp", action, stage="alternate")
    assert agent._semantic_loop_guard_decision("ignored-fp", action) == "stop"


def test_observe_and_think_non_json_completion_fallback_returns_done(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _ObservePage(
        "Your application has been submitted successfully.",
        "https://jobs.ashbyhq.com/acme/role/application",
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    agent.step_count = 9
    agent.client = object()
    agent.visual_fallback_budget = 0

    snapshot_map = {
        "e1": SnapshotItem(ref="e1", role="button", name="Submit Application", nth=0)
    }
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.build_ui_snapshot",
        lambda _page: ("e1 | role=button | name=Submit Application", snapshot_map),
    )
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.build_question_blocks",
        lambda _page, _snapshot_map, **_kwargs: [],
    )
    monkeypatch.setattr(
        agent,
        "_collect_manual_required_evidence",
        lambda *_args, **_kwargs: {
            "password_input_count": 0,
            "captcha_element_count": 0,
            "has_captcha_challenge_text": False,
            "has_login_button": False,
            "has_apply_cta": False,
        },
    )
    monkeypatch.setattr(
        agent,
        "_classify_page_state",
        lambda *_args, **_kwargs: "application_or_form_page",
    )
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.run_chat_with_fallback",
        lambda **_kwargs: LLMCallResult(
            ok=True,
            raw="Your application was successfully submitted. The process is complete.",
            model="gpt-4o",
            model_index=0,
        ),
    )
    monkeypatch.setattr(
        agent,
        "_verify_completion",
        lambda: (True, "页面显示申请成功信息，无错误提示"),
    )
    state = agent._observe_and_think()
    assert state.status == "done"


def test_observe_and_think_llm_refusal_on_blocked_page_returns_refresh_action(
    monkeypatch,
):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _ObservePage(
        "We couldn't submit your application. Your submission was flagged as possible spam.",
        "https://jobs.ashbyhq.com/acme/role/application",
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    agent.step_count = 6
    agent.client = object()
    agent.visual_fallback_budget = 0
    monkeypatch.setattr(agent, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.build_ui_snapshot",
        lambda _page: (
            "e1 | role=link | name=Learn more",
            {"e1": SnapshotItem(ref="e1", role="link", name="Learn more", nth=0)},
        ),
    )
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.build_question_blocks",
        lambda _page, _snapshot_map, **_kwargs: [],
    )
    monkeypatch.setattr(
        agent,
        "_collect_manual_required_evidence",
        lambda *_args, **_kwargs: {
            "password_input_count": 0,
            "captcha_element_count": 0,
            "has_captcha_challenge_text": False,
            "has_login_button": False,
            "has_apply_cta": False,
        },
    )
    monkeypatch.setattr(
        agent,
        "_classify_page_state",
        lambda *_args, **_kwargs: "application_or_form_page",
    )
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.run_chat_with_fallback",
        lambda **_kwargs: LLMCallResult(
            ok=True,
            raw="I'm unable to assist with this request.",
            model="gpt-4o",
            model_index=0,
        ),
    )
    monkeypatch.setattr(agent, "_capture_step_screenshot", lambda: None)
    state = agent._observe_and_think()
    assert state.status == "continue"
    assert state.next_action is not None
    assert state.next_action.action == "refresh"


def test_observe_and_think_llm_refusal_blocked_page_refresh_exhausted_goes_stuck(
    monkeypatch,
):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    page = _ObservePage(
        "We couldn't submit your application. Your submission was flagged as possible spam.",
        "https://jobs.ashbyhq.com/acme/role/application",
    )
    agent = BrowserAgent(page=page, job=_DummyJob())
    agent.step_count = 6
    agent.client = object()
    agent.visual_fallback_budget = 0
    agent.refresh_attempts = agent.max_refresh_attempts
    monkeypatch.setattr(agent, "_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.build_ui_snapshot",
        lambda _page: (
            "e1 | role=link | name=Learn more",
            {"e1": SnapshotItem(ref="e1", role="link", name="Learn more", nth=0)},
        ),
    )
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.build_question_blocks",
        lambda _page, _snapshot_map, **_kwargs: [],
    )
    monkeypatch.setattr(
        agent,
        "_collect_manual_required_evidence",
        lambda *_args, **_kwargs: {
            "password_input_count": 0,
            "captcha_element_count": 0,
            "has_captcha_challenge_text": False,
            "has_login_button": False,
            "has_apply_cta": False,
        },
    )
    monkeypatch.setattr(
        agent,
        "_classify_page_state",
        lambda *_args, **_kwargs: "application_or_form_page",
    )
    monkeypatch.setattr(
        "autojobagent.core.vision_agent.run_chat_with_fallback",
        lambda **_kwargs: LLMCallResult(
            ok=True,
            raw="I'm unable to assist with this request.",
            model="gpt-4o",
            model_index=0,
        ),
    )
    monkeypatch.setattr(agent, "_capture_step_screenshot", lambda: None)
    state = agent._observe_and_think()
    assert state.status == "stuck"


def test_run_reports_macro_action_result_after_execution(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage(
            "application page", "https://jobs.ashbyhq.com/suno/role/application"
        ),
        job=_DummyJob(),
    )
    agent.client = object()
    monkeypatch.setattr(agent, "_log", lambda *_args, **_kwargs: None)
    action = AgentAction(
        action="click",
        selector="Yes",
        target_question="Are you legally authorized to work in the United States?",
        reason="[macro:t2] execute planned question option selection",
    )
    states = iter(
        [
            AgentState(
                status="continue",
                summary="macro step",
                next_action=action,
                page_fingerprint="fp-1",
            ),
            AgentState(status="stuck", summary="stop"),
        ]
    )
    monkeypatch.setattr(agent, "_observe_and_think", lambda: next(states))
    monkeypatch.setattr(agent, "_semantic_loop_guard_decision", lambda *_args: "none")
    monkeypatch.setattr(agent, "_should_skip_repeated_action", lambda *_args: False)
    monkeypatch.setattr(agent, "_execute_action", lambda _action: False)
    monkeypatch.setattr(agent, "_record_action_result", lambda *_args: None)
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        agent,
        "_on_macro_action_result",
        lambda used_action, ok: calls.append((used_action.reason or "", bool(ok))),
    )
    result = agent.run()
    assert result is False
    assert calls == [("[macro:t2] execute planned question option selection", False)]


def test_run_reports_macro_result_when_submission_branch_stops(monkeypatch):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent.client = object()
    monkeypatch.setattr(agent, "_log", lambda *_args, **_kwargs: None)
    action = AgentAction(
        action="click",
        selector="Submit Application",
        reason="[macro:t9] progression submit",
    )
    monkeypatch.setattr(
        agent,
        "_observe_and_think",
        lambda: AgentState(
            status="continue",
            summary="submit",
            next_action=action,
            page_fingerprint="fp-submit",
        ),
    )
    monkeypatch.setattr(agent, "_semantic_loop_guard_decision", lambda *_args: "none")
    monkeypatch.setattr(agent, "_should_skip_repeated_action", lambda *_args: False)
    monkeypatch.setattr(agent, "_execute_action", lambda _action: False)
    monkeypatch.setattr(agent, "_is_progression_action", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        agent,
        "_handle_submission_outcome",
        lambda _action, _success: (False, True),
    )
    monkeypatch.setattr(agent, "_record_action_result", lambda *_args: None)
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        agent,
        "_on_macro_action_result",
        lambda used_action, ok: calls.append((used_action.reason or "", bool(ok))),
    )
    result = agent.run()
    assert result is False
    assert calls == [("[macro:t9] progression submit", False)]


def test_sync_failure_hints_records_failure_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("application", "https://jobs.ashbyhq.com/suno/role/application"),
        job=_DummyJob(),
    )
    agent._failure_memory = FailureMemoryStore(path=tmp_path / "failure_memory.ndjson")
    outcome = SubmissionOutcome(
        classification="external_blocked",
        reason_code="anti_spam_or_risk_blocked",
        evidence_snippet="Your application submission was flagged as possible spam.",
    )
    action = AgentAction(action="click", selector="Submit Application")
    agent._sync_failure_hints(outcome, action)
    hits = agent._failure_memory.query_similar(
        page_scope=agent._stable_page_scope(),
        classification="external_blocked",
        reason_code="anti_spam_or_risk_blocked",
        action="click",
        limit=2,
    )
    assert len(hits) >= 1
    assert "spam" in (hits[0].evidence_snippet or "").lower()


def test_load_failure_memory_hints_returns_promptable_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(
        BrowserManager,
        "_load_settings",
        lambda _self: {"llm": {"fallback_models": ["gpt-4o"]}},
    )
    agent = BrowserAgent(
        page=_ObservePage("application", "https://jobs.ashbyhq.com/suno/role/application"),
        job=_DummyJob(),
    )
    agent._failure_memory = FailureMemoryStore(path=tmp_path / "failure_memory.ndjson")
    agent._failure_memory.upsert_case(
        page_scope=agent._stable_page_scope(),
        classification="validation_error",
        reason_code="required_field_missing",
        symptom="submit blocked by missing required field",
        root_cause="required question not answered",
        successful_strategy="repair missing field first then submit",
        guardrails="do not repeat submit before fixing error",
        source_event="unit_test",
    )
    summary = agent._load_failure_memory_hints(
        page_scope=agent._stable_page_scope(),
        question_blocks=[],
    )
    assert summary != "无"
    assert "策略" in summary


def test_should_query_failure_memory_skips_stable_fill_path():
    agent = BrowserAgent(page=object(), job=_DummyJob())
    blocks = [
        QuestionBlock(
            question_id="q1",
            question_text="Are you authorized to work in the United States?",
            control_type="single_choice",
            required=True,
            has_error=False,
            options=[
                OptionNode(text="Yes", role="button", selected=False),
                OptionNode(text="No", role="button", selected=False),
            ],
            selected_options=[],
        )
    ]
    enabled, reason = agent._should_query_failure_memory(
        page_state="application_or_form_page",
        question_blocks=blocks,
        has_pending_macro_tasks=True,
    )
    assert enabled is False
    assert reason == "stable_fill_path"


def test_should_query_failure_memory_enables_after_failure():
    agent = BrowserAgent(page=object(), job=_DummyJob())
    agent.consecutive_failures = 1
    enabled, reason = agent._should_query_failure_memory(
        page_state="application_or_form_page",
        question_blocks=[],
        has_pending_macro_tasks=True,
    )
    assert enabled is True
    assert reason == "failure_recovery"


def test_should_query_failure_memory_enables_before_submit_when_queue_done():
    agent = BrowserAgent(page=object(), job=_DummyJob())
    enabled, reason = agent._should_query_failure_memory(
        page_state="application_or_form_page",
        question_blocks=[],
        has_pending_macro_tasks=False,
    )
    assert enabled is True
    assert reason == "pre_submit_review"
