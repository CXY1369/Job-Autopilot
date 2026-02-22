"""
视觉 AI Agent：像人类一样操作浏览器。

核心循环：
1. 观察（截图）
2. 思考（LLM 分析当前状态，决定下一步）
3. 行动（执行单个操作）
4. 反馈（检查结果，继续循环）

不写死逻辑，让 LLM 动态决策。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import random
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

from openai import OpenAI
from playwright.sync_api import Page
from PIL import Image

from ..db.database import SessionLocal
from ..models.job_log import JobLog
from ..config import (
    get_user_info_for_prompt,
    load_user_profile,
    load_agent_guidelines,
    list_upload_candidates,
    is_upload_path_allowed,
    resolve_upload_candidate,
)
from .browser_manager import BrowserManager
from .debug_probe import append_debug_log
from .ui_snapshot import build_ui_snapshot, SnapshotItem
from .heuristics import assess_manual_required
from .semantic_perception import (
    SemanticSnapshot,
    build_semantic_snapshot,
    extract_semantic_error_snippets,
)
from .outcome_classifier import (
    assess_completion_confidence as oc_assess_completion_confidence,
    SubmissionOutcome,
    build_submission_manual_reason as oc_build_submission_manual_reason,
    classify_submission_outcome as oc_classify_submission_outcome,
    looks_like_completion_text as oc_looks_like_completion_text,
)
from .loop_guard import (
    promote_semantic_fail_count as lg_promote_semantic_fail_count,
    record_loop_action_result as lg_record_loop_action_result,
    semantic_loop_guard_decision as lg_semantic_loop_guard_decision,
    stable_page_scope as lg_stable_page_scope,
)
from .executor import (
    do_scroll as exec_do_scroll,
    do_select as exec_do_select,
    locate_file_input as exec_locate_file_input,
    smart_click as exec_smart_click,
    smart_fill as exec_smart_fill,
    smart_type as exec_smart_type,
    verify_upload_success as exec_verify_upload_success,
)
from .planner import (
    safe_parse_json as planner_safe_parse_json,
    sanitize_simplify_claims as planner_sanitize_simplify_claims,
)
from .intent_engine import (
    fallback_label_intents as ie_fallback_label_intents,
    infer_label_intents as ie_infer_label_intents,
    infer_label_intents_with_llm as ie_infer_label_intents_with_llm,
    infer_snapshot_intents as ie_infer_snapshot_intents,
    infer_text_intents as ie_infer_text_intents,
    intent_cache_key as ie_intent_cache_key,
)
from .manual_gate import (
    classify_page_state as mg_classify_page_state,
    collect_manual_required_evidence as mg_collect_manual_required_evidence,
    collect_selector_details as mg_collect_selector_details,
    count_visible_captcha_challenge as mg_count_visible_captcha_challenge,
    safe_locator_count as mg_safe_locator_count,
    select_apply_entry_candidate as mg_select_apply_entry_candidate,
)
from .fsm_orchestrator import (
    decide_local_adjustment_path as fsm_decide_local_adjustment_path,
    decide_failure_recovery_path as fsm_decide_failure_recovery_path,
    decide_repeated_skip_path as fsm_decide_repeated_skip_path,
    decide_semantic_guard_path as fsm_decide_semantic_guard_path,
    derive_execution_phase as fsm_derive_execution_phase,
)
from .llm_runtime import run_chat_with_fallback
from .prompt_builder import (
    build_system_prompt,
    build_user_prompt,
)
from .state_parser import parse_agent_response_payload
from .terminal_guard import raw_response_implies_completion
from .semantic_tree import (
    FormGraph,
    QuestionBlock,
    build_form_graph,
    build_question_blocks,
    format_form_graph,
    format_question_blocks,
)
from .macro_tasks import MacroTask, build_macro_tasks, summarize_macro_tasks
from .failure_memory import FailureMemoryStore
from .verifier import (
    get_input_value as verifier_get_input_value,
    is_dropdown_open as verifier_is_dropdown_open,
    normalize_answer_label as verifier_normalize_answer_label,
    verify_ref_action_effect as verifier_verify_ref_action_effect,
)


# 截图保存目录
STORAGE_DIR = Path(__file__).parent.parent / "storage" / "screenshots"
# Debug log 目录/路径（NDJSON）
DEBUG_LOG_DIR = Path(__file__).parent.parent / "storage" / "logs"
TRACE_DIR = Path(__file__).parent.parent / "storage" / "logs"
DEBUG_LOG_PATH = DEBUG_LOG_DIR / "vision_agent.ndjson"


DEFAULT_FALLBACK_MODELS = [
    "gpt-4o",  # 默认模型：最佳视觉理解
    "gpt-4o-2024-11-20",  # 最新版本
    "gpt-4.1",  # 新一代模型
    "gpt-4.1-mini",  # 轻量版
    "gpt-5-mini",  # 实验版
    "gpt-4-turbo",  # 稳定后备
    "gpt-4o-mini",  # 最后备选
]

# 截图压缩配置
SCREENSHOT_MAX_WIDTH = 1280  # 最大宽度（像素）
SCREENSHOT_JPEG_QUALITY = 75  # JPEG 质量（0-100），75 是清晰度和体积的良好平衡


@dataclass
class AgentAction:
    """单个操作"""

    action: str  # click, fill, type, select, upload, scroll, refresh, wait, done, stuck
    ref: Optional[str] = None  # 目标元素 ref（优先）
    selector: Optional[str] = None  # 目标元素的文本/描述
    value: Optional[str] = None  # 填入的值
    target_question: Optional[str] = (
        None  # 回答题绑定的问题文本（用于 Yes/No 等同名选项）
    )
    element_type: Optional[str] = (
        None  # 元素类型：button, link, checkbox, radio, input, option, text
    )
    reason: Optional[str] = None  # 为什么这样做


@dataclass
class AgentState:
    """Agent 当前状态"""

    status: Literal["continue", "done", "stuck", "error"]
    summary: str  # 当前页面状态描述
    next_action: Optional[AgentAction] = None
    raw_response: Optional[str] = None
    page_overview: Optional[str] = None
    field_audit: Optional[str] = None
    action_plan: Optional[list[str]] = None
    risk_or_blocker: Optional[str] = None
    page_fingerprint: Optional[str] = None


@dataclass
class QueueTaskView:
    task_id: str
    task_type: str
    label: str
    status: str
    action: str
    ref: str | None = None
    target_question: str | None = None


@dataclass
class VisualAuditResult:
    visual_summary: str
    required_fields: list[str]
    required_questions: list[str]
    required_uploads: list[str]
    source: str = "heuristic"


def evaluate_progression_block_reason(
    evidence: dict[str, int | list[str] | bool],
    *,
    llm_confirms_context_error: bool = False,
) -> str | None:
    """根据结构化证据评估是否应阻止 Next/Submit。"""
    invalid_field_count = int(evidence.get("invalid_field_count", 0) or 0)
    required_empty_count = int(evidence.get("required_empty_count", 0) or 0)
    red_error_hits = int(evidence.get("red_error_hits", 0) or 0)
    error_container_hits = int(evidence.get("error_container_hits", 0) or 0)
    local_error_keyword_hits = int(evidence.get("local_error_keyword_hits", 0) or 0)
    global_error_keyword_hits = int(evidence.get("global_error_keyword_hits", 0) or 0)
    submit_candidates = evidence.get("submit_candidates", [])
    has_enabled_submit = False
    has_submit_candidate = False
    if isinstance(submit_candidates, list):
        for item in submit_candidates:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).lower()
            item_type = str(item.get("type", "")).lower()
            is_submit_like = (
                ("submit" in text) or ("apply" in text) or item_type == "submit"
            )
            if not is_submit_like:
                continue
            has_submit_candidate = True
            disabled = bool(item.get("disabled", False))
            aria_disabled = str(item.get("aria_disabled", "")).lower()
            if (not disabled) and aria_disabled not in ("true", "1"):
                has_enabled_submit = True
                break
    invalid_field_samples = evidence.get("invalid_field_samples", [])
    file_upload_state_samples = evidence.get("file_upload_state_samples", [])
    required_empty_samples = evidence.get("required_empty_samples", [])
    all_invalid_are_file = False
    all_required_empty_are_file = False
    if isinstance(invalid_field_samples, list) and invalid_field_samples:
        all_invalid_are_file = all(
            isinstance(it, dict) and str(it.get("type", "")).lower() == "file"
            for it in invalid_field_samples
        )
    if isinstance(required_empty_samples, list) and required_empty_samples:
        all_required_empty_are_file = all(
            isinstance(it, dict) and str(it.get("type", "")).lower() == "file"
            for it in required_empty_samples
        )
    has_upload_ready_signal = False
    if isinstance(file_upload_state_samples, list):
        for sample in file_upload_state_samples:
            if not isinstance(sample, dict):
                continue
            if bool(sample.get("has_replace_text")) or bool(
                sample.get("has_uploaded_file_name")
            ):
                has_upload_ready_signal = True
                break

    if invalid_field_count > 0:
        # 对 file input 的站点差异做特例：只要上传状态已就绪，不阻塞提交
        if (
            all_invalid_are_file
            and has_upload_ready_signal
            and (has_enabled_submit or has_submit_candidate)
            and (required_empty_count <= 0 or all_required_empty_are_file)
            and error_container_hits <= 0
            and red_error_hits <= 0
            and local_error_keyword_hits <= 0
        ):
            evidence["allowed_by_file_upload_state"] = True
            evidence["gate_decision"] = "allow"
            evidence["allowed_by"] = "file_only_invalid_with_upload_ready"
            return None
        # 对“仅 invalid 单信号”做保护：若提交按钮可用且无其它错误证据，不阻塞提交流程
        if (
            not all_invalid_are_file
            and required_empty_count <= 0
            and error_container_hits <= 0
            and red_error_hits <= 0
            and local_error_keyword_hits <= 0
            and has_enabled_submit
        ):
            evidence["gate_decision"] = "allow"
            evidence["allowed_by"] = "single_invalid_without_other_errors"
            return None
        evidence["gate_decision"] = "block"
        evidence["blocked_by"] = "invalid_field_count"
        return f"检测到 {invalid_field_count} 个无效字段（aria-invalid/:invalid）"
    if required_empty_count > 0:
        evidence["gate_decision"] = "block"
        evidence["blocked_by"] = "required_empty_count"
        return f"检测到 {required_empty_count} 个必填字段为空"
    if error_container_hits > 0 and (
        red_error_hits > 0 or local_error_keyword_hits > 0
    ):
        evidence["gate_decision"] = "block"
        evidence["blocked_by"] = "error_container_with_visual_or_local_keyword"
        return "检测到表单错误提示（错误容器/红色文本）"

    # 仅有全页关键词时，不立即拦截；需要 LLM 复核上下文
    if global_error_keyword_hits > 0 and llm_confirms_context_error:
        evidence["gate_decision"] = "block"
        evidence["blocked_by"] = "global_keyword_confirmed_by_llm"
        return "检测到与当前表单相关的错误提示（经语义复核）"

    evidence["gate_decision"] = "allow"
    evidence["allowed_by"] = "no_blocking_evidence"
    return None


class BrowserAgent:
    """
    像人类一样操作浏览器的 AI Agent。

    核心能力：
    - 观察：截图 + 获取页面文本
    - 思考：让 LLM 分析状态并决定下一步
    - 行动：执行点击、填写、滚动等基本操作
    - 循环：不断重复直到任务完成或放弃
    """

    def __init__(
        self,
        page: Page,
        job,
        max_steps: int = 50,
        *,
        pre_nav_only: bool = False,
    ):
        self.page = page
        self.job = job
        self.job_id = job.id
        self.max_steps = max_steps
        self.pre_nav_only = pre_nav_only
        self.step_count = 0
        self.history: list[str] = []  # 操作历史，帮助 LLM 避免重复

        # OpenAI 客户端
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.api_key) if self.api_key else None

        settings = BrowserManager()._load_settings()
        self.llm_cfg = settings.get("llm", {})
        fallback_models = self.llm_cfg.get("fallback_models") or DEFAULT_FALLBACK_MODELS
        if not isinstance(fallback_models, list) or not fallback_models:
            fallback_models = DEFAULT_FALLBACK_MODELS
        preferred_model = self.llm_cfg.get("model")
        if preferred_model and preferred_model in fallback_models:
            fallback_models = [preferred_model] + [
                m for m in fallback_models if m != preferred_model
            ]
        self.fallback_models = fallback_models
        # 默认首选 GPT-4o
        self.model_index = 0
        self.model = self.fallback_models[self.model_index]
        self.intent_model = self.llm_cfg.get("intent_model") or self.fallback_models[0]

        # 创建 job 专属截图目录
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.screenshot_dir = STORAGE_DIR / f"job_{self.job_id}_{timestamp}"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_screenshot_bytes: bytes = b""  # 缓存最近一次截图用于保存
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self.trace_path = (
            TRACE_DIR / f"agent_trace_job_{self.job_id}_{timestamp}.ndjson"
        )

        # 智能终止机制
        self.consecutive_failures = 0  # 连续失败计数
        self.max_consecutive_failures = 5  # 连续失败阈值
        self.last_url = None  # 页面 URL 跟踪（用于检测页面跳转）
        self._last_snapshot_map: dict[str, SnapshotItem] = {}
        self.upload_candidates: list[str] = list_upload_candidates(max_files=30)
        self.preferred_resume_path: str | None = getattr(job, "resume_used", None)
        self._last_upload_signals: list[str] = []
        self.refresh_attempts = 0
        self.max_refresh_attempts = 2
        self.refresh_exhausted = False
        self.manual_reason_hint: str | None = None
        self.simplify_state = str(
            getattr(job, "simplify_state", "unknown") or "unknown"
        )
        self.simplify_message = str(getattr(job, "simplify_message", "") or "")
        self.assist_required_before = int(
            getattr(job, "assist_required_before", 0) or 0
        )
        self.assist_required_after = int(getattr(job, "assist_required_after", 0) or 0)
        self.assist_prefill_delta = int(getattr(job, "assist_prefill_delta", 0) or 0)
        self.assist_prefill_verified = bool(
            getattr(job, "assist_prefill_verified", False)
        )
        self._intent_cache: dict[str, dict[str, list[str]]] = {}
        self._last_snapshot_intents: dict[str, set[str]] = {}
        self._last_question_blocks: list[QuestionBlock] = []
        self._last_form_graph: FormGraph | None = None
        self._last_form_graph_text: str = ""
        self._user_profile: dict = load_user_profile()
        self._macro_tasks: list[MacroTask] = []
        self._macro_scope: str = ""
        self._active_macro_task_id: str | None = None
        self._macro_retry_limit = 3
        self._macro_precondition_wait_limit = 3
        self._macro_disabled_scopes: set[str] = set()
        self._macro_manual_block_reason: str | None = None
        self._error_gate_cache: dict[str, bool] = {}
        self._last_observed_fingerprint: str = ""
        self._state_cache_by_fingerprint: dict[str, AgentState] = {}
        self._action_fail_counts: dict[str, int] = {}
        self._action_cache_use_counts: dict[str, int] = {}
        self._repeated_skip_counts: dict[str, int] = {}
        self._semantic_fail_counts: dict[str, int] = {}
        self._last_progression_block_reason: str | None = None
        self._last_progression_block_snippets: list[str] = []
        self._last_validation_signature: str = ""
        self._validation_repeat_count: int = 0
        self._submission_retry_limit = 3
        self._submission_retry_counts: dict[str, int] = {}
        self._submission_refresh_attempts: dict[str, int] = {}
        self._last_submission_outcome: SubmissionOutcome | None = None
        self.failure_class_hint: str | None = None
        self.failure_code_hint: str | None = None
        self.retry_count_hint: int = 0
        self.last_error_snippet_hint: str | None = None
        self.last_outcome_class_hint: str | None = None
        self.last_outcome_at_hint: datetime | None = None
        self._llm_parse_fail_streak = 0
        self._llm_refusal_streak = 0
        self._execution_phase: str = "observe"
        self._queue_retry_limit = 3
        self._max_full_restart_attempts = 2
        self._full_restart_attempts = 0
        self._last_failed_task_key: str = ""
        self._same_task_failure_streak = 0
        self._last_queue_plan: list[QueueTaskView] = []
        self._scope_visual_audits: dict[str, VisualAuditResult] = {}
        self._latest_required_dom_summary: str = ""
        self._latest_visual_summary: str = ""
        self._latest_failure_memory_summary: str = "无"
        self._failure_memory = FailureMemoryStore()
        self._upload_task_locks: set[str] = set()
        self._force_visual_audit_next_plan: bool = False
        try:
            self.visual_fallback_budget = max(
                0, int(os.getenv("VISION_FALLBACK_BUDGET", "8"))
            )
        except Exception:
            self.visual_fallback_budget = 8
        self.visual_fallback_used = 0
        self.step_screenshot_mode = (
            os.getenv("STEP_SCREENSHOT_MODE", "vision_only").strip().lower()
        )

    # region agent log
    def _ndjson_log(self, hypothesis_id: str, location: str, message: str, data: dict):
        """轻量级调试日志，写入 NDJSON 文件。"""
        payload = {
            "sessionId": "debug-session",
            "runId": "run1",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        try:
            DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # endregion

    def run(self) -> bool:
        """
        运行 Agent 主循环，返回是否成功完成任务。
        """
        self._log("========== AI Agent 开始运行 ==========")
        self._log(f"最大步数: {self.max_steps}")

        if not self.client and not self.pre_nav_only:
            self._log("❌ OPENAI_API_KEY 未设置，无法运行 Agent", "error")
            self._log_finalized("failed", "missing_openai_api_key")
            return False

        while self.step_count < self.max_steps:
            self.step_count += 1
            self._log(f"\n--- 第 {self.step_count} 步 ---")

            # 1. 观察
            state = self._observe_and_think()

            if state.status == "error":
                self._log(f"❌ 观察/思考出错: {state.summary}", "error")
                continue

            # 2. 记录 LLM 的分析
            if self._latest_required_dom_summary:
                self._log(
                    f"🧩 检测页面必填项DOM元素: {self._latest_required_dom_summary}"
                )
            if self._latest_visual_summary:
                self._log(
                    f"🖼 根据AI视觉理解简单描述页面截图内容: {self._latest_visual_summary}"
                )
            if (
                self._latest_failure_memory_summary
                and self._latest_failure_memory_summary != "无"
            ):
                self._log(
                    f"🧠 Failure Memory 提示: {self._latest_failure_memory_summary}"
                )
            self._log(f"📋 状态: {state.summary}")
            if state.page_overview:
                self._log(f"🧭 页面概览: {state.page_overview}")
            if state.field_audit:
                self._log(f"🧾 字段审计: {state.field_audit}")
            if state.action_plan:
                self._log(f"🗺 计划序列: {' -> '.join(state.action_plan)}")
            if state.risk_or_blocker:
                self._log(f"⚠ 风险/阻塞: {state.risk_or_blocker}")

            next_item = None
            if state.next_action is not None:
                next_item = self._last_snapshot_map.get((state.next_action.ref or ""))
            next_is_progression = bool(
                state.next_action
                and self._is_progression_action(state.next_action, item=next_item)
            )
            pending_macro_tasks = any(
                task.status not in ("done", "blocked") for task in self._macro_tasks
            )
            self._execution_phase = fsm_derive_execution_phase(
                state_status=state.status,
                has_next_action=state.next_action is not None,
                action_is_progression=next_is_progression,
                progression_blocked=bool(self._last_progression_block_reason),
                manual_required=state.status == "stuck",
                has_pending_macro_tasks=pending_macro_tasks,
                consecutive_failures=self.consecutive_failures,
            )
            self._step_log(
                "workflow_phase",
                {
                    "step": self.step_count,
                    "phase": self._execution_phase,
                    "state_status": state.status,
                    "has_next_action": state.next_action is not None,
                    "next_is_progression": next_is_progression,
                    "pending_macro_tasks": pending_macro_tasks,
                    "consecutive_failures": self.consecutive_failures,
                },
            )

            # 3. 检查是否完成（带二次验证）
            if state.status == "done":
                if self.pre_nav_only:
                    self._log("✓ 预导航完成：已进入申请页")
                    self._log("========== AI Agent 运行结束 ==========")
                    self._log_finalized("done", "pre_nav_completed")
                    return True
                self._log("🔍 Agent 判断任务完成，进行二次验证...")

                # 二次验证：检查页面是否真的显示成功信息
                is_really_done, verification_msg = self._verify_completion()

                if is_really_done:
                    self._log(f"✓ 二次验证通过: {verification_msg}")
                    self._log("========== AI Agent 运行结束 ==========")
                    self._log_finalized("done", verification_msg)
                    return True
                else:
                    self._log(f"⚠ 二次验证失败: {verification_msg}", "warn")
                    self._log("   继续执行，可能还有未完成的步骤...")
                    # 不返回，继续循环
                    continue

            if state.status == "stuck":
                self._set_manual_reason_hint(state.summary or "需要人工介入")
                self._log("⚠ Agent 判断无法继续，需要人工介入", "warn")
                self._log("========== AI Agent 运行结束 ==========")
                self._log_finalized("manual_required", state.summary or "stuck")
                return False

            # 4. 执行下一步操作
            if state.next_action:
                action = state.next_action
                fp = state.page_fingerprint or self._last_observed_fingerprint
                is_macro_action = self._is_macro_action(action)
                is_submission_action = self._is_submission_click_action(
                    action, item=self._last_snapshot_map.get(action.ref or "")
                )
                semantic_guard = (
                    "none"
                    if is_submission_action
                    else self._semantic_loop_guard_decision(fp, action)
                )
                precomputed_alternate = None
                if is_macro_action and semantic_guard != "none":
                    self._step_log(
                        "macro_local_adjustment",
                        {
                            "step": self.step_count,
                            "reason_code": "ignore_global_semantic_guard_for_macro",
                            "original_guard": semantic_guard,
                            "action": action.action,
                            "selector": action.selector,
                        },
                    )
                    semantic_guard = "none"
                if semantic_guard == "alternate":
                    precomputed_alternate = self._build_alternate_action(action)
                guard_path = fsm_decide_semantic_guard_path(
                    semantic_guard,
                    has_alternate_action=precomputed_alternate is not None,
                )
                if guard_path == "replan":
                    self._promote_semantic_guard(fp, action, stage="replan")
                    if fp:
                        self._state_cache_by_fingerprint.pop(fp, None)
                    self.history.append(
                        f"步骤{self.step_count}: 语义动作重复失败，清理缓存并强制重规划 {action.action}({action.ref or action.selector or ''})"
                    )
                    self.consecutive_failures += 1
                    continue
                if guard_path == "alternate":
                    self._log("⚠ 语义动作重复失败，改用替代动作", "warn")
                    action = precomputed_alternate or action
                elif guard_path == "alternate_missing_replan":
                    self._promote_semantic_guard(fp, action, stage="alternate")
                    if fp:
                        self._state_cache_by_fingerprint.pop(fp, None)
                    self.history.append(
                        f"步骤{self.step_count}: 语义动作重复失败，暂无替代动作，强制重规划 {action.action}({action.ref or action.selector or ''})"
                    )
                    self.consecutive_failures += 1
                    continue
                if guard_path == "stop":
                    if not self._last_submission_outcome:
                        self._sync_failure_hints(
                            SubmissionOutcome(
                                classification="unknown_blocked",
                                reason_code="semantic_loop_stop",
                                evidence_snippet=self._last_progression_block_reason
                                or "",
                            )
                        )
                    hint = self._build_semantic_loop_manual_reason(action)
                    self._set_manual_reason_hint(hint)
                    self._log(
                        "⚠ 语义动作重复失败超过阈值，停止执行并转人工处理", "warn"
                    )
                    self._log("========== AI Agent 运行结束（语义循环熔断）==========")
                    self._log_finalized("manual_required", "semantic_loop_guard_stop")
                    return False
                if (not is_macro_action) and self._should_skip_repeated_action(
                    fp, action
                ):
                    skip_key = self._action_fail_key(fp, action)
                    skip_count = self._repeated_skip_counts.get(skip_key, 0) + 1
                    self._repeated_skip_counts[skip_key] = skip_count
                    self._log(
                        "⚠ 检测到同页面重复失败动作，触发重规划而不重复执行",
                        "warn",
                    )
                    alternate_action = self._build_alternate_action(action)
                    skip_path = fsm_decide_repeated_skip_path(
                        skip_count=skip_count,
                        has_alternate_action=alternate_action is not None,
                    )
                    if skip_path == "alternate":
                        self._log("   ↪ 尝试同页替代动作以打破循环")
                        action = alternate_action or action
                    elif skip_path == "replan":
                        # 第一次跳过时清理该页缓存，强制下一步重规划。
                        if fp:
                            self._state_cache_by_fingerprint.pop(fp, None)
                        self.history.append(
                            f"步骤{self.step_count}: 跳过重复失败动作后清理页面计划缓存 {action.action}({action.ref or action.selector or ''})"
                        )
                        self.consecutive_failures += 1
                        continue
                    elif skip_path == "stop":
                        self._set_manual_reason_hint(
                            "同页面重复失败且无可执行替代动作，需要人工处理"
                        )
                        self._log(
                            "⚠ 重复失败已无替代路径，停止执行并转人工处理",
                            "warn",
                        )
                        self._log(
                            "========== AI Agent 运行结束（重复失败无替代）=========="
                        )
                        self._log_finalized(
                            "manual_required", "repeated_failure_without_alternate"
                        )
                        return False
                    self.history.append(
                        f"步骤{self.step_count}: 跳过重复失败动作 {action.action}({action.ref or action.selector or ''})，要求改用其他策略"
                    )
                    self.consecutive_failures += 1
                elem_info = f"[{action.element_type}]" if action.element_type else ""
                ref_info = f"(ref={action.ref}) " if action.ref else ""
                self._log(
                    f"🎯 计划: {action.action} {ref_info}{elem_info} {action.selector or ''} {action.value or ''}"
                )
                if action.target_question:
                    self._log(f"   绑定问题: {action.target_question}")
                if action.reason:
                    self._log(f"   原因: {action.reason}")

                success = self._execute_action(action)
                should_stop = False
                source_item = self._last_snapshot_map.get(action.ref or "")
                if self._is_submission_click_action(action, item=source_item):
                    success, should_stop = self._handle_submission_outcome(
                        action, success
                    )
                    if should_stop:
                        self._on_macro_action_result(action, False)
                        self._record_action_result(fp, action, False)
                        self._set_manual_reason_hint(
                            self._build_submission_manual_reason(action)
                        )
                        self._log(
                            "⚠ 提交阻断达到重试上限，停止执行并转人工处理", "warn"
                        )
                        self._log("========== AI Agent 运行结束（提交阻断）==========")
                        self._log_finalized(
                            "manual_required", "submission_blocked_retry_exhausted"
                        )
                        return False
                self._on_macro_action_result(action, success)
                self._record_action_result(fp, action, success)
                task_key = self._task_execution_key(fp, action)
                if success:
                    self._same_task_failure_streak = 0
                    self._last_failed_task_key = ""
                else:
                    if task_key and task_key == self._last_failed_task_key:
                        self._same_task_failure_streak += 1
                    else:
                        self._last_failed_task_key = task_key
                        self._same_task_failure_streak = 1

                # 记录到历史（让 AI 能看到操作结果，从而调整策略）
                target_desc = action.ref or (action.selector or "")
                action_desc = f"{action.action}({target_desc}"
                if action.value:
                    action_desc += f", {action.value}"
                if action.target_question:
                    action_desc += f", q={action.target_question}"
                action_desc += ")"

                if success:
                    self.history.append(
                        f"步骤{self.step_count}: {action_desc} ✓ [请检查截图确认是否正确生效]"
                    )
                    self.consecutive_failures = 0  # 重置连续失败计数
                else:
                    self.history.append(
                        f"步骤{self.step_count}: {action_desc} ✗失败 [操作未成功，可能需要换方法]"
                    )
                    self.consecutive_failures = self._same_task_failure_streak

                if success:
                    self._log("   ✓ 执行成功")
                else:
                    if is_macro_action:
                        adjustment = fsm_decide_local_adjustment_path(
                            action_success=False,
                            is_macro_action=True,
                            has_alternate_action=self._build_alternate_action(action)
                            is not None,
                            repeated_same_error=self._validation_repeat_count >= 2,
                            retry_count=self._macro_task_retry_count(action),
                            retry_limit=self._macro_retry_limit,
                        )
                        self._step_log(
                            "macro_local_adjustment",
                            {
                                "step": self.step_count,
                                "task_id": self._macro_task_id_from_action(action),
                                "adjustment": adjustment,
                                "retry_count": self._macro_task_retry_count(action),
                                "retry_limit": self._macro_retry_limit,
                            },
                        )
                    self._log(
                        (
                            "   ❌ 执行失败 "
                            f"(同任务连续失败: {self._same_task_failure_streak}/{self._queue_retry_limit})"
                        ),
                        "warn",
                    )
                    # 保存失败截图（带 _failed 后缀）
                    try:
                        failed_screenshot = self.page.screenshot(full_page=True)
                        failed_compressed = self._compress_screenshot(failed_screenshot)
                        failed_path = (
                            self.screenshot_dir
                            / f"step_{self.step_count:02d}_failed.jpg"
                        )
                        failed_path.write_bytes(failed_compressed)
                        self._log(f"   💾 失败截图: {failed_path.name}")
                    except Exception:
                        pass

                    failure_path = fsm_decide_failure_recovery_path(
                        consecutive_failures=self._same_task_failure_streak,
                        max_consecutive_failures=self._queue_retry_limit,
                        refresh_attempts=self.refresh_attempts,
                        max_refresh_attempts=self.max_refresh_attempts,
                        refresh_exhausted=self.refresh_exhausted,
                    )
                    if failure_path == "refresh":
                        self._log(
                            (
                                "⚠ 当前任务连续失败达到 "
                                f"{self._same_task_failure_streak} 次，触发页面刷新并重建队列"
                            ),
                            "warn",
                        )
                        refreshed = self._do_refresh(trigger="auto_stuck_recovery")
                        if refreshed:
                            self._full_restart_attempts = self.refresh_attempts
                            self.consecutive_failures = 0
                            self._same_task_failure_streak = 0
                            self._last_failed_task_key = ""
                            continue
                    elif failure_path == "stop_refresh_exhausted":
                        self._set_manual_reason_hint(
                            "页面刷新两次后仍无进展，需要人工处理"
                        )
                        self._log(
                            "⚠ 页面刷新次数已用尽，停止执行并标记待人工处理",
                            "warn",
                        )
                        self._log(
                            "========== AI Agent 运行结束（刷新重试耗尽）=========="
                        )
                        self._log_finalized(
                            "manual_required", "refresh_retries_exhausted"
                        )
                        return False
                    elif failure_path == "stop_max_failures":
                        self._set_manual_reason_hint(
                            "连续操作失败达到上限，需要人工处理"
                        )
                        self._log(
                            f"⚠ 连续 {self.consecutive_failures} 次操作失败，停止执行",
                            "warn",
                        )
                        self._log("========== AI Agent 运行结束（智能终止）==========")
                        self._log_finalized(
                            "manual_required", "consecutive_action_failures_exhausted"
                        )
                        return False

                # 等待页面响应后立即截图（让 AI 看到实时变化）
                # 短暂等待让页面 UI 更新（如下拉框出现）
                self.page.wait_for_timeout(500)
            else:
                self._log("⚠ LLM 没有给出下一步操作", "warn")

        self._log(f"⚠ 已达到最大步数 {self.max_steps}，停止执行", "warn")
        self._set_manual_reason_hint("已达到最大步数仍未完成，需要人工处理")
        self._log("========== AI Agent 运行结束 ==========")
        self._log_finalized("manual_required", "max_steps_exhausted")
        return False

    def _observe_and_think(self) -> AgentState:
        """
        观察当前页面状态，让 LLM 思考下一步。
        """
        # 1. 先走语义观察（文本 + 可交互快照），截图仅按预算兜底
        screenshot_b64 = None

        # 2. 获取页面文本
        try:
            visible_text = self.page.inner_text("body")[:5000]
        except Exception:
            visible_text = ""

        # 2.5 生成可交互元素快照
        snapshot_text, snapshot_map = build_ui_snapshot(self.page)
        self._last_snapshot_map = snapshot_map
        try:
            current_url_for_fp = self.page.url
        except Exception:
            current_url_for_fp = "unknown"
        page_fingerprint = self._build_page_fingerprint(
            current_url_for_fp, snapshot_map
        )
        self._last_observed_fingerprint = page_fingerprint
        self._last_snapshot_intents = self._infer_snapshot_intents(
            snapshot_map, visible_text
        )
        semantic_snapshot = self._build_semantic_snapshot(
            current_url_for_fp,
            snapshot_map,
            visible_text,
        )
        question_blocks = build_question_blocks(
            self.page, snapshot_map, visible_text=visible_text
        )
        self._last_question_blocks = question_blocks
        question_blocks_text = format_question_blocks(question_blocks)
        form_graph = build_form_graph(
            current_url=current_url_for_fp,
            snapshot_map=snapshot_map,
            question_blocks=question_blocks,
            error_snippets=semantic_snapshot.errors,
        )
        self._last_form_graph = form_graph
        form_graph_text = format_form_graph(form_graph)
        self._last_form_graph_text = form_graph_text
        self._step_log(
            event="snapshot_generated",
            payload={
                "step": self.step_count,
                "url": semantic_snapshot.url,
                "domain": semantic_snapshot.domain,
                "normalized_path": semantic_snapshot.normalized_path,
                "page_id": semantic_snapshot.page_id,
                "element_count": len(semantic_snapshot.elements),
                "required_unfilled_count": len(semantic_snapshot.required_unfilled),
                "submit_candidate_count": len(semantic_snapshot.submit_candidates),
                "error_preview": semantic_snapshot.errors[:3],
            },
        )
        self._step_log(
            event="question_blocks_detected",
            payload={
                "step": self.step_count,
                "count": len(question_blocks),
                "sample": [
                    {
                        "question_id": qb.question_id,
                        "question_text": qb.question_text[:140],
                        "control_type": qb.control_type,
                        "required": qb.required,
                        "has_error": qb.has_error,
                        "option_count": len(qb.options),
                    }
                    for qb in question_blocks[:6]
                ],
            },
        )
        self._step_log(
            event="form_graph_generated",
            payload={
                "step": self.step_count,
                "scope": form_graph.page_scope,
                "field_count": len(form_graph.fields),
                "question_count": len(form_graph.questions),
                "required_unfilled_count": len(form_graph.required_unfilled),
                "submit_candidate_count": len(form_graph.submit_refs),
            },
        )
        # 终端展示上下文：每步都刷新 DOM 必填摘要；视觉摘要按 scope 缓存展示
        self._latest_required_dom_summary = self._build_required_dom_summary(
            snapshot_map,
            question_blocks,
        )
        scope_now = self._stable_page_scope()
        cached_audit = self._scope_visual_audits.get(scope_now)
        self._latest_visual_summary = (
            cached_audit.visual_summary if cached_audit else "尚未执行页面截图交叉审计"
        )
        # region agent log
        append_debug_log(
            location="vision_agent.py:_observe_and_think:snapshot_intents",
            message="snapshot and intent summary",
            data={
                "job_id": self.job_id,
                "step": self.step_count,
                "url": getattr(self.page, "url", ""),
                "snapshot_items": len(snapshot_map),
                "apply_intent_refs": sum(
                    1
                    for intents in self._last_snapshot_intents.values()
                    if "apply_entry" in intents
                ),
                "login_intent_refs": sum(
                    1
                    for intents in self._last_snapshot_intents.values()
                    if "login_action" in intents
                ),
                "sample_refs": list(sorted(snapshot_map.keys()))[:8],
            },
            run_id="pre-fix-debug",
            hypothesis_id="H4",
        )
        # endregion

        # 2.6 证据化检测登录/验证码等需人工介入场景（避免纯关键词误判）
        evidence = self._collect_manual_required_evidence(
            visible_text,
            snapshot_map,
            self._last_snapshot_intents,
        )
        manual_assessment = assess_manual_required(
            visible_text,
            password_input_count=evidence["password_input_count"],
            captcha_element_count=evidence["captcha_element_count"],
            has_captcha_challenge_text=evidence["has_captcha_challenge_text"],
            has_login_button=evidence["has_login_button"],
            has_apply_cta=evidence["has_apply_cta"],
        )
        page_state = self._classify_page_state(
            snapshot_map, evidence, manual_assessment
        )
        # region agent log
        append_debug_log(
            location="vision_agent.py:_observe_and_think:manual_gate_check",
            message="manual gate decision",
            data={
                "job_id": self.job_id,
                "step": self.step_count,
                "url": getattr(self.page, "url", ""),
                "page_state": page_state,
                "manual_required": manual_assessment.manual_required,
                "manual_reason": manual_assessment.reason,
                "manual_confidence": manual_assessment.confidence,
                "evidence": manual_assessment.evidence,
            },
            run_id="pre-fix-debug",
            hypothesis_id="H2",
        )
        # endregion
        self._step_log(
            event="page_state",
            payload={
                "page_state": page_state,
                "manual_required": manual_assessment.manual_required,
                "manual_reason": manual_assessment.reason,
                "manual_confidence": manual_assessment.confidence,
                "evidence": manual_assessment.evidence,
            },
        )
        if manual_assessment.manual_required:
            self._step_log(
                event="manual_required",
                payload={
                    "reason": manual_assessment.reason,
                    "confidence": manual_assessment.confidence,
                    "evidence": manual_assessment.evidence,
                },
            )
            return AgentState(
                status="stuck",
                summary="检测到登录/验证码/身份验证页面，需要人工处理",
                page_fingerprint=page_fingerprint,
            )

        if self.pre_nav_only:
            if page_state == "application_or_form_page":
                return AgentState(
                    status="done",
                    summary="预导航阶段：已进入申请页",
                    page_fingerprint=page_fingerprint,
                )
            if page_state == "job_detail_with_apply":
                apply_action = self._build_apply_entry_action(
                    snapshot_map, self._last_snapshot_intents
                )
                if apply_action:
                    return AgentState(
                        status="continue",
                        summary="预导航阶段：点击 Apply 进入申请页",
                        next_action=apply_action,
                        page_fingerprint=page_fingerprint,
                    )
            return AgentState(
                status="stuck",
                summary="预导航阶段：未识别到可进入申请页的入口",
                page_fingerprint=page_fingerprint,
            )

        if page_state == "job_detail_with_apply":
            apply_action = self._build_apply_entry_action(
                snapshot_map, self._last_snapshot_intents
            )
            if apply_action:
                return AgentState(
                    status="continue",
                    summary="检测到职位详情页，先点击 Apply 进入申请页面",
                    next_action=apply_action,
                    page_fingerprint=page_fingerprint,
                )

        # 2.7 终态硬判定（独立于 LLM JSON）：命中成功证据即直接收敛
        done_now, done_reason = self._verify_completion()
        if done_now:
            self._step_log(
                event="terminal_success_detected",
                payload={
                    "step": self.step_count,
                    "source": "observe_pre_llm",
                    "reason": done_reason,
                },
            )
            return AgentState(
                status="done",
                summary=f"检测到提交成功终态：{done_reason}",
                page_fingerprint=page_fingerprint,
            )

        # 3. 获取页面 URL 并检测页面变化
        try:
            current_url = self.page.url
        except Exception:
            current_url = "unknown"

        # 页面变化检测：URL 变化时重置状态并标记
        is_new_page = False
        if self.last_url is not None and self.last_url != current_url:
            is_new_page = True
            self._log(f"🔄 检测到页面跳转: {current_url}")
            self.history.append("[页面跳转] 新页面，需要重新扫描空缺字段并规划")
            self.consecutive_failures = 0  # 重置连续失败计数
        self.last_url = current_url

        # 3.5 记录快照用于复盘
        self._step_log(
            event="snapshot",
            payload={
                "step": self.step_count,
                "url": current_url,
                "snapshot_lines": snapshot_text.count("\n")
                + (1 if snapshot_text else 0),
                "snapshot_preview": snapshot_text[:2000],
            },
        )

        # 关键修复：在任何宏任务决策前刷新上传等运行时信号
        self._refresh_runtime_signals(visible_text, snapshot_map)

        macro_action = self._maybe_get_macro_action(
            snapshot_map=snapshot_map,
            page_fingerprint=page_fingerprint,
        )
        if self._macro_manual_block_reason:
            reason = self._macro_manual_block_reason
            self._set_manual_reason_hint(reason)
            return AgentState(
                status="stuck",
                summary=f"检测到当前页面存在必须人工处理的附件要求：{reason}",
                page_fingerprint=page_fingerprint,
            )
        if macro_action is not None:
            remaining = [
                line
                for line in summarize_macro_tasks(self._macro_tasks)
                if ":done:" not in line
            ]
            return AgentState(
                status="continue",
                summary="执行全局宏任务链中的当前步骤（语义树计划）",
                next_action=macro_action,
                action_plan=remaining if remaining else None,
                page_fingerprint=page_fingerprint,
            )
        if self._macro_tasks:
            pending = [t for t in self._macro_tasks if t.status != "done"]
            if pending and all(t.status == "blocked" for t in pending):
                blocked_summary = "; ".join(
                    f"{t.task_id}:{t.mapping_reason or t.title}" for t in pending[:4]
                )
                self._step_log(
                    "macro_plan_blocked",
                    {
                        "step": self.step_count,
                        "blocked_tasks": [t.task_id for t in pending[:10]],
                        "summary": blocked_summary,
                    },
                )
                self.history.append(
                    f"宏任务队列全部阻断，切换到 LLM 修复模式：{blocked_summary}"
                )
                self._macro_disabled_scopes.add(self._stable_page_scope())
                self._macro_tasks = []
                self._active_macro_task_id = None
        has_pending_macro = any(
            task.status not in ("done", "blocked") for task in self._macro_tasks
        )
        if not has_pending_macro:
            progression_action = self._maybe_get_progression_queue_action(snapshot_map)
            if progression_action is not None:
                return AgentState(
                    status="continue",
                    summary="宏任务队列已完成，执行提交流程步骤",
                    next_action=progression_action,
                    action_plan=["提交申请（队列）"],
                    page_fingerprint=page_fingerprint,
                )

        self._latest_failure_memory_summary = "无"
        should_load_failure_memory, failure_memory_reason = (
            self._should_query_failure_memory(
                page_state=page_state,
                question_blocks=question_blocks,
                has_pending_macro_tasks=has_pending_macro,
            )
        )
        self._step_log(
            "failure_memory_query_decision",
            {
                "step": self.step_count,
                "enabled": bool(should_load_failure_memory),
                "reason": failure_memory_reason,
            },
        )
        if should_load_failure_memory:
            self._latest_failure_memory_summary = self._load_failure_memory_hints(
                page_scope=self._stable_page_scope(),
                question_blocks=question_blocks,
            )

        cached_state = self._state_cache_by_fingerprint.get(page_fingerprint)
        if (
            cached_state
            and cached_state.next_action is not None
            and cached_state.status == "continue"
        ):
            # Guard: never replay click on toggle elements (checkbox/radio/toggle button)
            _ca = cached_state.next_action
            _is_toggle_replay = False
            _is_risky_replay = bool(_ca.target_question) or _ca.action in (
                "upload",
                "refresh",
            )
            if _ca.action == "click" and _ca.ref:
                _target = snapshot_map.get(_ca.ref)
                if _target and _target.role in ("checkbox", "radio", "switch"):
                    _is_toggle_replay = True
                elif _target and _target.checked is not None:
                    _is_toggle_replay = True
                if self._is_progression_action(_ca, item=_target):
                    _is_risky_replay = True
                # region agent log
                append_debug_log(
                    location="vision_agent.py:_observe_and_think:cache_toggle_guard",
                    message="cache hit toggle guard evaluation",
                    data={
                        "job_id": self.job_id,
                        "step": self.step_count,
                        "cached_action": _ca.action,
                        "cached_ref": _ca.ref,
                        "cached_element_type": _ca.element_type,
                        "target_found": _target is not None,
                        "target_role": _target.role if _target else None,
                        "target_name": (_target.name or "")[:60] if _target else None,
                        "target_checked": _target.checked if _target else "N/A",
                        "target_input_type": _target.input_type if _target else None,
                        "_is_toggle_replay": _is_toggle_replay,
                        "_is_risky_replay": _is_risky_replay,
                        "page_fingerprint": page_fingerprint[:32],
                    },
                    run_id="debug-v2",
                    hypothesis_id="H1",
                )
                # endregion

            cache_key = self._action_fail_key(
                page_fingerprint, cached_state.next_action
            )
            if (
                not _is_toggle_replay
                and not _is_risky_replay
                and self._action_fail_counts.get(cache_key, 0) == 0
                and self._action_cache_use_counts.get(cache_key, 0) < 1
            ):
                self._action_cache_use_counts[cache_key] = (
                    self._action_cache_use_counts.get(cache_key, 0) + 1
                )
                # region agent log
                append_debug_log(
                    location="vision_agent.py:_observe_and_think:cache_replay_accepted",
                    message="cache replay ACCEPTED",
                    data={
                        "job_id": self.job_id,
                        "step": self.step_count,
                        "action": _ca.action,
                        "ref": _ca.ref,
                        "element_type": _ca.element_type,
                        "page_fingerprint": page_fingerprint[:32],
                    },
                    run_id="debug-v2",
                    hypothesis_id="H1",
                )
                # endregion
                self._log("⚡ 页面稳定，复用上一步计划缓存")
                return replace(
                    cached_state,
                    summary=f"{cached_state.summary}（缓存计划）",
                    page_fingerprint=page_fingerprint,
                )

        # 4. 构建 prompt
        history_text = "\n".join(self.history[-5:]) if self.history else "无"
        upload_signals = self._last_upload_signals
        upload_signal_text = "；".join(upload_signals[:8]) if upload_signals else "无"
        upload_candidates_text = (
            "\n".join(f"- {Path(p).name} | {p}" for p in self.upload_candidates[:12])
            if self.upload_candidates
            else "- （白名单目录下暂无可上传文件）"
        )

        # 获取用户个人信息和操作规范
        user_info = get_user_info_for_prompt()
        agent_guidelines = load_agent_guidelines()
        system_prompt = build_system_prompt(
            user_info=user_info,
            agent_guidelines=agent_guidelines,
        )
        user_prompt = build_user_prompt(
            history_text=history_text,
            visible_text=visible_text,
            snapshot_text=snapshot_text,
            question_blocks_text=question_blocks_text,
            form_graph_text=form_graph_text,
            upload_signal_text=upload_signal_text,
            simplify_state=self.simplify_state,
            simplify_message=self.simplify_message,
            assist_required_before=self.assist_required_before,
            assist_required_after=self.assist_required_after,
            assist_prefill_delta=self.assist_prefill_delta,
            assist_prefill_verified=self.assist_prefill_verified,
            failure_memory_text=self._latest_failure_memory_summary,
            upload_candidates_text=upload_candidates_text,
            is_new_page=is_new_page,
        )

        # 5. 调用 LLM（带模型降级机制）
        self._log(f"🤔 正在思考... (模型: {self.model})")
        use_vision, vision_reason = self._should_use_vision_fallback(
            page_state=page_state,
            snapshot_map=snapshot_map,
            visible_text=visible_text,
        )
        self._step_log(
            "visual_fallback_decision",
            {
                "step": self.step_count,
                "use_vision": use_vision,
                "reason": vision_reason,
                "used": self.visual_fallback_used,
                "budget": self.visual_fallback_budget,
            },
        )
        should_capture = self._should_capture_step_screenshot(use_vision=use_vision)
        self._step_log(
            "screenshot_capture_decision",
            {
                "step": self.step_count,
                "mode": self.step_screenshot_mode,
                "should_capture": should_capture,
                "use_vision": use_vision,
            },
        )
        if should_capture:
            screenshot_b64 = self._capture_step_screenshot()

        user_content: list[dict] = [{"type": "text", "text": user_prompt}]
        if use_vision and screenshot_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                }
            )
            self.visual_fallback_used += 1
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        # region agent log
        self._ndjson_log(
            hypothesis_id="H1",
            location="vision_agent:_observe_and_think:before_llm",
            message="pre LLM call",
            data={
                "model": self.model,
                "step": self.step_count,
                "use_vision": use_vision,
                "vision_reason": vision_reason,
                "vision_used": self.visual_fallback_used,
                "vision_budget": self.visual_fallback_budget,
                "screenshot_b64_len": len(screenshot_b64 or ""),
                "visible_text_len": len(visible_text),
                "upload_signals": upload_signals[:5],
                "upload_candidates_count": len(self.upload_candidates),
            },
        )
        # endregion

        call_result = run_chat_with_fallback(
            client=self.client,
            fallback_models=self.fallback_models,
            start_model_index=self.model_index,
            messages=messages,
            temperature=self.llm_cfg.get("temperature", 0.2),
            max_tokens=self.llm_cfg.get("max_tokens", 1000),
            on_log=lambda level, msg: self._log(
                msg,
                "warn" if level == "warn" else "info",
            ),
            sleep_seconds=1.0,
        )
        self.model_index = call_result.model_index
        self.model = call_result.model or self.fallback_models[self.model_index]
        if not call_result.ok:
            self._log(f"❌ {call_result.error_summary or 'LLM 调用失败'}", "error")
            return AgentState(
                status="error",
                summary=call_result.error_summary or "LLM 调用失败",
            )
        raw = call_result.raw
        # region agent log
        self._ndjson_log(
            hypothesis_id="H2",
            location="vision_agent:_observe_and_think:after_llm",
            message="llm raw response",
            data={
                "model": self.model,
                "step": self.step_count,
                "raw_prefix": raw[:200],
            },
        )
        # endregion

        # 6. 解析返回
        data = self._safe_parse_json(raw)
        if not isinstance(data, dict):
            if raw_response_implies_completion(raw):
                done_from_raw, done_reason = self._verify_completion()
                if done_from_raw:
                    self._step_log(
                        event="terminal_success_detected",
                        payload={
                            "step": self.step_count,
                            "source": "raw_parse_fallback",
                            "reason": done_reason,
                            "raw_prefix": raw[:200],
                        },
                    )
                    return AgentState(
                        status="done",
                        summary=f"检测到提交成功终态：{done_reason}",
                        raw_response=raw,
                        page_fingerprint=page_fingerprint,
                    )
            self._llm_parse_fail_streak += 1
            is_refusal = self._is_llm_refusal_response(raw)
            if is_refusal:
                self._llm_refusal_streak += 1
            else:
                self._llm_refusal_streak = 0
            self._log(f"❌ LLM 返回格式错误: {raw[:300]}", "error")
            self._step_log(
                "llm_parse_fail",
                {
                    "step": self.step_count,
                    "raw_prefix": raw[:220],
                    "is_refusal": is_refusal,
                    "parse_fail_streak": self._llm_parse_fail_streak,
                    "refusal_streak": self._llm_refusal_streak,
                },
            )
            # region agent log
            self._ndjson_log(
                hypothesis_id="H3",
                location="vision_agent:_observe_and_think:parse_fail",
                message="parse fail",
                data={
                    "model": self.model,
                    "step": self.step_count,
                    "raw_prefix": raw[:200],
                },
            )
            # endregion

            blocked_like = self._looks_like_external_blocked_text(visible_text) or bool(
                self._last_submission_outcome
                and self._last_submission_outcome.classification == "external_blocked"
            )
            if is_refusal and blocked_like:
                if self.refresh_attempts < self.max_refresh_attempts:
                    self._step_log(
                        "llm_parse_fail_fallback",
                        {
                            "step": self.step_count,
                            "reason_code": "blocked_page_refusal_refresh",
                            "action": "refresh",
                            "refresh_attempts": self.refresh_attempts,
                            "refresh_limit": self.max_refresh_attempts,
                        },
                    )
                    return AgentState(
                        status="continue",
                        summary="LLM 拒答且检测到外部阻断，执行刷新重开",
                        next_action=AgentAction(
                            action="refresh",
                            reason="LLM refusal fallback on externally blocked page",
                        ),
                        raw_response=raw,
                        page_fingerprint=page_fingerprint,
                    )
                self._step_log(
                    "llm_parse_fail_fallback",
                    {
                        "step": self.step_count,
                        "reason_code": "blocked_page_refusal_refresh_exhausted",
                        "action": "stop_to_manual",
                        "refresh_attempts": self.refresh_attempts,
                        "refresh_limit": self.max_refresh_attempts,
                    },
                )
                return AgentState(
                    status="stuck",
                    summary="外部阻断且 LLM 连续拒答，刷新次数已耗尽，需要人工处理",
                    raw_response=raw,
                    page_fingerprint=page_fingerprint,
                )

            if self._llm_parse_fail_streak >= 2:
                deterministic_action = self._maybe_get_progression_queue_action(
                    snapshot_map
                )
                if deterministic_action is not None:
                    self._step_log(
                        "llm_parse_fail_fallback",
                        {
                            "step": self.step_count,
                            "reason_code": "deterministic_progression_fallback",
                            "action": deterministic_action.action,
                            "ref": deterministic_action.ref,
                            "selector": deterministic_action.selector,
                        },
                    )
                    return AgentState(
                        status="continue",
                        summary="LLM 连续格式错误，回退到确定性流程动作",
                        next_action=deterministic_action,
                        raw_response=raw,
                        page_fingerprint=page_fingerprint,
                    )
                self._step_log(
                    "llm_parse_fail_fallback",
                    {
                        "step": self.step_count,
                        "reason_code": "parse_fail_exhausted",
                        "action": "stop_to_manual",
                        "parse_fail_streak": self._llm_parse_fail_streak,
                    },
                )
                return AgentState(
                    status="stuck",
                    summary="LLM 连续返回无效格式，停止自动重试并转人工",
                    raw_response=raw,
                    page_fingerprint=page_fingerprint,
                )
            return AgentState(
                status="error", summary="LLM 返回格式错误", raw_response=raw
            )

        self._llm_parse_fail_streak = 0
        self._llm_refusal_streak = 0
        parsed = parse_agent_response_payload(
            data,
            simplify_state=self.simplify_state,
            assist_prefill_verified=self.assist_prefill_verified,
            assist_prefill_delta=self.assist_prefill_delta,
            sanitize_claims=self._sanitize_simplify_claims,
        )
        next_action = None
        next_action_payload = parsed.get("next_action")
        if isinstance(next_action_payload, dict):
            next_action = AgentAction(
                action=next_action_payload.get("action", ""),
                ref=next_action_payload.get("ref"),
                selector=next_action_payload.get("selector"),
                value=next_action_payload.get("value"),
                target_question=next_action_payload.get("target_question"),
                element_type=next_action_payload.get("element_type"),
                reason=next_action_payload.get("reason"),
            )

        result_state = AgentState(
            status=str(parsed.get("status", "continue")),
            summary=str(parsed.get("summary", "")),
            next_action=next_action,
            raw_response=raw,
            page_overview=parsed.get("page_overview"),
            field_audit=parsed.get("field_audit"),
            action_plan=parsed.get("action_plan"),
            risk_or_blocker=parsed.get("risk_or_blocker"),
            page_fingerprint=page_fingerprint,
        )
        if result_state.status == "continue" and result_state.next_action is not None:
            self._state_cache_by_fingerprint[page_fingerprint] = result_state
            cache_key = self._action_fail_key(
                page_fingerprint, result_state.next_action
            )
            self._action_cache_use_counts[cache_key] = 0
        return result_state

    def _execute_action(self, action: AgentAction) -> bool:
        """
        执行单个操作，返回是否成功。
        根据 element_type 智能选择定位策略，像人类一样快速操作。
        """
        try:
            # 优先使用 ref 执行，降低误定位
            if action.ref:
                return self._execute_ref_action(action)
            self._log_action_executed(action, source="selector")
            before_url, before_excerpt, before_fp = self._capture_page_change_markers()
            success = False

            if action.action == "click":
                if self._has_question_binding(action):
                    bound = self._try_question_binding_click(action)
                    if bound is True:
                        success = True
                        self._log_action_verified(action, ok=success)
                        return success
                    if bound is False:
                        success = False
                        self._log_action_verified(action, ok=success)
                        return success
                if self._is_progression_action(action):
                    blocked_reason = self._get_progression_block_reason()
                    if blocked_reason:
                        self._log(f"⚠ 阻止盲目前进：{blocked_reason}", "warn")
                        success = False
                        self._log_action_verified(action, ok=success)
                        return success
                success = self._smart_click(action.selector, action.element_type)
                if success:
                    success = self._verify_non_ref_action_effect(
                        action,
                        before_url=before_url,
                        before_excerpt=before_excerpt,
                        before_fp=before_fp,
                    )
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "fill":
                success = self._smart_fill(action.selector, action.value)
                if success:
                    success = self._verify_non_ref_action_effect(
                        action,
                        before_url=before_url,
                        before_excerpt=before_excerpt,
                        before_fp=before_fp,
                    )
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "type":
                success = self._smart_type(action.selector, action.value)
                if success:
                    success = self._verify_non_ref_action_effect(
                        action,
                        before_url=before_url,
                        before_excerpt=before_excerpt,
                        before_fp=before_fp,
                    )
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "select":
                success = self._do_select(action.selector, action.value)
                if success:
                    success = self._verify_non_ref_action_effect(
                        action,
                        before_url=before_url,
                        before_excerpt=before_excerpt,
                        before_fp=before_fp,
                    )
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "upload":
                success = self._do_upload(action)
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "scroll":
                direction = action.value or action.selector or "down"
                success = self._do_scroll(direction)
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "refresh":
                success = self._do_refresh(trigger="llm_action")
                self._log_action_verified(action, ok=success)
                return success

            elif action.action == "wait":
                seconds = int(action.value or 2)
                self.page.wait_for_timeout(seconds * 1000)
                success = True
                self._log_action_verified(action, ok=success)
                return success

            elif action.action in ("done", "stuck"):
                success = True
                self._log_action_verified(action, ok=success)
                return success

            else:
                self._log(f"未知操作类型: {action.action}", "warn")
                success = False
                self._log_action_verified(action, ok=success)
                return success

        except Exception as e:
            self._log(f"执行异常: {e}", "error")
            self._log_action_verified(action, ok=False)
            return False

    def _log_action_executed(self, action: AgentAction, *, source: str) -> None:
        self._step_log(
            "action_executed",
            {
                "step": self.step_count,
                "action": action.action,
                "ref": action.ref,
                "selector": action.selector,
                "target_question": action.target_question,
                "source": source,
            },
        )

    def _log_action_verified(self, action: AgentAction, *, ok: bool) -> None:
        payload = {
            "step": self.step_count,
            "action": action.action,
            "ref": action.ref,
            "selector": action.selector,
            "target_question": action.target_question,
            "ok": bool(ok),
        }
        # backward compatibility with existing log consumers
        self._step_log("action_verify", payload)
        self._step_log("action_verified", payload)

    def _capture_page_change_markers(self) -> tuple[str, str, str]:
        """抓取轻量页面标记用于非-ref 动作后验。"""
        try:
            before_url = self.page.url or ""
        except Exception:
            before_url = ""
        try:
            before_excerpt = (self.page.inner_text("body") or "")[:1200]
        except Exception:
            before_excerpt = ""
        before_fp = ""
        try:
            _text, snap = build_ui_snapshot(self.page)
            before_fp = self._build_page_fingerprint(before_url, snap)
        except Exception:
            before_fp = ""
        return before_url, before_excerpt, before_fp

    def _verify_non_ref_action_effect(
        self,
        action: AgentAction,
        *,
        before_url: str,
        before_excerpt: str,
        before_fp: str,
    ) -> bool:
        """非 ref 路径后验：URL / 语义快照 / 文本至少有一项变化。"""
        if action.action not in ("click", "fill", "type", "select"):
            return True
        source_item = self._last_snapshot_map.get(action.ref or "")
        if self._is_progression_action(action, item=source_item):
            return True
        if self._has_question_binding(action) and action.selector:
            if self._verify_question_option_state(
                action.target_question or "", action.selector
            ):
                return True
        try:
            after_url = self.page.url or ""
        except Exception:
            after_url = ""
        try:
            after_excerpt = (self.page.inner_text("body") or "")[:1200]
        except Exception:
            after_excerpt = ""
        after_fp = ""
        try:
            _text, snap = build_ui_snapshot(self.page)
            after_fp = self._build_page_fingerprint(after_url, snap)
        except Exception:
            after_fp = ""
        if before_url and after_url and before_url != after_url:
            return True
        if before_fp and after_fp and before_fp != after_fp:
            return True
        if before_excerpt and after_excerpt and before_excerpt != after_excerpt:
            return True
        if action.action in ("fill", "type", "select"):
            # 输入类操作在某些站点不会立刻刷新文本，避免误判。
            return True
        return False

    def _execute_ref_action(self, action: AgentAction) -> bool:
        """基于快照 ref 执行动作（确定性定位）。"""
        self._log_action_executed(action, source="ref")
        item = self._last_snapshot_map.get(action.ref or "")
        if not item:
            self._log(f"ref 不存在: {action.ref}", "warn")
            return False

        locator = self._locator_from_snapshot_item(item)
        if locator is None:
            return False

        try:
            if action.action == "click":
                if self._has_question_binding(action):
                    bound = self._try_question_binding_click(action)
                    if bound is True:
                        self._log_action_verified(action, ok=True)
                        return True
                    if bound is False:
                        self._log_action_verified(action, ok=False)
                        return False
                if self._is_progression_action(action, item=item):
                    blocked_reason = self._get_progression_block_reason()
                    if blocked_reason:
                        self._log(f"⚠ 阻止盲目前进：{blocked_reason}", "warn")
                        return False
                locator.click(timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._log_action_verified(action, ok=True)
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action == "fill":
                if action.value is None:
                    return False
                locator.fill(str(action.value), timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._log_action_verified(action, ok=True)
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action == "type":
                if action.value is None:
                    return False
                locator.click(timeout=800)
                locator.type(str(action.value), delay=40)
                if self._verify_ref_action_effect(action, locator, item):
                    self._log_action_verified(action, ok=True)
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action == "select":
                if action.value is None:
                    return False
                try:
                    locator.select_option(label=str(action.value), timeout=2000)
                except Exception:
                    locator.click(timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._log_action_verified(action, ok=True)
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action == "upload":
                ok = self._do_upload(action, locator=locator)
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action == "scroll":
                direction = action.value or action.selector or "down"
                ok = self._do_scroll(direction)
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action == "refresh":
                ok = self._do_refresh(trigger="llm_action")
                self._log_action_verified(action, ok=ok)
                return ok
            if action.action in ("wait", "done", "stuck"):
                if action.action == "wait":
                    seconds = int(action.value or 2)
                    self.page.wait_for_timeout(seconds * 1000)
                self._log_action_verified(action, ok=True)
                return True
        except Exception as e:
            self._log(f"ref 执行失败: {e}", "warn")
            self._log_action_verified(action, ok=False)
            return False

        return False

    def _locator_from_snapshot_item(self, item: SnapshotItem):
        """从快照项构建定位器。"""
        try:
            if item.role == "file_input":
                locator = self.page.locator("input[type='file']")
                return locator.nth(item.nth)
            locator = self.page.get_by_role(item.role, name=item.name)
            return locator.nth(item.nth)
        except Exception:
            return None

    def _detect_upload_signals(self, visible_text: str) -> list[str]:
        """
        检测页面是否存在“需要上传文件”的信号，避免盲目上传。
        """
        signals: list[str] = []

        try:
            input_count = self.page.locator("input[type='file']").count()
        except Exception:
            input_count = 0
        if input_count > 0:
            signals.append(f"input[type=file] x{input_count}")

        # 首选语义意图：通过快照元素名称和页面文本识别“上传诉求”
        upload_refs = [
            ref
            for ref, intents in self._last_snapshot_intents.items()
            if "upload_request" in intents
        ]
        if upload_refs:
            signals.append(f"intent:upload_request refs={len(upload_refs)}")

        page_text_intents = self._infer_text_intents(visible_text, limit=1200)
        if "upload_request" in page_text_intents:
            signals.append("intent:upload_request text")

        return signals

    def _refresh_runtime_signals(
        self, visible_text: str, snapshot_map: dict[str, SnapshotItem]
    ) -> None:
        """每步刷新运行时信号，避免宏任务分支读取陈旧状态。"""
        upload_signals = self._detect_upload_signals(visible_text)
        dom_upload_count = sum(
            1
            for item in snapshot_map.values()
            if item.role == "file_input" or (item.input_type or "").lower() == "file"
        )
        if dom_upload_count > 0:
            upload_signals.append(f"dom:file_input x{dom_upload_count}")
        self._last_upload_signals = upload_signals
        self._step_log(
            "runtime_signals_refreshed",
            {
                "step": self.step_count,
                "upload_signal_count": len(upload_signals),
                "upload_signals": upload_signals[:8],
            },
        )

    def _build_required_dom_summary(
        self,
        snapshot_map: dict[str, SnapshotItem],
        question_blocks: list[QuestionBlock],
    ) -> str:
        required_fields: list[str] = []
        for item in snapshot_map.values():
            role = (item.role or "").lower()
            is_required = bool(item.required)
            if role in ("textbox", "combobox", "file_input"):
                if not is_required:
                    continue
                value_hint = (item.value_hint or "").strip()
                if role == "file_input" or not value_hint:
                    required_fields.append(item.name or item.ref)
        required_questions = [
            qb.question_text.strip()
            for qb in question_blocks
            if qb.required and (qb.question_text or "").strip()
        ]
        if not required_questions:
            fallback_questions = [
                qb.question_text.strip()
                for qb in question_blocks
                if (qb.question_text or "").strip() and len(qb.options) >= 2
            ]
            if fallback_questions:
                required_questions = fallback_questions[:4]
        fields_part = ", ".join(required_fields[:6]) if required_fields else "无"
        questions_part = (
            ", ".join(required_questions[:4]) if required_questions else "无"
        )
        return f"字段[{fields_part}]；问题[{questions_part}]"

    def _heuristic_visual_audit(
        self,
        snapshot_map: dict[str, SnapshotItem],
        question_blocks: list[QuestionBlock],
    ) -> VisualAuditResult:
        required_fields: list[str] = []
        required_uploads: list[str] = []
        for item in snapshot_map.values():
            if not item.required:
                continue
            role = (item.role or "").lower()
            if role in ("textbox", "combobox"):
                required_fields.append(item.name or item.ref)
            if role == "file_input" or (item.input_type or "").lower() == "file":
                required_uploads.append(item.name or "Resume")
        required_questions = [
            qb.question_text.strip()
            for qb in question_blocks
            if qb.required and (qb.question_text or "").strip()
        ]
        if not required_questions:
            required_questions = [
                qb.question_text.strip()
                for qb in question_blocks
                if (qb.question_text or "").strip() and len(qb.options) >= 2
            ]
        summary = (
            f"检测到必填字段 {len(required_fields)} 项，"
            f"必答问题 {len(required_questions)} 项，"
            f"必传附件 {len(required_uploads)} 项。"
        )
        return VisualAuditResult(
            visual_summary=summary,
            required_fields=required_fields[:12],
            required_questions=required_questions[:12],
            required_uploads=required_uploads[:8],
            source="heuristic",
        )

    def _run_scope_visual_audit(
        self,
        *,
        scope: str,
        snapshot_map: dict[str, SnapshotItem],
        question_blocks: list[QuestionBlock],
        force: bool = False,
    ) -> VisualAuditResult:
        """
        新 scope 初始规划前做一次截图+语义交叉审计。
        若模型不可用，则回退到 DOM 启发式审计。
        """
        cached = self._scope_visual_audits.get(scope)
        if cached and not force:
            return cached

        result = self._heuristic_visual_audit(snapshot_map, question_blocks)
        self._step_log(
            "plan_visual_audit_started",
            {
                "step": self.step_count,
                "scope": scope,
                "question_count": len(question_blocks),
            },
        )

        screenshot_b64 = self._capture_step_screenshot()
        if screenshot_b64 and self.client:
            prompt = (
                "You are auditing a job application page screenshot. "
                "Return strict JSON only: "
                '{"visual_summary":"...",'
                '"required_fields":["..."],'
                '"required_questions":["..."],'
                '"required_uploads":["..."]}. '
                "Only include required items that appear mandatory on the page."
            )
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{screenshot_b64}"
                            },
                        },
                    ],
                }
            ]
            llm_result = run_chat_with_fallback(
                client=self.client,
                fallback_models=self.fallback_models,
                start_model_index=self.model_index,
                messages=messages,
                temperature=0.1,
                top_p=0.8,
                max_tokens=320,
                on_log=lambda lvl, msg: self._log(msg, lvl),
            )
            if llm_result.ok:
                self.model_index = llm_result.model_index
                self.model = llm_result.model
                parsed = planner_safe_parse_json(llm_result.raw)
                if isinstance(parsed, dict):
                    visual_summary = str(parsed.get("visual_summary") or "").strip()
                    required_fields = [
                        str(x).strip()
                        for x in (parsed.get("required_fields") or [])
                        if str(x).strip()
                    ][:12]
                    required_questions = [
                        str(x).strip()
                        for x in (parsed.get("required_questions") or [])
                        if str(x).strip()
                    ][:12]
                    required_uploads = [
                        str(x).strip()
                        for x in (parsed.get("required_uploads") or [])
                        if str(x).strip()
                    ][:8]
                    if (
                        visual_summary
                        or required_fields
                        or required_questions
                        or required_uploads
                    ):
                        merged_fields = list(
                            dict.fromkeys(result.required_fields + required_fields)
                        )
                        merged_questions = list(
                            dict.fromkeys(
                                result.required_questions + required_questions
                            )
                        )
                        merged_uploads = list(
                            dict.fromkeys(result.required_uploads + required_uploads)
                        )
                        result = VisualAuditResult(
                            visual_summary=visual_summary or result.visual_summary,
                            required_fields=merged_fields[:12],
                            required_questions=merged_questions[:12],
                            required_uploads=merged_uploads[:8],
                            source="vision+heuristic",
                        )
            else:
                self._step_log(
                    "plan_visual_audit_fallback",
                    {
                        "step": self.step_count,
                        "scope": scope,
                        "error_code": llm_result.error_code,
                        "error_summary": llm_result.error_summary,
                    },
                )

        self._scope_visual_audits[scope] = result
        self._latest_visual_summary = result.visual_summary
        self._step_log(
            "plan_visual_audit_merged",
            {
                "step": self.step_count,
                "scope": scope,
                "source": result.source,
                "required_field_count": len(result.required_fields),
                "required_question_count": len(result.required_questions),
                "required_upload_count": len(result.required_uploads),
            },
        )
        return result

    def _collect_manual_required_evidence(
        self,
        visible_text: str,
        snapshot_map: dict[str, SnapshotItem],
        snapshot_intents: dict[str, set[str]],
    ) -> dict[str, int | bool]:
        """收集登录/验证码判定所需 DOM+文本证据。"""
        page_text_intents = self._infer_text_intents(visible_text, limit=1200)
        evidence, details = mg_collect_manual_required_evidence(
            page=self.page,
            visible_text=visible_text,
            snapshot_map=snapshot_map,
            snapshot_intents=snapshot_intents,
            page_text_intents=page_text_intents,
        )
        # region agent log
        append_debug_log(
            location="vision_agent.py:_collect_manual_required_evidence:captcha",
            message="captcha selector diagnostics",
            data={
                "job_id": self.job_id,
                "url": getattr(self.page, "url", ""),
                "captcha_selector_details": details.get("captcha_selector_details", {}),
                "password_input_count": evidence.get("password_input_count", 0),
                "has_captcha_challenge_text": evidence.get(
                    "has_captcha_challenge_text", False
                ),
                "has_login_button": evidence.get("has_login_button", False),
                "has_apply_cta": evidence.get("has_apply_cta", False),
                "page_text_intents": details.get("page_text_intents", []),
            },
            run_id="pre-fix-debug",
            hypothesis_id="H1",
        )
        # endregion
        return evidence

    def _classify_page_state(
        self,
        snapshot_map: dict[str, SnapshotItem],
        evidence: dict[str, int | bool],
        manual_assessment,
    ) -> str:
        """轻量页面状态分类：login/captcha、职位详情页、申请页。"""
        try:
            current_url = self.page.url or ""
        except Exception:
            current_url = ""
        page_state, stats = mg_classify_page_state(
            snapshot_map=snapshot_map,
            evidence=evidence,
            manual_required=manual_assessment.manual_required,
            current_url=current_url,
        )
        # region agent log
        append_debug_log(
            location="vision_agent.py:_classify_page_state:inputs",
            message="page state classification inputs",
            data={
                "job_id": self.job_id,
                **stats,
            },
            run_id="pre-fix-debug",
            hypothesis_id="H5",
        )
        # endregion
        return page_state

    def _build_apply_entry_action(
        self,
        snapshot_map: dict[str, SnapshotItem],
        snapshot_intents: dict[str, set[str]],
    ) -> AgentAction | None:
        """在职位详情页中优先定位进入申请流程的 Apply 按钮。"""
        try:
            current_url = self.page.url or ""
        except Exception:
            current_url = ""
        picked = mg_select_apply_entry_candidate(
            snapshot_map=snapshot_map,
            snapshot_intents=snapshot_intents,
            current_url=current_url,
        )
        if not picked:
            return None
        return AgentAction(
            action="click",
            ref=picked.ref,
            selector=picked.name,
            element_type=picked.role,
            reason="职位详情页检测到 Apply 入口，先进入申请页",
        )

    def _safe_locator_count(self, selector: str) -> int:
        return mg_safe_locator_count(self.page, selector)

    def _collect_selector_details(self, selectors: list[str]) -> dict[str, dict]:
        return mg_collect_selector_details(self.page, selectors)

    def _count_visible_captcha_challenge(self, selectors: list[str]) -> int:
        """只统计可见验证码挑战节点，排除 recaptcha 法律声明文本。"""
        return mg_count_visible_captcha_challenge(self.page, selectors)

    def _infer_snapshot_intents(
        self,
        snapshot_map: dict[str, SnapshotItem],
        visible_text: str,
    ) -> dict[str, set[str]]:
        """为当前快照中的按钮/链接推断语义意图。"""
        return ie_infer_snapshot_intents(
            snapshot_map,
            visible_text,
            infer_label_intents_fn=self._infer_label_intents,
        )

    def _infer_label_intents(
        self,
        labels: list[str],
        context: str = "",
    ) -> dict[str, set[str]]:
        """
        对一组 UI 文本做语义意图分类。
        优先使用低成本文本模型，失败回退到强共识关键词。
        """
        return ie_infer_label_intents(
            labels,
            context=context,
            intent_cache=self._intent_cache,
            infer_label_intents_with_llm_fn=self._infer_label_intents_with_llm,
        )

    def _infer_label_intents_with_llm(
        self,
        labels: list[str],
        context: str,
    ) -> dict[str, set[str]] | None:
        return ie_infer_label_intents_with_llm(
            client=self.client,
            intent_model=self.intent_model,
            labels=labels,
            context=context,
            safe_parse_json_fn=self._safe_parse_json,
        )

    def _infer_text_intents(self, text: str, limit: int = 1200) -> set[str]:
        """
        对整页文本做语义意图分类（低频、可缓存）。
        只输出少量全局意图。
        """
        return ie_infer_text_intents(
            text,
            limit=limit,
            intent_cache=self._intent_cache,
            client=self.client,
            intent_model=self.intent_model,
            safe_parse_json_fn=self._safe_parse_json,
        )

    def _fallback_label_intents(self, label: str) -> set[str]:
        """当语义模型不可用时，使用极小硬规则集合兜底。"""
        return ie_fallback_label_intents(label)

    def _intent_cache_key(self, labels: list[str], context: str = "") -> str:
        return ie_intent_cache_key(labels, context)

    def _is_progression_action(
        self,
        action: AgentAction,
        item: SnapshotItem | None = None,
    ) -> bool:
        if action.action != "click":
            return False
        name = ""
        if item is not None:
            name = item.name or ""
        elif action.selector:
            name = action.selector
        if not name:
            return False
        label_intents = self._infer_label_intents([name])
        intents = label_intents.get(name, set())
        return "progression_action" in intents or "apply_entry" in intents

    def _is_submission_click_action(
        self,
        action: AgentAction,
        item: SnapshotItem | None = None,
    ) -> bool:
        if action.action != "click":
            return False
        label = ""
        if item is not None:
            label = item.name or ""
        elif action.selector:
            label = action.selector
        lower = (label or "").strip().lower()
        if not lower:
            return False
        if "submit" in lower:
            return True
        if "complete application" in lower or "finish application" in lower:
            return True
        return False

    def _get_progression_block_reason(self) -> str | None:
        """
        前进门控：存在明显错误或必填未填时，阻止 Next/Submit。
        """
        try:
            visible_text = self.page.inner_text("body")
        except Exception:
            visible_text = ""
        evidence = self._collect_form_error_evidence(visible_text)
        self._step_log(
            event="progression_gate_evidence",
            payload={
                "step": self.step_count,
                "url": getattr(self.page, "url", ""),
                "evidence": evidence,
            },
        )

        # 先看强结构化证据，避免“required skills”这类正文干扰
        reason = evaluate_progression_block_reason(
            evidence, llm_confirms_context_error=False
        )
        if reason:
            self._last_progression_block_reason = reason
            snippets = evidence.get("error_snippets", [])
            if isinstance(snippets, list):
                self._last_progression_block_snippets = [str(s)[:180] for s in snippets]
            else:
                self._last_progression_block_snippets = []
            self._record_progression_block_fix_hint(reason, evidence)
            # region agent log
            append_debug_log(
                location="vision_agent.py:_get_progression_block_reason:decision",
                message="progression gate blocked by structured evidence",
                data={
                    "job_id": self.job_id,
                    "step": self.step_count,
                    "url": getattr(self.page, "url", ""),
                    "reason": reason,
                    "evidence": evidence,
                },
                run_id="pre-fix-debug",
                hypothesis_id="H6",
            )
            # endregion
            return reason

        # 仅有关键词命中时，做一次低频语义复核（可缓存）
        global_hits = int(evidence.get("global_error_keyword_hits", 0) or 0)
        if global_hits <= 0:
            # region agent log
            append_debug_log(
                location="vision_agent.py:_get_progression_block_reason:decision",
                message="progression gate allowed without global keyword hits",
                data={
                    "job_id": self.job_id,
                    "step": self.step_count,
                    "url": getattr(self.page, "url", ""),
                    "reason": None,
                    "evidence": evidence,
                },
                run_id="pre-fix-debug",
                hypothesis_id="H6",
            )
            # endregion
            return None
        llm_confirm = self._verify_error_context_with_llm(evidence, visible_text)
        final_reason = evaluate_progression_block_reason(
            evidence, llm_confirms_context_error=llm_confirm
        )
        if final_reason:
            self._last_progression_block_reason = final_reason
            snippets = evidence.get("error_snippets", [])
            if isinstance(snippets, list):
                self._last_progression_block_snippets = [str(s)[:180] for s in snippets]
            else:
                self._last_progression_block_snippets = []
            self._record_progression_block_fix_hint(final_reason, evidence)
        # region agent log
        append_debug_log(
            location="vision_agent.py:_get_progression_block_reason:decision",
            message="progression gate decision after llm verification",
            data={
                "job_id": self.job_id,
                "step": self.step_count,
                "url": getattr(self.page, "url", ""),
                "llm_confirm": llm_confirm,
                "reason": final_reason,
                "evidence": evidence,
            },
            run_id="pre-fix-debug",
            hypothesis_id="H6",
        )
        # endregion
        return final_reason

    def _collect_form_error_evidence(
        self, visible_text: str
    ) -> dict[str, int | list[str]]:
        """收集表单错误相关证据，尽量只看表单上下文。"""
        base = {
            "invalid_field_count": 0,
            "required_empty_count": 0,
            "error_container_hits": 0,
            "local_error_keyword_hits": 0,
            "red_error_hits": 0,
            "global_error_keyword_hits": 0,
            "error_snippets": [],
            "invalid_field_samples": [],
            "required_empty_samples": [],
            "submit_candidates": [],
            "file_upload_state_samples": [],
        }
        error_keywords = [
            "required",
            "missing",
            "invalid",
            "needs corrections",
            "please complete",
            "please fill",
            "error",
            "必填",
            "缺失",
            "错误",
        ]
        lower = (visible_text or "").lower()
        base["global_error_keyword_hits"] = sum(
            1 for kw in error_keywords if kw in lower
        )
        try:
            payload = self.page.evaluate(
                """
                (errorKeywords) => {
                  const toLower = (v) => String(v || "").toLowerCase();
                  const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (!style) return false;
                    if (style.display === "none" || style.visibility === "hidden") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };
                  const parseRgb = (color) => {
                    if (!color) return null;
                    const m = String(color).match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                    if (!m) return null;
                    return { r: Number(m[1]), g: Number(m[2]), b: Number(m[3]) };
                  };
                  const isReddish = (el) => {
                    const rgb = parseRgb(window.getComputedStyle(el).color);
                    if (!rgb) return false;
                    return rgb.r >= 140 && rgb.r > rgb.g + 25 && rgb.r > rgb.b + 25;
                  };
                  const forms = Array.from(document.querySelectorAll("form"));
                  const roots = forms.length > 0 ? forms : [document.body];
                  const labelFor = new Map();
                  document.querySelectorAll("label[for]").forEach((lb) => {
                    const k = String(lb.getAttribute("for") || "").trim();
                    if (k && !labelFor.has(k)) {
                      labelFor.set(k, (lb.innerText || lb.textContent || "").trim());
                    }
                  });
                  const fieldName = (el) => {
                    const aria = String(el.getAttribute("aria-label") || "").trim();
                    if (aria) return aria;
                    const nm = String(el.getAttribute("name") || "").trim();
                    if (nm) return nm;
                    const id = String(el.id || "").trim();
                    if (id && labelFor.has(id)) {
                      const byFor = String(labelFor.get(id) || "").trim();
                      if (byFor) return byFor;
                    }
                    const wrapped = el.closest("label");
                    if (wrapped) {
                      const t = String(wrapped.innerText || wrapped.textContent || "").trim();
                      if (t) return t.slice(0, 80);
                    }
                    const ph = String(el.getAttribute("placeholder") || "").trim();
                    if (ph) return ph;
                    return String(el.id || el.getAttribute("name") || el.tagName || "").trim();
                  };
                  const sampleField = (el) => ({
                    tag: (el.tagName || "").toLowerCase(),
                    type: String(el.getAttribute("type") || "").toLowerCase(),
                    name: fieldName(el).slice(0, 120),
                    required: Boolean(el.required || el.getAttribute("aria-required") === "true"),
                    value_len: "value" in el ? String(el.value || "").trim().length : 0
                  });
                  const inScope = (el) => roots.some((root) => root && root.contains(el));
                  const matchesKeyword = (text) => {
                    const t = toLower(text);
                    return errorKeywords.some((kw) => t.includes(toLower(kw)));
                  };
                  const invalidSet = new Set();
                  const reqEmptySet = new Set();
                  roots.forEach((root) => {
                    if (!root) return;
                    root.querySelectorAll("input, textarea, select").forEach((el) => {
                      if (!isVisible(el)) return;
                      if (el.getAttribute("aria-invalid") === "true" || el.matches(":invalid")) {
                        invalidSet.add(el);
                      }
                      const required = el.required || el.getAttribute("aria-required") === "true";
                      if (required) {
                        const val = "value" in el ? String(el.value || "").trim() : "";
                        if (!val) reqEmptySet.add(el);
                      }
                    });
                  });
                  const selectors = [
                    "[role='alert']",
                    "[aria-live='assertive']",
                    "[class*='error' i]",
                    "[class*='invalid' i]",
                    "[class*='field-error' i]",
                    "[data-testid*='error' i]"
                  ];
                  const nodes = [];
                  selectors.forEach((sel) => {
                    document.querySelectorAll(sel).forEach((el) => {
                      if (isVisible(el) && inScope(el)) nodes.push(el);
                    });
                  });
                  const dedup = Array.from(new Set(nodes));
                  let localKwHits = 0;
                  let redHits = 0;
                  const snippets = [];
                  dedup.forEach((node) => {
                    const text = (node.innerText || node.textContent || "").trim();
                    if (!text) return;
                    if (matchesKeyword(text)) localKwHits += 1;
                    if (isReddish(node)) redHits += 1;
                    if (snippets.length < 6) snippets.push(text.slice(0, 180));
                  });
                  const invalidSamples = Array.from(invalidSet).slice(0, 6).map(sampleField);
                  const requiredEmptySamples = Array.from(reqEmptySet).slice(0, 6).map(sampleField);
                  const submitCandidates = Array.from(
                    document.querySelectorAll("button, input[type='submit']")
                  )
                    .filter((el) => isVisible(el) && inScope(el))
                    .map((el) => ({
                      text: String(el.innerText || el.value || el.getAttribute("aria-label") || "").trim().slice(0, 80),
                      disabled: Boolean(el.disabled),
                      aria_disabled: String(el.getAttribute("aria-disabled") || "").toLowerCase(),
                      type: String(el.getAttribute("type") || "").toLowerCase()
                    }))
                    .filter((it) => {
                      const t = it.text.toLowerCase();
                      return t.includes("submit") || t.includes("apply") || it.type === "submit";
                    })
                    .slice(0, 6);
                  const fileUploadStateSamples = Array.from(
                    document.querySelectorAll("input[type='file']")
                  )
                    .filter((el) => isVisible(el) && inScope(el))
                    .map((el) => {
                      const parent = el.closest("label, div, section, form") || el.parentElement;
                      const parentText = String(parent?.innerText || "").toLowerCase();
                      const hasReplaceText = parentText.includes("replace");
                      const hasUploadText = parentText.includes("upload");
                      const hasUploadedFileName =
                        parentText.includes(".pdf") ||
                        parentText.includes(".doc") ||
                        parentText.includes(".docx");
                      return {
                        name: fieldName(el).slice(0, 120),
                        required: Boolean(el.required || el.getAttribute("aria-required") === "true"),
                        value_len: "value" in el ? String(el.value || "").trim().length : 0,
                        has_replace_text: hasReplaceText,
                        has_upload_text: hasUploadText,
                        has_uploaded_file_name: hasUploadedFileName
                      };
                    })
                    .slice(0, 6);
                  return {
                    invalid_field_count: invalidSet.size,
                    required_empty_count: reqEmptySet.size,
                    error_container_hits: dedup.length,
                    local_error_keyword_hits: localKwHits,
                    red_error_hits: redHits,
                    error_snippets: snippets,
                    invalid_field_samples: invalidSamples,
                    required_empty_samples: requiredEmptySamples,
                    submit_candidates: submitCandidates,
                    file_upload_state_samples: fileUploadStateSamples
                  };
                }
                """,
                error_keywords,
            )
        except Exception:
            payload = {}

        if isinstance(payload, dict):
            for key in (
                "invalid_field_count",
                "required_empty_count",
                "error_container_hits",
                "local_error_keyword_hits",
                "red_error_hits",
            ):
                try:
                    base[key] = int(payload.get(key, 0) or 0)
                except Exception:
                    base[key] = 0
            snippets = payload.get("error_snippets", [])
            if isinstance(snippets, list):
                base["error_snippets"] = [str(s)[:200] for s in snippets[:6]]
            invalid_samples = payload.get("invalid_field_samples", [])
            if isinstance(invalid_samples, list):
                base["invalid_field_samples"] = invalid_samples[:6]
            required_samples = payload.get("required_empty_samples", [])
            if isinstance(required_samples, list):
                base["required_empty_samples"] = required_samples[:6]
            submit_candidates = payload.get("submit_candidates", [])
            if isinstance(submit_candidates, list):
                base["submit_candidates"] = submit_candidates[:6]
            file_upload_state_samples = payload.get("file_upload_state_samples", [])
            if isinstance(file_upload_state_samples, list):
                base["file_upload_state_samples"] = file_upload_state_samples[:6]

        return base

    def _record_progression_block_fix_hint(
        self,
        blocked_reason: str,
        evidence: dict[str, int | list[str]],
    ) -> None:
        snippets = evidence.get("error_snippets", [])
        snippet_list: list[str] = []
        if isinstance(snippets, list):
            snippet_list = [str(s)[:180] for s in snippets[:3]]
        hint = "请先修复报错字段后再继续提交"
        if snippet_list:
            hint = f"{hint}；错误摘要: {' | '.join(snippet_list)}"
        self.history.append(
            f"步骤{self.step_count}: 提交门控拦截 -> {blocked_reason}；{hint}"
        )
        self._step_log(
            "progression_block_with_fix_hint",
            {
                "step": self.step_count,
                "classification": "validation_error",
                "reason_code": "progression_blocked",
                "evidence_snippet": " | ".join(snippet_list)[:220],
                "reason": blocked_reason,
                "hint": hint,
                "error_snippets": snippet_list,
            },
        )

    def _verify_error_context_with_llm(
        self,
        evidence: dict[str, int | list[str]],
        visible_text: str,
    ) -> bool:
        """只在歧义场景下调用 LLM，判断是否为真实表单错误上下文。"""
        if not self.client:
            return False
        cache_payload = json.dumps(
            {
                "evidence": evidence,
                "text": (visible_text or "")[:1200],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        key = hashlib.sha1(cache_payload.encode("utf-8")).hexdigest()
        if key in self._error_gate_cache:
            return self._error_gate_cache[key]

        snippets = evidence.get("error_snippets", [])
        if not isinstance(snippets, list):
            snippets = []
        prompt = {
            "task": "Decide if current page has blocking form-validation errors",
            "rules": [
                "Return true only if errors are clearly about form validation/submission.",
                "Ignore job description text such as 'required skills'.",
                "Prefer field/error-container evidence over generic wording.",
            ],
            "evidence": {
                "invalid_field_count": int(evidence.get("invalid_field_count", 0) or 0),
                "required_empty_count": int(
                    evidence.get("required_empty_count", 0) or 0
                ),
                "error_container_hits": int(
                    evidence.get("error_container_hits", 0) or 0
                ),
                "local_error_keyword_hits": int(
                    evidence.get("local_error_keyword_hits", 0) or 0
                ),
                "red_error_hits": int(evidence.get("red_error_hits", 0) or 0),
                "global_error_keyword_hits": int(
                    evidence.get("global_error_keyword_hits", 0) or 0
                ),
                "error_snippets": [str(s)[:180] for s in snippets[:6]],
            },
            "visible_text_excerpt": (visible_text or "")[:1000],
            "return_json_only": {"is_blocking_error": True, "reason": "brief"},
        }
        try:
            completion = self.client.chat.completions.create(
                model=self.fallback_models[0],
                temperature=0.0,
                max_tokens=160,
                messages=[
                    {
                        "role": "system",
                        "content": "You validate form error context. Return strict JSON only.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(prompt, ensure_ascii=False),
                    },
                ],
            )
            raw = completion.choices[0].message.content or ""
            data = self._safe_parse_json(raw)
            verdict = bool(data and data.get("is_blocking_error") is True)
        except Exception:
            verdict = False

        self._error_gate_cache[key] = verdict
        return verdict

    def _count_empty_required_fields(self) -> int:
        """
        尝试统计当前快照里明显为空的 required 输入字段。
        """
        total = 0
        for item in self._last_snapshot_map.values():
            if not item.required:
                continue
            if item.role not in ("textbox", "combobox"):
                continue
            locator = self._locator_from_snapshot_item(item)
            if locator is None:
                continue
            value = self._get_input_value(locator).strip()
            if not value:
                total += 1
        return total

    def _verify_ref_action_effect(
        self, action: AgentAction, locator, item: SnapshotItem
    ) -> bool:
        """对 ref 动作进行基础后验校验，失败则返回 False 触发重试。"""
        return verifier_verify_ref_action_effect(
            action,
            locator,
            item,
            is_answer_click_action=self._is_answer_click_action,
            verify_question_answer_state=self._verify_question_answer_state,
            verify_question_option_state=self._verify_question_option_state,
        )

    def _retry_ref_action(
        self, action: AgentAction, locator, item: SnapshotItem
    ) -> bool:
        """当后验失败时，尝试一次更稳妥的补救动作。"""
        try:
            if action.action in ("fill", "type"):
                if action.value is None:
                    return False
                locator.fill(str(action.value), timeout=1500)
                return self._verify_ref_action_effect(action, locator, item)
            if action.action == "click" and item.role in ("checkbox", "radio"):
                try:
                    locator.check(timeout=1500)
                except Exception:
                    locator.click(timeout=1500)
                return self._verify_ref_action_effect(action, locator, item)
            if action.action == "click":
                try:
                    locator.scroll_into_view_if_needed(timeout=1500)
                    locator.click(timeout=1500)
                    return self._verify_ref_action_effect(action, locator, item)
                except Exception:
                    return False
        except Exception:
            return False
        return False

    def _get_input_value(self, locator) -> str:
        """尽力获取输入框当前值。"""
        return verifier_get_input_value(locator)

    def _is_dropdown_open(self, locator) -> bool:
        """检测 autocomplete 下拉是否打开（aria-expanded）。"""
        return verifier_is_dropdown_open(locator)

    def _normalize_answer_label(self, text: str | None) -> str:
        return verifier_normalize_answer_label(text)

    def _normalize_option_text(self, text: str | None) -> str:
        value = (text or "").strip().lower()
        value = re.sub(r"[\s\u00a0]+", " ", value)
        value = re.sub(r"[^\w\s]", "", value)
        return value.strip()

    def _option_text_matches(self, left: str | None, right: str | None) -> bool:
        left_norm = self._normalize_option_text(left)
        right_norm = self._normalize_option_text(right)
        if not left_norm or not right_norm:
            return False
        if left_norm == right_norm:
            return True
        if left_norm in right_norm or right_norm in left_norm:
            return True
        return False

    def _has_question_binding(self, action: AgentAction) -> bool:
        return bool((action.target_question or "").strip())

    def _is_answer_click_action(
        self, action: AgentAction, item: SnapshotItem | None = None
    ) -> bool:
        if action.action != "click":
            return False
        label = action.selector or ""
        if not label and item is not None:
            label = item.name or ""
        if not label and action.ref:
            snapshot_item = self._last_snapshot_map.get(action.ref)
            if snapshot_item:
                label = snapshot_item.name or ""
        return self._normalize_answer_label(label) in ("yes", "no")

    def _try_question_binding_click(self, action: AgentAction) -> bool | None:
        """
        对目标问题优先执行“问题绑定点击”（支持 Yes/No、A/B/C 及通用选项）。
        返回：
        - True：绑定点击成功且后验通过
        - False：绑定点击已执行但后验失败
        - None：不适用或定位失败，回退原有点击路径
        """
        question = (action.target_question or "").strip()
        option = (action.selector or "").strip()
        if not option and action.ref:
            target = self._last_snapshot_map.get(action.ref)
            if target:
                option = (target.name or "").strip()
        if not option or not question:
            return None
        payload = self._click_answer_with_question_binding(question, option)
        if not bool(payload.get("ok", False)):
            self._step_log(
                "answer_binding_attempt",
                {
                    "step": self.step_count,
                    "classification": "validation_error",
                    "reason_code": "answer_binding",
                    "evidence_snippet": str(payload.get("reason", ""))[:220],
                    "question": question,
                    "answer": option,
                    "ok": False,
                    "reason": payload.get("reason", ""),
                },
            )
            return False
        try:
            self.page.wait_for_timeout(180)
        except Exception:
            pass
        inspected = self._inspect_question_option_state(question, option)
        verified = bool(
            inspected.get("matched")
            and inspected.get("option_found")
            and inspected.get("option_selected")
        )
        verify_reason = (
            "option_selected_verified" if verified else "option_not_selected"
        )
        self._step_log(
            "answer_binding_attempt",
            {
                "step": self.step_count,
                "classification": "validation_error",
                "reason_code": "answer_binding",
                "evidence_snippet": str(f"{payload.get('reason', '')}|{verify_reason}")[
                    :220
                ],
                "question": question,
                "answer": option,
                "ok": bool(verified),
                "reason": verify_reason,
                "option_found": bool(inspected.get("option_found")),
                "scope_kind": str(inspected.get("scope_kind") or ""),
                "scope_control_count": int(inspected.get("scope_control_count") or 0),
                "selected_options": [
                    str(x) for x in (inspected.get("selected_options") or [])[:6]
                ],
            },
        )
        if not verified and self._is_answer_click_action(action):
            # Yes/No 场景继续兼容旧后验，以覆盖部分站点的 aria 差异。
            answer = self._normalize_answer_label(option)
            if answer in ("yes", "no"):
                verified = self._verify_question_answer_state(question, answer)
        return bool(verified)

    def _try_answer_binding_click(self, action: AgentAction) -> bool | None:
        """
        兼容旧调用：已升级为通用问题绑定点击。
        """
        return self._try_question_binding_click(action)

    def _click_option_with_question_binding(
        self, question: str, option_text: str
    ) -> dict[str, str | bool]:
        """
        在包含问题文本的容器内点击指定选项（通用）。
        """
        try:
            result = self.page.evaluate(
                """
                ({ question, optionText }) => {
                  const norm = (v) => String(v || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const q = norm(question);
                  const optionNorm = norm(optionText);
                  if (!q || !optionNorm) return { ok: false, reason: "missing_question_or_answer" };
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const textOf = (el) => {
                    if (!el) return "";
                    return norm(el.innerText || el.textContent || el.getAttribute("aria-label") || el.value || "");
                  };
                  const clickableNodes = Array.from(
                    document.querySelectorAll(
                      "button, [role='button'], label, input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox']"
                    )
                  ).filter((el) => isVisible(el));
                  const answerCandidates = clickableNodes.filter((el) => {
                    const t = textOf(el);
                    return (
                      t === optionNorm ||
                      t.startsWith(optionNorm + " ") ||
                      optionNorm.startsWith(t + " ")
                    );
                  });
                  if (!answerCandidates.length) {
                    return { ok: false, reason: "answer_candidates_not_found" };
                  }
                  const controlSelectors = "button, [role='button'], label, input[type='radio'], input[type='checkbox'], [role='radio'], [role='checkbox']";
                  const controlsInside = (scope) => {
                    if (!scope) return [];
                    return Array.from(scope.querySelectorAll(controlSelectors)).filter((el) => isVisible(el));
                  };
                  let best = null;
                  let bestScore = -Infinity;
                  for (const candidate of answerCandidates) {
                    let cur = candidate;
                    let depth = 0;
                    let candidateScored = false;
                    while (cur && depth < 8) {
                      const tag = String(cur.tagName || "").toLowerCase();
                      if (tag === "form" || tag === "body" || tag === "html") {
                        break;
                      }
                      const scoreText = textOf(cur);
                      const controls = controlsInside(cur);
                      if (scoreText.includes(q) && controls.length >= 2) {
                        let score = 0;
                        if (tag === "fieldset") score += 90;
                        const role = String(cur.getAttribute?.("role") || "").toLowerCase();
                        if (role === "radiogroup" || role === "group") score += 80;
                        score += Math.max(0, 40 - depth * 5);
                        score -= controls.length * 10;
                        score -= Math.min(scoreText.length / 40, 50);
                        if (score > bestScore) {
                          bestScore = score;
                          best = candidate;
                        }
                        candidateScored = true;
                        break;
                      }
                      cur = cur.parentElement;
                      depth += 1;
                    }
                    if (!candidateScored && depth >= 8) {
                      // no-op; keep searching other candidates
                    }
                  }
                  if (!best) return { ok: false, reason: "question_container_not_found" };
                  try {
                    best.click();
                  } catch (_) {
                    const input = best.querySelector && best.querySelector("input[type='radio'],input[type='checkbox']");
                    if (input) input.click();
                    else return { ok: false, reason: "click_failed" };
                  }
                  return { ok: true, reason: "clicked_in_question_container" };
                }
                """,
                {"question": question, "optionText": option_text},
            )
        except Exception as e:
            return {"ok": False, "reason": f"binding_eval_error:{type(e).__name__}"}
        if isinstance(result, dict):
            return {
                "ok": bool(result.get("ok", False)),
                "reason": str(result.get("reason", ""))[:120],
            }
        return {"ok": False, "reason": "binding_eval_unexpected_payload"}

    def _click_answer_with_question_binding(
        self, question: str, answer: str
    ) -> dict[str, str | bool]:
        """兼容旧调用：answer 语义等同于 option_text。"""
        return self._click_option_with_question_binding(question, answer)

    def _verify_question_answer_state(
        self, question: str, expected_answer: str
    ) -> bool:
        """
        校验目标问题的答案是否已落在预期选项上。
        """
        if not question or expected_answer not in ("yes", "no"):
            return False
        return self._verify_question_option_state(question, expected_answer)

    def _verify_question_option_state(
        self, question: str, expected_option: str
    ) -> bool:
        """
        校验目标问题的选项是否已被选中（节点级后验）。
        """
        if not question or not expected_option:
            return False
        result = self._inspect_question_option_state(question, expected_option)
        return bool(
            result.get("matched")
            and result.get("option_found")
            and result.get("option_selected")
        )

    def _collect_selected_options_for_question(self, question: str) -> list[str]:
        result = self._inspect_question_option_state(question, expected_option="")
        selected = result.get("selected_options") or []
        if not isinstance(selected, list):
            return []
        return [str(x) for x in selected if str(x).strip()][:12]

    def _inspect_question_option_state(
        self, question: str, expected_option: str
    ) -> dict[str, object]:
        """
        返回问题级选项状态详情，供 question_single/question_multi 共用。
        """
        if not question:
            return {
                "matched": False,
                "option_found": False,
                "option_selected": False,
                "selected_options": [],
            }
        try:
            result = self.page.evaluate(
                """
                ({ question, expected }) => {
                  const norm = (v) => String(v || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const q = norm(question);
                  const expectedNorm = norm(expected);
                  if (!q) {
                    return {
                      matched: false,
                      option_found: false,
                      option_selected: false,
                      selected: [],
                    };
                  }
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const textOf = (el) => {
                    if (!el) return "";
                    const text = el.innerText || el.textContent || "";
                    const aria = el.getAttribute ? (el.getAttribute("aria-label") || "") : "";
                    const value = el.value || "";
                    return norm(text || aria || value);
                  };
                  const roleOf = (el) => {
                    const role = String(el?.getAttribute?.("role") || "").toLowerCase();
                    if (role) return role;
                    const tag = String(el?.tagName || "").toLowerCase();
                    const type = String(el?.getAttribute?.("type") || "").toLowerCase();
                    if (tag === "input" && type === "checkbox") return "checkbox";
                    if (tag === "input" && type === "radio") return "radio";
                    return tag || "unknown";
                  };
                  const labelOf = (el) => {
                    if (!el) return "";
                    const role = roleOf(el);
                    if (role === "checkbox" || role === "radio") {
                      const id = el.getAttribute?.("id") || "";
                      if (id) {
                        const byFor = document.querySelector(`label[for="${id}"]`);
                        const txt = textOf(byFor);
                        if (txt) return txt;
                      }
                      const wrap = el.closest?.("label");
                      const wrapText = textOf(wrap);
                      if (wrapText) return wrapText;
                    }
                    const own = textOf(el);
                    if (own) return own;
                    const parentLabel = textOf(el.closest?.("label"));
                    if (parentLabel) return parentLabel;
                    return "";
                  };
                  const selectedOf = (el) => {
                    if (!el) return false;
                    const role = roleOf(el);
                    if (role === "checkbox" || role === "radio") {
                      const checked = el.checked;
                      if (typeof checked === "boolean") return checked;
                    }
                    const ariaChecked = String(el.getAttribute?.("aria-checked") || "").toLowerCase();
                    if (ariaChecked === "true") return true;
                    if (ariaChecked === "false") return false;
                    const ariaPressed = String(el.getAttribute?.("aria-pressed") || "").toLowerCase();
                    if (ariaPressed === "true") return true;
                    if (ariaPressed === "false") return false;
                    const dataState = String(el.getAttribute?.("data-state") || "").toLowerCase();
                    if (dataState === "checked" || dataState === "on" || dataState === "selected") return true;
                    const cls = String(el.className || "").toLowerCase();
                    if (cls.includes("selected") || cls.includes("active") || cls.includes("checked")) return true;
                    if (el.matches?.("label")) {
                      const child = el.querySelector("input[type='checkbox'],input[type='radio']");
                      if (child && typeof child.checked === "boolean") return child.checked;
                    }
                    return false;
                  };
                  const controlSelectors = [
                    "input[type='radio']",
                    "input[type='checkbox']",
                    "[role='radio']",
                    "[role='checkbox']",
                    "button",
                    "[role='button']",
                    "[aria-pressed]",
                    "[aria-checked]",
                    "label"
                  ].join(",");
                  const controlsInside = (scope) => {
                    if (!scope) return [];
                    return Array.from(scope.querySelectorAll(controlSelectors)).filter((el) => isVisible(el));
                  };
                  const scoreScope = (scope) => {
                    const tag = String(scope?.tagName || "").toLowerCase();
                    const role = String(scope?.getAttribute?.("role") || "").toLowerCase();
                    const txt = textOf(scope);
                    const controls = controlsInside(scope);
                    if (!txt.includes(q) || controls.length < 2) return null;
                    if (tag === "form" || tag === "body" || tag === "html") return null;
                    let depth = 0;
                    let cur = scope;
                    while (cur && depth < 20) {
                      depth += 1;
                      cur = cur.parentElement;
                    }
                    let score = 0;
                    if (tag === "fieldset") score += 90;
                    if (role === "radiogroup" || role === "group") score += 80;
                    if (role === "group" && (scope.getAttribute("aria-label") || "").toLowerCase().includes(q)) score += 25;
                    score += Math.min(depth * 3, 60);
                    score -= controls.length * 12;
                    score -= Math.min(txt.length / 30, 80);
                    return { scope, controls, score, txtLen: txt.length };
                  };
                  const primaryCandidates = Array.from(
                    document.querySelectorAll(
                      "fieldset,[role='radiogroup'],[role='group'],[data-testid*='question' i],[class*='question' i],section,li,div"
                    )
                  )
                    .filter((el) => isVisible(el))
                    .map((el) => scoreScope(el))
                    .filter(Boolean);
                  const expectedCandidates = expectedNorm
                    ? Array.from(document.querySelectorAll(controlSelectors)).filter((el) => {
                        if (!isVisible(el)) return false;
                        const label = labelOf(el);
                        if (!label) return false;
                        return (
                          label === expectedNorm ||
                          label.startsWith(expectedNorm + " ") ||
                          expectedNorm.startsWith(label + " ")
                        );
                      })
                    : [];
                  const fallbackCandidates = [];
                  for (const candidate of expectedCandidates.slice(0, 24)) {
                    let cur = candidate;
                    let depth = 0;
                    while (cur && depth < 10) {
                      cur = cur.parentElement;
                      depth += 1;
                      if (!cur) break;
                      const tag = String(cur.tagName || "").toLowerCase();
                      if (tag === "form" || tag === "body" || tag === "html") break;
                      const scored = scoreScope(cur);
                      if (scored) {
                        scored.score += 50 - depth * 2;
                        fallbackCandidates.push(scored);
                        break;
                      }
                    }
                  }
                  const merged = [...primaryCandidates, ...fallbackCandidates];
                  merged.sort((a, b) => {
                    if (b.score !== a.score) return b.score - a.score;
                    if (a.controls.length !== b.controls.length) return a.controls.length - b.controls.length;
                    return a.txtLen - b.txtLen;
                  });
                  const chosen = merged[0] || null;
                  let matched = false;
                  let optionFound = false;
                  let optionSelected = false;
                  const selected = [];
                  if (chosen) {
                    matched = true;
                    for (const el of chosen.controls) {
                      const label = labelOf(el);
                      if (!label) continue;
                      const isSelected = selectedOf(el);
                      if (isSelected) selected.push(label);
                      if (!expectedNorm) continue;
                      if (
                        label === expectedNorm ||
                        label.startsWith(expectedNorm + " ") ||
                        expectedNorm.startsWith(label + " ")
                      ) {
                        optionFound = true;
                        if (isSelected) optionSelected = true;
                      }
                    }
                  }
                  const dedup = Array.from(new Set(selected.filter(Boolean)));
                  if (!expectedNorm) optionFound = true;
                  return {
                    matched,
                    option_found: optionFound,
                    option_selected: optionSelected,
                    selected: dedup,
                    scope_kind: chosen
                      ? `${String(chosen.scope.tagName || "").toLowerCase()}#${String(chosen.scope.getAttribute?.("role") || "").toLowerCase()}`
                      : "",
                    scope_control_count: chosen ? chosen.controls.length : 0,
                  };
                }
                """,
                {"question": question, "expected": expected_option},
            )
        except Exception:
            return {
                "matched": False,
                "option_found": False,
                "option_selected": False,
                "selected_options": [],
            }
        if not isinstance(result, dict):
            return {
                "matched": False,
                "option_found": False,
                "option_selected": False,
                "selected_options": [],
            }
        selected = result.get("selected") or []
        if not isinstance(selected, list):
            selected = []
        return {
            "matched": bool(result.get("matched")),
            "option_found": bool(result.get("option_found")),
            "option_selected": bool(result.get("option_selected")),
            "selected_options": [str(x) for x in selected if str(x).strip()][:12],
            "scope_kind": str(result.get("scope_kind") or ""),
            "scope_control_count": int(result.get("scope_control_count") or 0),
        }

    def _classify_submission_outcome(
        self, action: AgentAction, action_success: bool
    ) -> SubmissionOutcome:
        if action_success:
            done, reason = self._verify_completion()
            if done:
                return SubmissionOutcome(
                    classification="success_confirmed",
                    reason_code="post_submit_terminal_verified",
                    evidence_snippet=(reason or "")[:220],
                )
        evidence = self._extract_outcome_text_evidence()
        block_reason = self._get_progression_block_reason()
        return oc_classify_submission_outcome(
            evidence_text=evidence,
            action_success=action_success,
            progression_block_reason=block_reason,
            progression_block_snippets=self._last_progression_block_snippets,
        )

    def _handle_submission_outcome(
        self, action: AgentAction, action_success: bool
    ) -> tuple[bool, bool]:
        outcome = self._classify_submission_outcome(action, action_success)
        self._last_submission_outcome = outcome
        self._step_log(
            "submission_outcome_classified",
            {
                "step": self.step_count,
                "classification": outcome.classification,
                "reason_code": outcome.reason_code,
                "evidence_snippet": outcome.evidence_snippet,
                "action": action.action,
                "selector": action.selector,
                "ref": action.ref,
            },
        )
        self._step_log(
            "submission_classified",
            {
                "step": self.step_count,
                "classification": outcome.classification,
                "reason_code": outcome.reason_code,
            },
        )
        self._sync_failure_hints(outcome, action)
        if outcome.classification == "success_confirmed":
            return True, False
        if outcome.classification == "validation_error":
            signature = f"{outcome.reason_code}|{(outcome.evidence_snippet or '').strip().lower()}"
            if signature and signature == self._last_validation_signature:
                self._validation_repeat_count += 1
            else:
                self._validation_repeat_count = 1
                self._last_validation_signature = signature
            if self._validation_repeat_count >= 2:
                self._step_log(
                    "progression_block_with_fix_hint",
                    {
                        "step": self.step_count,
                        "classification": "validation_error",
                        "reason_code": "repeat_same_validation_error",
                        "evidence_snippet": outcome.evidence_snippet[:220],
                        "reason": "repeat_same_validation_error",
                        "hint": "同一错误重复出现，下一步必须改为定位并修复具体字段，禁止继续提交",
                        "error_snippets": [outcome.evidence_snippet],
                    },
                )
            self.history.append(
                f"步骤{self.step_count}: 提交后检测到表单校验错误，必须先修复字段；{outcome.evidence_snippet}"
            )
            return False, False
        if outcome.classification in ("external_blocked", "transient_network"):
            key = self._semantic_action_key("", action) or "progression::submit_apply"
            retry_count = self._submission_retry_counts.get(key, 0) + 1
            self._submission_retry_counts[key] = retry_count
            self.retry_count_hint = retry_count
            self._step_log(
                "retry_policy_applied",
                {
                    "step": self.step_count,
                    "classification": outcome.classification,
                    "reason_code": outcome.reason_code,
                    "retry_count": retry_count,
                    "retry_limit": self._submission_retry_limit,
                    "semantic_key": key,
                    "evidence_snippet": outcome.evidence_snippet,
                },
            )
            if (
                outcome.classification == "external_blocked"
                and outcome.reason_code == "anti_spam_or_risk_blocked"
            ):
                refreshed = self._do_refresh(
                    trigger="external_blocked_immediate_restart"
                )
                self._step_log(
                    "retry_policy_applied",
                    {
                        "step": self.step_count,
                        "classification": outcome.classification,
                        "reason_code": "immediate_refresh_restart",
                        "retry_count": retry_count,
                        "retry_limit": self._submission_retry_limit,
                        "semantic_key": key,
                        "refreshed": bool(refreshed),
                    },
                )
                if retry_count >= self._submission_retry_limit:
                    return False, True
                if not refreshed and self.refresh_exhausted:
                    return False, True
                self.history.append(
                    f"步骤{self.step_count}: 检测到 anti-spam/risk 阻断，立即刷新并重开流程（重试 {retry_count}/{self._submission_retry_limit}）"
                )
                return False, False
            if retry_count >= self._submission_retry_limit:
                return False, True
            refresh_attempts = self._submission_refresh_attempts.get(key, 0)
            if (
                retry_count >= 2
                and refresh_attempts < 1
                and hasattr(self.page, "reload")
            ):
                refreshed = self._do_refresh(trigger="submission_blocked_recovery")
                self._submission_refresh_attempts[key] = refresh_attempts + 1
                self._step_log(
                    "retry_policy_applied",
                    {
                        "step": self.step_count,
                        "classification": outcome.classification,
                        "reason_code": "refresh_recovery_attempt",
                        "retry_count": retry_count,
                        "semantic_key": key,
                        "refreshed": bool(refreshed),
                    },
                )
            self._apply_humanized_retry_pacing()
            self.history.append(
                f"步骤{self.step_count}: 提交受阻（{outcome.classification}），已执行合规重试节奏，下一步改策略"
            )
            return False, False
        return False, False

    def _apply_humanized_retry_pacing(self) -> None:
        wait_ms = random.randint(900, 1800)
        try:
            self.page.wait_for_timeout(wait_ms)
        except Exception:
            pass
        try:
            self.page.evaluate("window.scrollBy(0, 120)")
            self.page.wait_for_timeout(200)
            self.page.evaluate("window.scrollBy(0, -80)")
        except Exception:
            pass
        try:
            self.page.keyboard.press("Tab")
            self.page.wait_for_timeout(120)
            self.page.keyboard.press("Shift+Tab")
        except Exception:
            pass

    def _extract_outcome_text_evidence(self) -> str:
        try:
            text = self.page.inner_text("body")
        except Exception:
            text = ""
        snippets = self._last_progression_block_snippets[:2]
        if snippets:
            text = f"{text}\n" + "\n".join(snippets)
        return (text or "")[:3000]

    def _recommended_strategy_for_failure(
        self, classification: str, reason_code: str
    ) -> tuple[str, str]:
        cls = (classification or "").strip().lower()
        code = (reason_code or "").strip().lower()
        if cls == "external_blocked":
            if code == "anti_spam_or_risk_blocked":
                return (
                    "检测到站点风控阻断时，立即刷新页面并完整重开流程；最多 3 次后转人工。",
                    "禁止在同一阻断页面连续重复点击 Submit。",
                )
            return (
                "提交受外部阻断时，先做一次刷新恢复，再执行有限重试。",
                "禁止无证据地无限重试提交动作。",
            )
        if cls == "validation_error":
            return (
                "先定位并修复具体报错字段，再继续提交流程。",
                "禁止在必填错误未修复时直接重复提交。",
            )
        if cls == "transient_network":
            return (
                "等待短暂网络恢复后重试合法入口，失败则切换恢复路径。",
                "禁止连续快速点击导致额外网络抖动。",
            )
        if "precondition_timeout" in code:
            return (
                "前置条件连续不满足时，执行全页重采样并重建计划。",
                "禁止在同一缺失前置条件下反复执行同一任务。",
            )
        return (
            "切换到替代策略并重采样页面语义，再决定下一步。",
            "禁止同语义动作在无状态变化时无限重复。",
        )

    def _record_failure_memory_case(
        self,
        *,
        classification: str,
        reason_code: str,
        symptom: str,
        root_cause: str,
        evidence_snippet: str = "",
        question_text: str = "",
        action: str = "",
        selector: str = "",
        source_event: str = "",
    ) -> None:
        try:
            strategy, guardrails = self._recommended_strategy_for_failure(
                classification, reason_code
            )
            self._failure_memory.upsert_case(
                page_scope=self._stable_page_scope(),
                classification=classification or "unknown_blocked",
                reason_code=reason_code or "unspecified",
                symptom=(symptom or "")[:220],
                root_cause=(root_cause or "")[:220],
                successful_strategy=strategy,
                guardrails=guardrails,
                evidence_snippet=(evidence_snippet or "")[:320],
                question_text=(question_text or "")[:220],
                action=(action or "")[:80],
                selector=(selector or "")[:120],
                source_event=source_event or "runtime",
                status="active",
            )
        except Exception:
            # Failure memory is best-effort; never block runtime.
            return

    def _load_failure_memory_hints(
        self,
        *,
        page_scope: str,
        question_blocks: list[QuestionBlock],
    ) -> str:
        entries = []
        seen: set[str] = set()
        classification = (
            self._last_submission_outcome.classification
            if self._last_submission_outcome is not None
            else ""
        )
        reason_code = (
            self._last_submission_outcome.reason_code
            if self._last_submission_outcome is not None
            else ""
        )
        primary = self._failure_memory.query_similar(
            page_scope=page_scope,
            classification=classification,
            reason_code=reason_code,
            question_text="",
            action="submit",
            limit=2,
        )
        for item in primary:
            if item.signature in seen:
                continue
            seen.add(item.signature)
            entries.append(item)
        for qb in question_blocks[:5]:
            hits = self._failure_memory.query_similar(
                page_scope=page_scope,
                classification="",
                reason_code="",
                question_text=qb.question_text,
                action="click",
                limit=1,
            )
            for item in hits:
                if item.signature in seen:
                    continue
                seen.add(item.signature)
                entries.append(item)
                if len(entries) >= 3:
                    break
            if len(entries) >= 3:
                break
        summary = self._failure_memory.format_hints_for_prompt(entries, max_items=3)
        self._step_log(
            "failure_memory_hints_loaded",
            {
                "step": self.step_count,
                "scope": page_scope,
                "hint_count": len(entries),
                "signatures": [item.signature for item in entries[:5]],
            },
        )
        return summary

    def _should_query_failure_memory(
        self,
        *,
        page_state: str,
        question_blocks: list[QuestionBlock],
        has_pending_macro_tasks: bool,
    ) -> tuple[bool, str]:
        """
        只在“问题态”或“关键提交态”检索 Failure Memory。
        普通稳定填表步骤默认不检索，降低延迟与提示噪音。
        """
        if self.consecutive_failures > 0 or self._same_task_failure_streak > 0:
            return True, "failure_recovery"
        if self._last_progression_block_reason:
            return True, "progression_blocked"
        if self._last_submission_outcome and (
            self._last_submission_outcome.classification != "success_confirmed"
        ):
            return True, "post_submit_problem"
        if any(block.has_error for block in question_blocks):
            return True, "question_error_detected"
        if page_state == "application_or_form_page" and not has_pending_macro_tasks:
            return True, "pre_submit_review"
        return False, "stable_fill_path"

    def _sync_failure_hints(
        self, outcome: SubmissionOutcome, action: AgentAction | None = None
    ) -> None:
        class_map = {
            "validation_error": "validation_error",
            "external_blocked": "external_blocked",
            "transient_network": "transient_network",
            "unknown_blocked": "unknown",
        }
        self.last_outcome_class_hint = outcome.classification
        self.last_outcome_at_hint = datetime.now()
        self.last_error_snippet_hint = outcome.evidence_snippet[:300]
        self.failure_code_hint = outcome.reason_code
        self.failure_class_hint = class_map.get(outcome.classification)
        if outcome.classification == "success_confirmed":
            self.failure_class_hint = None
            self.failure_code_hint = None
            self.retry_count_hint = 0
            self.last_error_snippet_hint = None
            return
        self._record_failure_memory_case(
            classification=outcome.classification,
            reason_code=outcome.reason_code,
            symptom=f"提交结果分类为 {outcome.classification}",
            root_cause=outcome.reason_code or "submission_outcome_unknown",
            evidence_snippet=outcome.evidence_snippet,
            question_text=(action.target_question if action else ""),
            action=(action.action if action else ""),
            selector=(action.selector if action else ""),
            source_event="submission_outcome_classified",
        )

    def _build_submission_manual_reason(self, action: AgentAction) -> str:
        outcome = self._last_submission_outcome
        return oc_build_submission_manual_reason(
            outcome,
            action_name=action.action,
            action_target=action.selector or action.ref or "unknown",
        )

    def _build_semantic_snapshot(
        self,
        current_url: str,
        snapshot_map: dict[str, SnapshotItem],
        visible_text: str,
    ) -> SemanticSnapshot:
        """从现有快照构建结构化语义快照（委托 semantic_perception 模块）。"""
        try:
            title = self.page.title()
        except Exception:
            title = ""
        return build_semantic_snapshot(
            current_url,
            snapshot_map,
            page_title=title,
            visible_text=visible_text,
            last_progression_block_snippets=self._last_progression_block_snippets,
        )

    def _extract_semantic_error_snippets(self, visible_text: str) -> list[str]:
        return extract_semantic_error_snippets(
            visible_text,
            self._last_progression_block_snippets,
        )

    def _should_use_vision_fallback(
        self,
        *,
        page_state: str,
        snapshot_map: dict[str, SnapshotItem],
        visible_text: str,
    ) -> tuple[bool, str]:
        """
        视觉兜底预算控制：
        - 默认语义优先（文本 + 结构化快照）
        - 仅在关键节点使用视觉输入
        """
        if self.visual_fallback_budget <= 0:
            return False, "budget_disabled"
        if self.visual_fallback_used >= self.visual_fallback_budget:
            return False, "budget_exhausted"
        if self.step_count <= 2:
            return True, "early_step_bootstrap"
        if self.consecutive_failures > 0:
            return True, "failure_recovery"
        if self._last_progression_block_reason:
            return True, "progression_blocked"
        if self._last_submission_outcome and (
            self._last_submission_outcome.classification != "success_confirmed"
        ):
            return True, "post_submit_verify"
        if page_state == "manual_gate":
            return True, "manual_gate_detection"
        if len(snapshot_map) < 6:
            return True, "low_semantic_density"
        lower = (visible_text or "").lower()
        if any(
            token in lower
            for token in (
                "verify you are human",
                "security check",
                "flagged as possible spam",
                "suspicious activity",
                "too many requests",
            )
        ):
            return True, "risk_or_challenge_keyword"
        return False, "semantic_only"

    def _should_capture_step_screenshot(self, *, use_vision: bool) -> bool:
        mode = (self.step_screenshot_mode or "vision_only").lower()
        if mode in {"off", "0", "false", "none"}:
            return False
        if mode in {"always", "on", "1", "true"}:
            return True
        # default: vision_only
        return bool(use_vision)

    def _capture_step_screenshot(self) -> str | None:
        """
        按需采集当前步骤截图。
        返回 base64（用于视觉输入）；采集失败时返回 None，但不阻断语义路径。
        """
        try:
            png_bytes = self.page.screenshot(full_page=True)
            original_size = len(png_bytes) / 1024
            compressed_bytes = self._compress_screenshot(png_bytes)
            compressed_size = len(compressed_bytes) / 1024
            screenshot_b64 = base64.b64encode(compressed_bytes).decode("utf-8")

            self._last_screenshot_bytes = compressed_bytes
            screenshot_path = self.screenshot_dir / f"step_{self.step_count:02d}.jpg"
            screenshot_path.write_bytes(compressed_bytes)
            ratio = (
                (1 - compressed_size / original_size) * 100 if original_size > 0 else 0
            )
            self._log(
                f"📸 截图成功: {original_size:.1f} KB → {compressed_size:.1f} KB (压缩 {ratio:.0f}%)"
            )
            self._log(f"   💾 已保存: {screenshot_path.name}")
            return screenshot_b64
        except Exception as e:
            self._log(f"⚠️ 截图采集失败，降级语义路径: {e}", "warn")
            self._step_log(
                "screenshot_capture_error",
                {"step": self.step_count, "error": f"{type(e).__name__}: {e}"},
            )
            return None

    def _build_page_fingerprint(
        self, current_url: str, snapshot_map: dict[str, SnapshotItem]
    ) -> str:
        """为页面构建稳定指纹，用于计划缓存与重复动作抑制。"""
        top_items = []
        sorted_items = sorted(snapshot_map.values(), key=lambda x: x.ref)[:40]
        # region agent log
        append_debug_log(
            location="vision_agent.py:_build_page_fingerprint:entry",
            message="fingerprint entry snapshot item schema",
            data={
                "job_id": self.job_id,
                "step": self.step_count,
                "url": current_url,
                "snapshot_count": len(snapshot_map),
                "first_item_class": (
                    sorted_items[0].__class__.__name__ if sorted_items else None
                ),
                "first_item_attrs": (
                    sorted(
                        [
                            k
                            for k in vars(sorted_items[0]).keys()
                            if not k.startswith("_")
                        ]
                    )[:20]
                    if sorted_items
                    else []
                ),
            },
            run_id="pre-fix-debug",
            hypothesis_id="H8",
        )
        # endregion
        # region agent log
        _btn_checked_samples = []
        for _si in sorted_items:
            if _si.role == "button" and "yes" in (_si.name or "").lower():
                _btn_checked_samples.append(
                    {
                        "ref": _si.ref,
                        "name": (_si.name or "")[:40],
                        "checked": _si.checked,
                        "input_type": _si.input_type,
                    }
                )
        if _btn_checked_samples:
            append_debug_log(
                location="vision_agent.py:_build_page_fingerprint:button_checked",
                message="Yes/No button checked states in fingerprint",
                data={
                    "job_id": self.job_id,
                    "step": self.step_count,
                    "button_samples": _btn_checked_samples,
                },
                run_id="debug-v2",
                hypothesis_id="H1",
            )
        # endregion
        try:
            for item in sorted_items:
                entry: dict = {
                    "r": item.role,
                    "n": (item.name or "")[:60],
                    "t": item.input_type or "",
                    "req": bool(item.required),
                }
                if item.checked is not None:
                    entry["chk"] = item.checked
                if item.value_hint:
                    entry["vh"] = item.value_hint
                top_items.append(entry)
        except Exception as e:
            # region agent log
            append_debug_log(
                location="vision_agent.py:_build_page_fingerprint:error",
                message="fingerprint build failed due to snapshot item schema mismatch",
                data={
                    "job_id": self.job_id,
                    "step": self.step_count,
                    "url": current_url,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "item_repr": repr(item)[:300] if "item" in locals() else None,
                    "item_attrs": (
                        sorted([k for k in vars(item).keys() if not k.startswith("_")])[
                            :20
                        ]
                        if "item" in locals()
                        else []
                    ),
                },
                run_id="pre-fix-debug",
                hypothesis_id="H9",
            )
            # endregion
            raise
        payload = {"url": (current_url or "").split("#")[0], "items": top_items}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        fp_hash = hashlib.sha1(encoded.encode("utf-8")).hexdigest()
        # region agent log
        append_debug_log(
            location="vision_agent.py:_build_page_fingerprint:result",
            message="fingerprint hash computed",
            data={
                "job_id": self.job_id,
                "step": self.step_count,
                "fingerprint": fp_hash[:32],
                "item_count": len(top_items),
                "has_any_chk": any("chk" in it for it in top_items),
                "chk_entries": [
                    {"n": it.get("n", "")[:30], "r": it.get("r"), "chk": it.get("chk")}
                    for it in top_items
                    if "chk" in it
                ][:10],
            },
            run_id="debug-v2",
            hypothesis_id="H2",
        )
        # endregion
        return fp_hash

    def _action_fail_key(self, page_fingerprint: str, action: AgentAction) -> str:
        return "|".join(
            [
                page_fingerprint or "",
                action.action or "",
                action.ref or "",
                action.selector or "",
                str(action.value or ""),
                str(action.target_question or ""),
            ]
        )

    def _task_execution_key(self, page_fingerprint: str, action: AgentAction) -> str:
        semantic_key = self._semantic_action_key(page_fingerprint, action)
        if semantic_key:
            return semantic_key
        selector = (action.selector or "").strip().lower()
        question = (action.target_question or "").strip().lower()
        return "|".join(
            [
                self._stable_page_scope(),
                action.action or "",
                selector,
                question,
            ]
        )

    def _maybe_get_progression_queue_action(
        self, snapshot_map: dict[str, SnapshotItem]
    ) -> AgentAction | None:
        # 若页面没有可提交入口，或仍存在明确阻断，不生成提交动作。
        submit_like: list[tuple[str, SnapshotItem]] = []
        fallback_progression: list[tuple[str, SnapshotItem]] = []
        for ref, item in snapshot_map.items():
            if item.role not in ("button", "link"):
                continue
            label = (item.name or "").strip().lower()
            if any(
                k in label
                for k in ("submit", "finish application", "complete application")
            ):
                submit_like.append((ref, item))
            elif any(k in label for k in ("review", "continue", "apply")):
                fallback_progression.append((ref, item))
        if not submit_like and fallback_progression:
            submit_like = fallback_progression
        if not submit_like:
            return None
        blocked_reason = self._get_progression_block_reason()
        if blocked_reason:
            return None
        chosen_ref, chosen_item = submit_like[0]
        self._step_log(
            "queue_task_selected",
            {
                "step": self.step_count,
                "task_type": "progression_submit",
                "ref": chosen_ref,
                "selector": chosen_item.name,
                "reason_code": "queue_progression_step",
            },
        )
        return AgentAction(
            action="click",
            ref=chosen_ref,
            selector=chosen_item.name,
            element_type=chosen_item.role,
            reason="[queue] macro tasks completed, try progression submit",
        )

    def _normalized_action_intent(self, action: AgentAction) -> str | None:
        if action.action != "click":
            return None
        if self._has_question_binding(action):
            raw_option = (action.selector or "").strip().lower()
            if not raw_option and action.ref:
                item = self._last_snapshot_map.get(action.ref)
                if item:
                    raw_option = (item.name or "").strip().lower()
            label = self._normalize_answer_label(raw_option) or raw_option
            question = (action.target_question or "").strip().lower()
            return f"answer::{question or 'unknown'}::{label or 'unknown_option'}"
        source_item = self._last_snapshot_map.get(action.ref or "")
        if self._is_progression_action(action, item=source_item):
            return "progression::submit_apply"
        return None

    def _stable_page_scope(self) -> str:
        try:
            current = self.page.url or ""
        except Exception:
            current = ""
        return lg_stable_page_scope(current)

    def _semantic_action_key(self, page_fingerprint: str, action: AgentAction) -> str:
        intent = self._normalized_action_intent(action)
        if not intent:
            return ""
        return f"{self._stable_page_scope()}|{intent}"

    def _semantic_loop_guard_decision(
        self, page_fingerprint: str, action: AgentAction
    ) -> str:
        key = self._semantic_action_key(page_fingerprint, action)
        if not key:
            return "none"
        fail_count = self._semantic_fail_counts.get(key, 0)
        decision = lg_semantic_loop_guard_decision(fail_count)
        if decision != "none":
            self._step_log(
                "semantic_loop_guard",
                {
                    "step": self.step_count,
                    "classification": "unknown_blocked",
                    "reason_code": "semantic_repeat",
                    "evidence_snippet": str(action.selector or action.ref or "")[:220],
                    "decision": decision,
                    "semantic_key": key,
                    "stable_scope": self._stable_page_scope(),
                    "fail_count": fail_count,
                    "action": action.action,
                    "selector": action.selector,
                    "ref": action.ref,
                    "target_question": action.target_question,
                },
            )
        return decision

    def _promote_semantic_guard(
        self, page_fingerprint: str, action: AgentAction, *, stage: str
    ) -> None:
        """
        当 guard 仅触发重规划但未真正执行动作时，仍推进失败计数，避免一直停在 replan。
        """
        key = self._semantic_action_key(page_fingerprint, action)
        if not key:
            return
        next_count = lg_promote_semantic_fail_count(self._semantic_fail_counts, key)
        self._step_log(
            "semantic_loop_guard",
            {
                "step": self.step_count,
                "classification": "unknown_blocked",
                "reason_code": "semantic_repeat_promoted",
                "evidence_snippet": str(action.selector or action.ref or "")[:220],
                "decision": f"{stage}_promote",
                "semantic_key": key,
                "stable_scope": self._stable_page_scope(),
                "fail_count": next_count,
                "action": action.action,
                "selector": action.selector,
                "ref": action.ref,
                "target_question": action.target_question,
            },
        )

    def _build_semantic_loop_manual_reason(self, action: AgentAction) -> str:
        snippets = " | ".join(self._last_progression_block_snippets[:2])
        blocker = self._last_progression_block_reason or "无明确门控错误摘要"
        suffix = f"；最近门控: {blocker}"
        if snippets:
            suffix += f"；错误片段: {snippets}"
        if self._last_submission_outcome:
            suffix += (
                f"；最近分类: {self._last_submission_outcome.classification}"
                f"/{self._last_submission_outcome.reason_code}"
            )
        return (
            "同一语义动作重复失败达到上限（已触发重规划与替代动作）"
            f"；动作={action.action}:{action.selector or action.ref or 'unknown'}{suffix}"
        )

    def _should_skip_repeated_action(
        self, page_fingerprint: str, action: AgentAction
    ) -> bool:
        key = self._action_fail_key(page_fingerprint, action)
        return self._action_fail_counts.get(key, 0) >= 2

    def _record_action_result(
        self, page_fingerprint: str, action: AgentAction, success: bool
    ) -> None:
        key = self._action_fail_key(page_fingerprint, action)
        semantic_key = self._semantic_action_key(page_fingerprint, action)
        lg_record_loop_action_result(
            action_fail_counts=self._action_fail_counts,
            repeated_skip_counts=self._repeated_skip_counts,
            semantic_fail_counts=self._semantic_fail_counts,
            action_key=key,
            semantic_key=semantic_key,
            success=success,
        )

    def _sanitize_simplify_claims(self, text: str | None) -> str | None:
        return planner_sanitize_simplify_claims(text)

    def _build_alternate_action(self, action: AgentAction) -> AgentAction | None:
        """为重复失败动作构建同页替代动作，优先尝试其他 submit/apply 按钮。"""
        if action.action != "click":
            return None
        source_item = self._last_snapshot_map.get(action.ref or "")
        if not self._is_progression_action(action, item=source_item):
            return None
        for ref, item in self._last_snapshot_map.items():
            if ref == action.ref:
                continue
            if item.role not in ("button", "link"):
                continue
            label = (item.name or "").lower()
            if "submit" not in label and "apply" not in label:
                continue
            return AgentAction(
                action="click",
                ref=ref,
                selector=item.name,
                element_type=item.role,
                reason="替代提交入口，避免重复点击同一按钮",
            )
        return None

    def _is_macro_action(self, action: AgentAction) -> bool:
        return bool((action.reason or "").startswith("[macro:"))

    def _macro_task_id_from_action(self, action: AgentAction) -> str:
        reason = action.reason or ""
        if not reason.startswith("[macro:"):
            return ""
        return reason.split("]", 1)[0].replace("[macro:", "").strip()

    def _macro_task_retry_count(self, action: AgentAction) -> int:
        task_id = self._macro_task_id_from_action(action)
        if not task_id:
            return 0
        for task in self._macro_tasks:
            if task.task_id == task_id:
                return task.retry_count
        return 0

    def _find_question_block(self, task: MacroTask) -> QuestionBlock | None:
        if not task.question_text:
            return None
        return self._find_matching_question_block(
            task.question_text, self._last_question_blocks
        )

    def _selected_expected_options(
        self, task: MacroTask, selected_values: list[str]
    ) -> list[str]:
        matched: list[str] = []
        for expected in task.expected_options:
            if any(
                self._option_text_matches(expected, picked)
                for picked in selected_values
            ):
                matched.append(expected)
        return matched

    def _compute_question_multi_remaining(
        self, task: MacroTask, block: QuestionBlock | None
    ) -> tuple[list[str], list[str]]:
        selected_values: list[str] = []
        if block:
            selected_values.extend(
                [str(x) for x in block.selected_options if str(x).strip()]
            )
        selected_values.extend(
            self._collect_selected_options_for_question(task.question_text or "")
        )
        selected_values.extend(
            [str(x) for x in task.completed_options if str(x).strip()]
        )
        dedup_selected = list(dict.fromkeys(selected_values))
        matched = self._selected_expected_options(task, dedup_selected)
        if matched:
            task.completed_options = list(
                dict.fromkeys(task.completed_options + matched)
            )
        remaining = [
            expected
            for expected in task.expected_options
            if expected not in task.completed_options
        ]
        return remaining, dedup_selected

    def _find_block_option_for_expected(self, block: QuestionBlock, expected: str):
        for opt in block.options:
            if self._option_text_matches(opt.text, expected):
                return opt
        return None

    def _macro_upload_lock_key(self, task: MacroTask) -> str:
        identity = self._macro_task_identity_key(task)
        if not identity:
            identity = f"file_upload|{(task.field_selector or '').strip().lower()}"
        return f"{self._stable_page_scope()}|{identity}"

    def _question_error_mentions(self, question_text: str | None) -> bool:
        question = (question_text or "").strip().lower()
        if not question:
            return False
        for snippet in self._last_progression_block_snippets:
            text = str(snippet or "").strip().lower()
            if not text:
                continue
            if question in text:
                return True
            if len(question) > 24 and question[:24] in text:
                return True
        return False

    def _macro_upload_completed(self, task: MacroTask) -> bool:
        """粗粒度上传完成判定：出现 Replace 或附件文件名信号即视为完成。"""
        if self._macro_upload_lock_key(task) in self._upload_task_locks:
            return True
        wanted = " ".join((task.field_selector or "resume").lower().split())
        for item in self._last_snapshot_map.values():
            label = " ".join((item.name or "").lower().split())
            if item.role == "button" and "replace" in label:
                if "resume" in wanted or "cv" in wanted or "cover letter" in wanted:
                    return True
            if item.role in ("button", "textbox", "file_input") and any(
                ext in label for ext in (".pdf", ".doc", ".docx")
            ):
                return True
        return False

    def _macro_task_completed(
        self, task: MacroTask, snapshot_map: dict[str, SnapshotItem]
    ) -> bool:
        if task.status in ("done", "blocked"):
            return task.status == "done"
        if task.task_type in ("field_fill", "field_fill_optional"):
            target = None
            if task.field_ref and task.field_ref in snapshot_map:
                target = snapshot_map.get(task.field_ref)
            if target is None and task.field_selector:
                wanted = " ".join((task.field_selector or "").lower().split())
                for item in snapshot_map.values():
                    if item.role != "textbox":
                        continue
                    current = " ".join((item.name or "").lower().split())
                    if current == wanted or (wanted and wanted in current):
                        target = item
                        break
            if not target:
                return False
            return bool((target.value_hint or "").strip())
        if task.task_type == "combobox_select":
            options_open = any(item.role == "option" for item in snapshot_map.values())
            if options_open:
                return False
            # combobox 任务一旦进入执行态且下拉已关闭，视为本轮完成
            return task.status == "in_progress"
        if task.task_type == "file_upload":
            return self._macro_upload_completed(task)
        if task.task_type == "manual_required":
            return False
        block = self._find_question_block(task)
        if not block and not task.question_text:
            return False
        if self._question_error_mentions(task.question_text):
            return False
        selected_values: list[str] = []
        if block:
            selected_values.extend(
                [str(x) for x in block.selected_options if str(x).strip()]
            )
        selected_values.extend(
            self._collect_selected_options_for_question(task.question_text or "")
        )
        selected_values.extend(
            [str(x) for x in task.completed_options if str(x).strip()]
        )
        selected_values = list(dict.fromkeys(selected_values))
        matched = self._selected_expected_options(task, selected_values)
        if matched:
            task.completed_options = list(
                dict.fromkeys(task.completed_options + matched)
            )
        if task.task_type == "question_single":
            verified = all(
                self._verify_question_option_state(task.question_text or "", expected)
                for expected in task.expected_options
                if (expected or "").strip()
            )
            return bool(task.expected_options) and bool(verified)
        if task.task_type == "inference_required":
            if task.expected_options:
                return len(task.completed_options) >= len(task.expected_options)
            return bool(selected_values)
        return bool(task.expected_options) and len(task.completed_options) >= len(
            task.expected_options
        )

    def _macro_task_precondition_met(
        self, task: MacroTask, snapshot_map: dict[str, SnapshotItem]
    ) -> bool:
        if task.task_type in ("field_fill", "field_fill_optional"):
            if task.field_ref and task.field_ref in snapshot_map:
                item = snapshot_map.get(task.field_ref)
                if item and item.role == "textbox":
                    return not bool((item.value_hint or "").strip())
            return any(
                item.role == "textbox"
                and " ".join((item.name or "").lower().split())
                == " ".join((task.field_selector or "").lower().split())
                and not bool((item.value_hint or "").strip())
                for item in snapshot_map.values()
            )
        if task.task_type == "combobox_select":
            if task.field_ref and task.field_ref in snapshot_map:
                return True
            return any(item.role == "combobox" for item in snapshot_map.values())
        if task.task_type == "file_upload":
            if self._macro_upload_completed(task):
                return False
            if task.field_ref and task.field_ref in snapshot_map:
                return True
            return any(
                (item.role == "file_input")
                or ((item.input_type or "").lower() == "file")
                for item in snapshot_map.values()
            )
        if task.task_type == "manual_required":
            return True
        block = self._find_question_block(task)
        if task.task_type == "inference_required":
            return block is not None and bool(task.question_text)
        return block is not None and bool(task.expected_options)

    def _build_macro_action_for_task(
        self, task: MacroTask, snapshot_map: dict[str, SnapshotItem]
    ) -> AgentAction | None:
        reason_prefix = f"[macro:{task.task_id}] "
        if task.task_type in ("field_fill", "field_fill_optional"):
            value = (task.target_value or "").strip()
            if not value:
                return None
            field_ref = task.field_ref
            selector = task.field_selector or "text field"
            if not field_ref:
                wanted = " ".join(selector.lower().split())
                for item in snapshot_map.values():
                    current = " ".join((item.name or "").lower().split())
                    if item.role == "textbox" and (
                        current == wanted or (wanted and wanted in current)
                    ):
                        field_ref = item.ref
                        break
            if not field_ref:
                return None
            return AgentAction(
                action="fill",
                ref=field_ref,
                selector=selector,
                value=value,
                element_type="textbox",
                reason=reason_prefix + "fill field from profile mapping",
            )
        if task.task_type == "combobox_select":
            options = [it for it in snapshot_map.values() if it.role == "option"]
            target = (task.target_value or "").strip()
            combo_ref = task.field_ref
            if not combo_ref or combo_ref not in snapshot_map:
                selector_norm = " ".join((task.field_selector or "").lower().split())
                for item in snapshot_map.values():
                    if item.role != "combobox":
                        continue
                    name_norm = " ".join((item.name or "").lower().split())
                    if (
                        not selector_norm
                        or name_norm == selector_norm
                        or selector_norm in name_norm
                    ):
                        combo_ref = item.ref
                        break
            task.field_ref = combo_ref
            if options:
                target_norm = target.lower()
                chosen = None
                for opt in options:
                    name = (opt.name or "").lower()
                    if target_norm and (
                        target_norm in name
                        or name in target_norm
                        or target_norm.split(",")[0].strip() in name
                    ):
                        chosen = opt
                        break
                if chosen is None:
                    chosen = options[0]
                return AgentAction(
                    action="click",
                    ref=chosen.ref,
                    selector=chosen.name,
                    element_type=chosen.role,
                    reason=reason_prefix
                    + "combobox options visible, select best matching option",
                )
            selector = task.field_selector or "Location"
            return AgentAction(
                action="type",
                ref=combo_ref,
                selector=selector,
                value=target,
                element_type="combobox",
                reason=reason_prefix + "type target value into combobox",
            )
        if task.task_type == "file_upload":
            selector = (task.field_selector or "Resume").strip()
            return AgentAction(
                action="upload",
                ref=task.field_ref,
                selector=selector,
                value=task.target_value,
                element_type="file_input",
                reason=reason_prefix + "upload required file",
            )
        if task.task_type == "manual_required":
            return None
        if task.task_type == "inference_required":
            block = self._find_question_block(task)
            if not block:
                return None
            inferred = self._infer_required_question_answers(task, block)
            if not inferred:
                return None
            task.expected_options = inferred
            for expected in inferred:
                if expected in task.completed_options:
                    continue
                if self._verify_question_option_state(block.question_text, expected):
                    task.completed_options = list(
                        dict.fromkeys(task.completed_options + [expected])
                    )
                    continue
                opt = self._find_block_option_for_expected(block, expected)
                if not opt:
                    continue
                task.last_attempt_option = opt.text
                return AgentAction(
                    action="click",
                    ref=opt.ref_id,
                    selector=opt.text,
                    target_question=block.question_text,
                    element_type=opt.role,
                    reason=reason_prefix
                    + "inference_required mapped required question to option",
                )
            return None

        block = self._find_question_block(task)
        if not block or not task.expected_options:
            return None
        remaining = list(task.expected_options)
        if task.task_type == "question_multi":
            remaining, selected_values = self._compute_question_multi_remaining(
                task, block
            )
            self._step_log(
                "question_multi_progress",
                {
                    "step": self.step_count,
                    "task_id": task.task_id,
                    "question_text": block.question_text[:180],
                    "selected_count": len(task.completed_options),
                    "expected_count": len(task.expected_options),
                    "selected_options": selected_values[:8],
                    "remaining_options": remaining[:8],
                },
            )
        else:
            selected_values = self._collect_selected_options_for_question(
                block.question_text
            )
            matched = self._selected_expected_options(task, selected_values)
            if matched:
                task.completed_options = list(
                    dict.fromkeys(task.completed_options + matched)
                )
            remaining = [
                expected
                for expected in task.expected_options
                if expected not in task.completed_options
            ]
        if not remaining:
            return None
        if task.task_type == "question_multi" and task.last_attempt_option:
            head = [
                x
                for x in remaining
                if not self._option_text_matches(x, task.last_attempt_option)
            ]
            tail = [
                x
                for x in remaining
                if self._option_text_matches(x, task.last_attempt_option)
            ]
            if head:
                remaining = head + tail
        for expected in remaining:
            if self._verify_question_option_state(block.question_text, expected):
                task.completed_options = list(
                    dict.fromkeys(task.completed_options + [expected])
                )
                continue
            target_opt = self._find_block_option_for_expected(block, expected)
            if target_opt is None:
                continue
            task.last_attempt_option = target_opt.text
            return AgentAction(
                action="click",
                ref=target_opt.ref_id,
                selector=target_opt.text,
                target_question=block.question_text,
                element_type=target_opt.role,
                reason=reason_prefix + "execute planned question option selection",
            )
        return None

    def _infer_required_question_answers(
        self,
        task: MacroTask,
        block: QuestionBlock,
    ) -> list[str]:
        """必答未映射问题：先规则推断，失败后 LLM 结构化推断。"""
        options = [opt.text for opt in block.options if (opt.text or "").strip()]
        if not options:
            return []
        question = (block.question_text or task.question_text or "").strip()
        lower_q = " ".join(question.lower().split())

        def _match_option(candidate: str) -> str:
            target = " ".join((candidate or "").lower().split())
            if not target:
                return ""
            for option in options:
                opt_norm = " ".join((option or "").lower().split())
                if opt_norm == target:
                    return option
            for option in options:
                opt_norm = " ".join((option or "").lower().split())
                if target in opt_norm or opt_norm in target:
                    return option
            return ""

        work_auth = self._user_profile.get("work_authorization", {})
        if isinstance(work_auth, dict):
            if "visa sponsorship" in lower_q:
                val = work_auth.get("require_visa_sponsorship")
                if isinstance(val, bool):
                    choice = "Yes" if val else "No"
                    hit = _match_option(choice)
                    if hit:
                        return [hit]
            if (
                "authorized to work" in lower_q
                or "legally authorized" in lower_q
                or "employment authorized" in lower_q
            ):
                val = work_auth.get("authorized_to_work_in_us")
                if isinstance(val, bool):
                    choice = "Yes" if val else "No"
                    hit = _match_option(choice)
                    if hit:
                        return [hit]

        if not self.client:
            return []

        profile_text = json.dumps(self._user_profile, ensure_ascii=False)[:3000]
        options_text = "\n".join(f"- {opt}" for opt in options[:12])
        prompt = (
            "Choose the best answer option(s) for a required job-application question.\n"
            'Return strict JSON only: {"answers":["..."],"confidence":0.0,"reason":"..."}\n'
            "Question:\n"
            f"{question}\n"
            "Options:\n"
            f"{options_text}\n"
            "Candidate profile summary JSON:\n"
            f"{profile_text}\n"
            "Rules: never return options not in the list."
        )
        llm_result = run_chat_with_fallback(
            client=self.client,
            fallback_models=self.fallback_models,
            start_model_index=self.model_index,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            temperature=0.1,
            top_p=0.8,
            max_tokens=220,
            on_log=lambda lvl, msg: self._log(msg, lvl),
        )
        if not llm_result.ok:
            return []
        self.model_index = llm_result.model_index
        self.model = llm_result.model
        parsed = planner_safe_parse_json(llm_result.raw)
        if not isinstance(parsed, dict):
            return []
        raw_answers = parsed.get("answers") or []
        if not isinstance(raw_answers, list):
            return []
        resolved: list[str] = []
        for ans in raw_answers:
            hit = _match_option(str(ans))
            if hit and hit not in resolved:
                resolved.append(hit)
        return resolved[:4]

    def _normalize_audit_text(self, text: str | None) -> str:
        value = (text or "").strip().lower()
        value = re.sub(r"[\s\u00a0]+", " ", value)
        value = re.sub(r"[^\w\s]", "", value)
        return value.strip()

    def _normalize_audit_semantic(self, text: str | None) -> str:
        stop_words = {
            "why",
            "are",
            "you",
            "interested",
            "in",
            "working",
            "at",
            "for",
            "this",
            "role",
            "position",
            "company",
            "job",
            "tell",
            "us",
            "about",
            "your",
            "the",
        }
        normalized = self._normalize_audit_text(text)
        if not normalized:
            return ""
        kept = [tok for tok in normalized.split() if tok not in stop_words]
        return " ".join(kept).strip()

    def _question_similarity_score(self, left: str | None, right: str | None) -> float:
        left_exact = self._normalize_audit_text(left)
        right_exact = self._normalize_audit_text(right)
        if not left_exact or not right_exact:
            return 0.0
        if left_exact == right_exact:
            return 1.0
        if left_exact in right_exact or right_exact in left_exact:
            return 0.92
        left_sem = self._normalize_audit_semantic(left)
        right_sem = self._normalize_audit_semantic(right)
        if left_sem and right_sem:
            if left_sem == right_sem:
                return 0.9
            left_set = set(left_sem.split())
            right_set = set(right_sem.split())
            if left_set and right_set:
                inter = len(left_set & right_set)
                union = len(left_set | right_set)
                if union > 0:
                    return inter / union
        return 0.0

    def _question_covered_by_tasks(
        self, tasks: list[MacroTask], question_text: str
    ) -> bool:
        for task in tasks:
            for raw in (task.question_text, task.field_selector):
                if self._question_similarity_score(raw, question_text) >= 0.72:
                    return True
        return False

    def _find_matching_question_block(
        self, question_text: str, question_blocks: list[QuestionBlock]
    ) -> QuestionBlock | None:
        best: QuestionBlock | None = None
        best_score = 0.0
        for block in question_blocks:
            score = self._question_similarity_score(question_text, block.question_text)
            if score > best_score:
                best_score = score
                best = block
        if best and best_score >= 0.72:
            return best
        return None

    def _dedupe_visual_augmentation(
        self,
        existing_tasks: list[MacroTask],
        audit_questions: list[str],
    ) -> tuple[list[str], int, list[str]]:
        existing_texts: list[str] = []
        for task in existing_tasks:
            for raw in (task.question_text, task.field_selector):
                text = str(raw or "").strip()
                if text:
                    existing_texts.append(text)

        kept: list[str] = []
        dropped = 0
        reason_codes: list[str] = []
        for question in audit_questions:
            candidate = str(question or "").strip()
            if not candidate:
                dropped += 1
                reason_codes.append("empty_question")
                continue
            if any(
                self._question_similarity_score(candidate, existing) >= 0.72
                for existing in existing_texts
            ):
                dropped += 1
                reason_codes.append("duplicate_existing_task")
                continue
            kept.append(candidate)
            existing_texts.append(candidate)
        return kept, dropped, sorted(set(reason_codes))

    def _is_location_like_question(self, question_text: str | None) -> bool:
        lowered = self._normalize_audit_text(question_text)
        if not lowered:
            return False
        return (
            lowered in ("location", "start typing")
            or "where are you located" in lowered
            or "current location" in lowered
            or "location preference" in lowered
        )

    def _enforce_macro_plan_completeness(
        self,
        *,
        tasks: list[MacroTask],
        question_blocks: list[QuestionBlock],
        audit: VisualAuditResult,
    ) -> tuple[list[MacroTask], dict]:
        if not question_blocks and not audit.required_questions:
            return tasks, {
                "missing_required_questions": [],
                "added_required_questions": 0,
                "added_unmapped_questions": 0,
                "dropped_questions": 0,
                "coverage_ok": True,
            }

        has_location_task = any(t.task_type == "combobox_select" for t in tasks)
        next_idx = len(tasks) + 1
        added_required = 0
        added_unmapped = 0
        dropped = 0
        missing_required: list[str] = []

        def _append_inference_task(
            question_text: str, options: list[str], *, required: bool, reason: str
        ) -> None:
            nonlocal next_idx, added_required, added_unmapped
            tasks.append(
                MacroTask(
                    task_id=f"t{next_idx}",
                    task_type="inference_required",
                    title="Infer answer for unmapped question",
                    question_text=question_text,
                    expected_options=options[:8],
                    mapping_reason=reason,
                    precondition="question_block_present",
                    postcondition=(
                        "required_question_answered"
                        if required
                        else "question_answered"
                    ),
                    required=required,
                )
            )
            next_idx += 1
            if required:
                added_required += 1
            else:
                added_unmapped += 1

        # 1) 以语义块为主，保证可交互问题尽量都有执行任务。
        for qb in question_blocks:
            if len(qb.options) < 2:
                continue
            if has_location_task and self._is_location_like_question(qb.question_text):
                continue
            if self._question_covered_by_tasks(tasks, qb.question_text):
                continue
            options = [opt.text for opt in qb.options if opt.text]
            if len(options) < 2:
                dropped += 1
                continue
            required = bool(qb.required or qb.has_error)
            reason = (
                "plan_completeness_missing_required_question"
                if required
                else "plan_completeness_unmapped_question"
            )
            _append_inference_task(
                qb.question_text,
                options,
                required=required,
                reason=reason,
            )

        # 2) 对视觉审计标记为必答但语义块里未能匹配的问题，只记录缺口，不再盲目创建幻影任务。
        for audit_question in audit.required_questions:
            if self._question_covered_by_tasks(tasks, audit_question):
                continue
            block = self._find_matching_question_block(audit_question, question_blocks)
            if block and len(block.options) >= 2:
                _append_inference_task(
                    block.question_text,
                    [opt.text for opt in block.options if opt.text],
                    required=True,
                    reason="plan_completeness_missing_required_question",
                )
            else:
                missing_required.append(str(audit_question).strip())

        coverage_ok = len(missing_required) == 0
        return tasks, {
            "missing_required_questions": missing_required[:8],
            "added_required_questions": added_required,
            "added_unmapped_questions": added_unmapped,
            "dropped_questions": dropped,
            "coverage_ok": coverage_ok,
        }

    def _augment_macro_tasks_with_visual_audit(
        self,
        *,
        tasks: list[MacroTask],
        audit: VisualAuditResult,
        snapshot_map: dict[str, SnapshotItem],
        question_blocks: list[QuestionBlock],
    ) -> list[MacroTask]:
        normalized_questions = {
            self._normalize_audit_text(t.question_text or "")
            for t in tasks
            if (t.question_text or "").strip()
        }
        next_idx = len(tasks) + 1
        added = 0
        dedup_questions, dedup_dropped_count, reason_codes = (
            self._dedupe_visual_augmentation(tasks, audit.required_questions)
        )
        for question in dedup_questions:
            q_norm = self._normalize_audit_text(question)
            if not q_norm or q_norm in normalized_questions:
                continue
            matched_block = self._find_matching_question_block(
                question, question_blocks
            )
            if not matched_block:
                dedup_dropped_count += 1
                reason_codes.append("audit_question_not_in_semantic_blocks")
                continue
            if self._question_covered_by_tasks(tasks, matched_block.question_text):
                dedup_dropped_count += 1
                reason_codes.append("duplicate_existing_task")
                continue
            inferred_options = [opt.text for opt in matched_block.options if opt.text][
                :8
            ]
            if len(inferred_options) < 2:
                dedup_dropped_count += 1
                reason_codes.append("audit_question_missing_options")
                continue
            tasks.append(
                MacroTask(
                    task_id=f"t{next_idx}",
                    task_type="inference_required",
                    title="Infer answer for required question (visual audit)",
                    question_text=matched_block.question_text,
                    expected_options=inferred_options,
                    mapping_reason="visual_required_question_missing",
                    precondition="question_block_present",
                    postcondition="required_question_answered",
                    required=True,
                )
            )
            normalized_questions.add(
                self._normalize_audit_text(matched_block.question_text)
            )
            next_idx += 1
            added += 1

        has_upload_task = any(t.task_type == "file_upload" for t in tasks)
        resume_upload_required = any(
            ("resume" in (u or "").lower()) or ("cv" in (u or "").lower())
            for u in audit.required_uploads
        )
        if resume_upload_required and not has_upload_task:
            fallback_ref = None
            fallback_label = "Resume"
            for item in snapshot_map.values():
                if (
                    item.role == "file_input"
                    or (item.input_type or "").lower() == "file"
                ):
                    fallback_ref = item.ref
                    fallback_label = item.name or "Resume"
                    break
            if fallback_ref or any(
                item.role == "button" and "upload" in (item.name or "").lower()
                for item in snapshot_map.values()
            ):
                tasks.append(
                    MacroTask(
                        task_id=f"t{next_idx}",
                        task_type="file_upload",
                        title="Upload required resume (visual audit补全)",
                        field_ref=fallback_ref,
                        field_selector=fallback_label,
                        target_value=self.preferred_resume_path,
                        mapping_reason="visual_required_resume_upload_missing",
                        precondition="resume_upload_needed",
                        postcondition="file_uploaded",
                    )
                )
                next_idx += 1
                added += 1

        if added > 0:
            self._step_log(
                "execution_queue_augmented_by_visual_audit",
                {
                    "step": self.step_count,
                    "scope": self._stable_page_scope(),
                    "added_task_count": added,
                    "dedup_dropped_count": dedup_dropped_count,
                    "reason_codes": reason_codes[:6],
                    "total_task_count": len(tasks),
                },
            )
        elif dedup_dropped_count > 0:
            self._step_log(
                "execution_queue_augmented_by_visual_audit",
                {
                    "step": self.step_count,
                    "scope": self._stable_page_scope(),
                    "added_task_count": 0,
                    "dedup_dropped_count": dedup_dropped_count,
                    "reason_codes": reason_codes[:6],
                    "total_task_count": len(tasks),
                },
            )
        return tasks

    def _maybe_get_macro_action(
        self,
        *,
        snapshot_map: dict[str, SnapshotItem],
        page_fingerprint: str,
    ) -> AgentAction | None:
        scope = self._stable_page_scope()
        self._macro_manual_block_reason = None
        if scope in self._macro_disabled_scopes:
            return None
        if scope != self._macro_scope:
            self._macro_scope = scope
            self._macro_tasks = []
            self._active_macro_task_id = None

        if not self._macro_tasks:
            audit = self._run_scope_visual_audit(
                scope=scope,
                snapshot_map=snapshot_map,
                question_blocks=self._last_question_blocks,
                force=self._force_visual_audit_next_plan,
            )
            self._force_visual_audit_next_plan = False
            self._macro_tasks = build_macro_tasks(
                profile=self._user_profile,
                snapshot_map=snapshot_map,
                question_blocks=self._last_question_blocks,
                preferred_resume_path=self.preferred_resume_path,
            )
            self._macro_tasks = self._augment_macro_tasks_with_visual_audit(
                tasks=self._macro_tasks,
                audit=audit,
                snapshot_map=snapshot_map,
                question_blocks=self._last_question_blocks,
            )
            self._macro_tasks, coverage = self._enforce_macro_plan_completeness(
                tasks=self._macro_tasks,
                question_blocks=self._last_question_blocks,
                audit=audit,
            )
            self._step_log(
                "plan_completeness_checked",
                {
                    "step": self.step_count,
                    "scope": scope,
                    "coverage_ok": coverage["coverage_ok"],
                    "added_required_questions": coverage["added_required_questions"],
                    "added_unmapped_questions": coverage["added_unmapped_questions"],
                    "dropped_questions": coverage["dropped_questions"],
                    "missing_required_questions": coverage[
                        "missing_required_questions"
                    ],
                },
            )
            if not coverage["coverage_ok"]:
                self._force_visual_audit_next_plan = True
            self._last_queue_plan = [
                QueueTaskView(
                    task_id=t.task_id,
                    task_type=t.task_type,
                    label=t.question_text or t.field_selector or t.title,
                    status=t.status,
                    action=(
                        "type"
                        if t.task_type == "combobox_select"
                        else "upload"
                        if t.task_type == "file_upload"
                        else "fill"
                        if t.task_type in ("field_fill", "field_fill_optional")
                        else "click"
                    ),
                    ref=t.field_ref,
                    target_question=t.question_text,
                )
                for t in self._macro_tasks
            ]
            if self._macro_tasks:
                self._step_log(
                    "macro_plan_built",
                    {
                        "step": self.step_count,
                        "scope": scope,
                        "page_fingerprint": page_fingerprint[:32],
                        "tasks": summarize_macro_tasks(self._macro_tasks),
                    },
                )
                self._step_log(
                    "plan_created",
                    {
                        "step": self.step_count,
                        "scope": scope,
                        "plan_type": "macro_task_chain",
                        "task_count": len(self._macro_tasks),
                    },
                )
                self._step_log(
                    "execution_queue_built",
                    {
                        "step": self.step_count,
                        "scope": scope,
                        "task_count": len(self._last_queue_plan),
                        "tasks": [
                            {
                                "task_id": item.task_id,
                                "task_type": item.task_type,
                                "action": item.action,
                                "label": item.label[:120],
                                "ref": item.ref,
                                "target_question": item.target_question,
                            }
                            for item in self._last_queue_plan[:12]
                        ],
                    },
                )
        else:
            refreshed_tasks = build_macro_tasks(
                profile=self._user_profile,
                snapshot_map=snapshot_map,
                question_blocks=self._last_question_blocks,
                preferred_resume_path=self.preferred_resume_path,
            )
            existing_keys = {
                self._macro_task_identity_key(t)
                for t in self._macro_tasks
                if self._macro_task_identity_key(t)
            }
            appended = 0
            for new_task in refreshed_tasks:
                key = self._macro_task_identity_key(new_task)
                if not key or key in existing_keys:
                    continue
                if any(
                    t.status == "done" and self._macro_task_identity_key(t) == key
                    for t in self._macro_tasks
                ):
                    continue
                new_task.task_id = f"t{len(self._macro_tasks) + 1}"
                self._macro_tasks.append(new_task)
                existing_keys.add(key)
                appended += 1
            if appended > 0:
                self._step_log(
                    "execution_queue_augmented",
                    {
                        "step": self.step_count,
                        "scope": scope,
                        "added_task_count": appended,
                        "total_task_count": len(self._macro_tasks),
                        "tasks": summarize_macro_tasks(self._macro_tasks),
                    },
                )
            cached_audit = self._scope_visual_audits.get(scope) or VisualAuditResult(
                visual_summary="",
                required_fields=[],
                required_questions=[],
                required_uploads=[],
                source="heuristic",
            )
            self._macro_tasks, coverage = self._enforce_macro_plan_completeness(
                tasks=self._macro_tasks,
                question_blocks=self._last_question_blocks,
                audit=cached_audit,
            )
            self._step_log(
                "plan_completeness_checked",
                {
                    "step": self.step_count,
                    "scope": scope,
                    "coverage_ok": coverage["coverage_ok"],
                    "added_required_questions": coverage["added_required_questions"],
                    "added_unmapped_questions": coverage["added_unmapped_questions"],
                    "dropped_questions": coverage["dropped_questions"],
                    "missing_required_questions": coverage[
                        "missing_required_questions"
                    ],
                },
            )

        for task in self._macro_tasks:
            if self._macro_task_completed(task, snapshot_map):
                if task.status != "done":
                    self._step_log(
                        "macro_task_completion_decision",
                        {
                            "step": self.step_count,
                            "task_id": task.task_id,
                            "task_type": task.task_type,
                            "decision": "done",
                            "question_text": task.question_text,
                            "field_selector": task.field_selector,
                            "completed_options": task.completed_options[:6],
                        },
                    )
                task.status = "done"
                task.wait_count = 0
                continue
            if task.status == "blocked":
                continue
            if task.task_type == "manual_required":
                self._macro_manual_block_reason = (
                    task.mapping_reason
                    or task.field_selector
                    or task.title
                    or "required_attachment_not_supported"
                )
                self._step_log(
                    "macro_manual_required_detected",
                    {
                        "step": self.step_count,
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "reason": self._macro_manual_block_reason,
                        "field_selector": task.field_selector,
                    },
                )
                return None
            if not self._macro_task_precondition_met(task, snapshot_map):
                task.wait_count += 1
                self._step_log(
                    "macro_task_waiting_precondition",
                    {
                        "step": self.step_count,
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "precondition": task.precondition,
                        "wait_count": task.wait_count,
                        "wait_limit": self._macro_precondition_wait_limit,
                    },
                )
                if task.wait_count >= self._macro_precondition_wait_limit:
                    task.status = "blocked"
                    self._step_log(
                        "macro_task_blocked",
                        {
                            "step": self.step_count,
                            "task_id": task.task_id,
                            "task_type": task.task_type,
                            "reason": "precondition_timeout",
                            "precondition": task.precondition,
                            "wait_count": task.wait_count,
                            "wait_limit": self._macro_precondition_wait_limit,
                        },
                    )
                    self._record_failure_memory_case(
                        classification="unknown_blocked",
                        reason_code="precondition_timeout",
                        symptom="宏任务前置条件长期不满足，任务被阻断",
                        root_cause=task.precondition or "question_block_missing",
                        question_text=task.question_text or "",
                        action=task.task_type,
                        selector=task.field_selector or task.question_text or "",
                        source_event="macro_task_blocked",
                    )
                continue
            action = self._build_macro_action_for_task(task, snapshot_map)
            if action is None:
                if self._macro_task_completed(task, snapshot_map):
                    task.status = "done"
                else:
                    task.status = "blocked"
                continue
            task.status = "in_progress"
            task.wait_count = 0
            self._active_macro_task_id = task.task_id
            self._step_log(
                "macro_task_selected",
                {
                    "step": self.step_count,
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "title": task.title,
                    "mapping_reason": task.mapping_reason,
                    "precondition": task.precondition,
                    "postcondition": task.postcondition,
                    "question_text": task.question_text,
                    "expected_options": task.expected_options[:4],
                    "target_value": task.target_value,
                    "retry_count": task.retry_count,
                },
            )
            self._step_log(
                "task_selected",
                {
                    "step": self.step_count,
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "mapping_reason": task.mapping_reason,
                },
            )
            return action
        return None

    def _macro_task_identity_key(self, task: MacroTask) -> str:
        if task.task_type == "combobox_select":
            return f"combobox|{(task.field_selector or '').strip().lower()}|{(task.target_value or '').strip().lower()}"
        if task.task_type in ("field_fill", "field_fill_optional"):
            return f"field_fill|{(task.field_selector or '').strip().lower()}"
        if task.task_type == "file_upload":
            return f"file_upload|{(task.field_selector or '').strip().lower()}"
        if task.task_type == "manual_required":
            return f"manual_required|{(task.field_selector or task.mapping_reason or task.title or '').strip().lower()}"
        if task.task_type == "inference_required":
            q_key = self._normalize_audit_semantic(
                task.question_text
            ) or self._normalize_audit_text(task.question_text)
            return f"question_infer|{q_key}"
        if task.task_type in ("question_single", "question_multi"):
            opts = "|".join(
                sorted(x.strip().lower() for x in task.expected_options if x.strip())
            )
            q_key = self._normalize_audit_semantic(
                task.question_text
            ) or self._normalize_audit_text(task.question_text)
            return f"question|{q_key}|{opts}"
        return ""

    def _on_macro_action_result(self, action: AgentAction, success: bool) -> None:
        reason = action.reason or ""
        if not reason.startswith("[macro:"):
            return
        task_id = reason.split("]", 1)[0].replace("[macro:", "").strip()
        if not task_id:
            return
        for task in self._macro_tasks:
            if task.task_id != task_id:
                continue
            if success:
                if task.task_type == "file_upload":
                    lock_key = self._macro_upload_lock_key(task)
                    self._upload_task_locks.add(lock_key)
                    task.status = "done"
                    task.retry_count = 0
                    task.wait_count = 0
                    self._step_log(
                        "upload_task_locked",
                        {
                            "step": self.step_count,
                            "task_id": task.task_id,
                            "task_type": task.task_type,
                            "lock_key": lock_key,
                            "selector": action.selector,
                            "ref": action.ref,
                        },
                    )
                    return
                if (
                    task.task_type
                    in ("question_single", "question_multi", "inference_required")
                    and action.selector
                ):
                    if any(
                        self._option_text_matches(done, action.selector)
                        for done in task.completed_options
                    ):
                        pass
                    else:
                        task.completed_options.append(action.selector)
                task.last_attempt_option = None
                # combobox type 后通常还需点 option，保留 in_progress；
                # combobox click/问题点击成功后交由下一轮状态检测判定是否 done。
                if task.task_type == "combobox_select" and action.action == "click":
                    task.status = "in_progress"
                else:
                    task.status = "in_progress"
                task.retry_count = 0
                task.wait_count = 0
            else:
                task.retry_count += 1
                if task.retry_count >= self._macro_retry_limit:
                    task.status = "blocked"
                    self._step_log(
                        "macro_task_blocked",
                        {
                            "step": self.step_count,
                            "task_id": task.task_id,
                            "task_type": task.task_type,
                            "reason": "retry_limit_exceeded",
                            "retry_count": task.retry_count,
                        },
                    )
                    self._record_failure_memory_case(
                        classification="unknown_blocked",
                        reason_code="macro_retry_limit_exceeded",
                        symptom="宏任务连续执行失败达到上限并被阻断",
                        root_cause=task.mapping_reason or task.title,
                        question_text=task.question_text or "",
                        action=task.task_type,
                        selector=action.selector or task.field_selector or "",
                        source_event="macro_task_blocked",
                    )
            break

    def _log_finalized(self, status: str, reason: str) -> None:
        if status == "manual_required":
            self._record_failure_memory_case(
                classification=self.last_outcome_class_hint or "unknown_blocked",
                reason_code=self.failure_code_hint or reason or "manual_required",
                symptom="流程最终转人工处理",
                root_cause=reason or self.manual_reason_hint or "manual_required",
                evidence_snippet=self.last_error_snippet_hint or "",
                question_text="",
                action="finalize",
                selector="",
                source_event="finalized",
            )
        self._step_log(
            "finalized",
            {
                "step": self.step_count,
                "status": status,
                "reason": (reason or "")[:280],
                "failure_class_hint": self.failure_class_hint,
                "failure_code_hint": self.failure_code_hint,
                "retry_count_hint": self.retry_count_hint,
            },
        )

    def _step_log(self, event: str, payload: dict) -> None:
        """写入每步证据链日志。"""
        data = {
            "job_id": self.job_id,
            "event": event,
            "timestamp": int(time.time() * 1000),
            "payload": payload,
        }
        try:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _smart_click(self, selector: str, element_type: str = None) -> bool:
        return exec_smart_click(
            self.page,
            selector,
            element_type=element_type,
            log_fn=lambda msg, lvl="info": self._log(msg, lvl),
        )

    def _smart_fill(self, selector: str, value: str) -> bool:
        return exec_smart_fill(self.page, selector, value)

    def _smart_type(self, selector: str, value: str) -> bool:
        return exec_smart_type(
            self.page,
            selector,
            value,
            log_fn=lambda msg, lvl="info": self._log(msg, lvl),
        )

    def _do_select(self, selector: str, value: str) -> bool:
        return exec_do_select(self.page, selector, value)

    def _do_upload(self, action: AgentAction, locator=None) -> bool:
        """
        执行可控文件上传：
        - 必须先检测到上传信号
        - 仅允许白名单目录内文件
        - 上传失败可重试并尝试候选文件回退
        """
        has_runtime_signal = bool(self._last_upload_signals)
        if not has_runtime_signal:
            # 兜底：即使文本信号不足，只要 DOM 能定位 file input 也继续上传
            fallback_locator = (
                locator
                if locator is not None
                else self._locate_file_input(action.selector)
            )
            if fallback_locator is None:
                self._log(
                    "⚠ 页面无上传信号，且未定位到 file input，跳过 upload 动作", "warn"
                )
                return False
            locator = fallback_locator
            self._step_log(
                "upload_signal_dom_override",
                {
                    "step": self.step_count,
                    "selector": action.selector,
                    "reason": "runtime_signal_empty_but_file_input_present",
                },
            )

        ordered_candidates = resolve_upload_candidate(
            action.value,
            self.upload_candidates,
        )
        # 任务预选简历优先（阶段A），失败再回退候选列表
        if self.preferred_resume_path:
            preferred = self.preferred_resume_path
            if is_upload_path_allowed(preferred):
                ordered_candidates = [preferred] + [
                    c for c in ordered_candidates if c != preferred
                ]

        if not ordered_candidates:
            self._log("⚠ 无可用上传候选文件（白名单目录为空）", "warn")
            return False

        max_attempts = min(3, len(ordered_candidates))
        for attempt_idx in range(max_attempts):
            candidate = ordered_candidates[attempt_idx]
            if not is_upload_path_allowed(candidate):
                self._log(f"⚠ 拒绝非白名单路径: {candidate}", "warn")
                continue

            target_locator = locator
            if target_locator is None:
                target_locator = self._locate_file_input(action.selector)
            if target_locator is None:
                self._log("⚠ 未定位到 file input，无法上传", "warn")
                return False

            try:
                target_locator.set_input_files(candidate, timeout=5000)
            except Exception as exc:
                self._log(
                    f"⚠ 上传失败，attempt={attempt_idx + 1}, file={Path(candidate).name}, err={exc}",
                    "warn",
                )
                continue

            if self._verify_upload_success(candidate):
                self._log(
                    f"✓ 上传成功，attempt={attempt_idx + 1}, file={Path(candidate).name}"
                )
                return True

            self._log(
                f"⚠ 上传后未确认成功，attempt={attempt_idx + 1}, file={Path(candidate).name}",
                "warn",
            )

        return False

    def _locate_file_input(self, selector: str | None):
        """
        尝试定位文件上传 input。
        """
        return exec_locate_file_input(
            self.page,
            selector,
            click_fn=lambda text, et=None: self._smart_click(text, et or "button"),
        )

    def _verify_upload_success(self, file_path: str) -> bool:
        """
        上传成功确认（多信号）：
        - input.files 非空且文件名匹配
        - 或页面文本出现文件名
        """
        return exec_verify_upload_success(self.page, file_path)

    def _do_scroll(self, direction: str) -> bool:
        """滚动页面"""
        return exec_do_scroll(self.page, direction)

    def _do_refresh(self, trigger: str = "unknown") -> bool:
        """
        刷新当前页面重试：
        - 最多允许两次
        - 超限后标记刷新耗尽
        """
        if self.refresh_attempts >= self.max_refresh_attempts:
            self.refresh_exhausted = True
            self._log(
                f"⚠ refresh 已达上限 ({self.max_refresh_attempts})，不再重试",
                "warn",
            )
            return False

        attempt = self.refresh_attempts + 1
        self._log(
            f"🔄 刷新当前页面重试 ({attempt}/{self.max_refresh_attempts}) trigger={trigger}",
            "warn",
        )
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(1200)
            self.refresh_attempts += 1
            self._force_visual_audit_next_plan = True
            # 刷新后清理缓存，避免沿用旧页面动作计划。
            self._state_cache_by_fingerprint.clear()
            self._action_fail_counts.clear()
            self._action_cache_use_counts.clear()
            self._repeated_skip_counts.clear()
            self._semantic_fail_counts.clear()
            self._error_gate_cache.clear()
            self._last_observed_fingerprint = ""
            self._macro_tasks = []
            self._active_macro_task_id = None
            self._macro_scope = ""
            self._upload_task_locks.clear()
            self._macro_disabled_scopes.clear()
            self._last_queue_plan = []
            self._same_task_failure_streak = 0
            self._last_failed_task_key = ""
            self.history.append(
                f"刷新页面重试({self.refresh_attempts}/{self.max_refresh_attempts})"
            )
            return True
        except Exception as e:
            self.refresh_attempts += 1
            self._log(f"⚠ 页面刷新失败: {e}", "warn")
            if self.refresh_attempts >= self.max_refresh_attempts:
                self.refresh_exhausted = True
            return False

    def _is_llm_refusal_response(self, raw: str) -> bool:
        lower = (raw or "").strip().lower()
        if not lower:
            return False
        refusal_tokens = (
            "i'm unable to assist with this request",
            "i am unable to assist with this request",
            "unable to assist with this request",
            "i can't assist with this request",
            "i cannot assist with this request",
            "can't help with this request",
            "cannot help with this request",
            "sorry, i can't help with that",
            "对不起，我不能",
            "无法协助",
            "无法帮助",
        )
        return any(token in lower for token in refusal_tokens)

    def _looks_like_external_blocked_text(self, text: str) -> bool:
        lower = (text or "").lower()
        blocked_tokens = (
            "flagged as possible spam",
            "couldn't submit your application",
            "submission was flagged",
            "suspicious activity",
            "too many requests",
            "rate limit",
            "try again later",
            "verify you are human",
            "security check",
        )
        return any(token in lower for token in blocked_tokens)

    def _looks_like_completion_text(self, lower_text: str) -> bool:
        return oc_looks_like_completion_text(lower_text)

    def _verify_completion(self) -> tuple[bool, str]:
        """二次验证：多信号终态评分，避免“已提交仍继续操作”。"""
        try:
            body_text = self.page.inner_text("body")
            lower_text = body_text.lower()
            error_indicators = [
                "this field is required",
                "please fill",
                "is required",
                "missing required",
                "please complete",
                "invalid",
            ]
            has_error = any(indicator in lower_text for indicator in error_indicators)
            has_submit_button = False
            try:
                _snapshot_text, _snapshot_map = build_ui_snapshot(self.page)
                has_submit_button = any(
                    item.role in ("button", "link")
                    and any(
                        kw in (item.name or "").lower()
                        for kw in ("submit", "apply", "continue")
                    )
                    for item in _snapshot_map.values()
                )
            except Exception:
                has_submit_button = False
            if not has_submit_button:
                try:
                    submit_btn = self.page.get_by_role("button", name="Submit").first
                    if submit_btn.is_visible(timeout=300):
                        has_submit_button = True
                except Exception:
                    pass
            try:
                current_url = self.page.url
            except Exception:
                current_url = ""
            assessment = oc_assess_completion_confidence(
                body_text=body_text,
                current_url=current_url,
                has_submit_button=has_submit_button,
                has_error=has_error,
            )
            self._step_log(
                "terminal_completion_assessed",
                {
                    "step": self.step_count,
                    "confirmed": assessment.confirmed,
                    "score": assessment.score,
                    "signals": assessment.signals,
                },
            )
            if assessment.confirmed:
                return True, f"终态评分通过(score={assessment.score:.2f})"
            if bool(assessment.signals.get("external_blocked")):
                return False, "检测到外部阻断信号，未完成提交"
            if has_error:
                return False, "页面仍有错误提示，表单未完成"
            if has_submit_button and not bool(assessment.signals.get("success_text")):
                return False, "Submit 按钮仍可见，表单尚未提交"
            return False, f"终态评分不足(score={assessment.score:.2f})"
        except Exception as e:
            self._log(f"⚠ 二次验证出错: {e}", "warn")
            return False, f"验证过程出错: {e}"

    def _compress_screenshot(self, png_bytes: bytes) -> bytes:
        """
        压缩截图：PNG → JPEG，限制宽度，降低体积但保证识别质量。

        压缩策略：
        - 转换为 JPEG 格式（比 PNG 体积小很多）
        - 限制最大宽度为 1280px（足够 LLM 识别文字和 UI 元素）
        - JPEG 质量 75（清晰度和体积的良好平衡）
        """
        try:
            # 打开 PNG 图片
            img = Image.open(io.BytesIO(png_bytes))

            # 如果宽度超过限制，等比例缩小
            if img.width > SCREENSHOT_MAX_WIDTH:
                ratio = SCREENSHOT_MAX_WIDTH / img.width
                new_height = int(img.height * ratio)
                img = img.resize(
                    (SCREENSHOT_MAX_WIDTH, new_height), Image.Resampling.LANCZOS
                )

            # 转换为 RGB（JPEG 不支持 RGBA）
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # 保存为 JPEG
            output = io.BytesIO()
            img.save(
                output, format="JPEG", quality=SCREENSHOT_JPEG_QUALITY, optimize=True
            )
            return output.getvalue()
        except Exception as e:
            # 压缩失败时返回原始 PNG
            self._log(f"⚠️ 截图压缩失败，使用原图: {e}", "warn")
            return png_bytes

    def _safe_parse_json(self, raw: str) -> dict | None:
        return planner_safe_parse_json(raw)

    def _log(self, message: str, level: str = "info") -> None:
        """写入日志"""
        with SessionLocal() as session:
            session.add(JobLog(job_id=self.job_id, level=level, message=message))
            session.commit()
        print(f"[job={self.job_id}] [{level.upper()}] {message}")

    def _set_manual_reason_hint(self, reason: str) -> None:
        """将人工介入原因同步给外层调用方。"""
        self.manual_reason_hint = reason
        try:
            setattr(self.job, "manual_reason_hint", reason)
        except Exception:
            pass


# 便捷函数
def run_browser_agent(
    page: Page,
    job,
    max_steps: int = 50,
    *,
    pre_nav_only: bool = False,
) -> bool:
    """运行浏览器 Agent"""
    agent = BrowserAgent(page, job, max_steps, pre_nav_only=pre_nav_only)
    success = agent.run()
    try:
        setattr(job, "manual_reason_hint", agent.manual_reason_hint)
        setattr(job, "failure_class_hint", agent.failure_class_hint)
        setattr(job, "failure_code_hint", agent.failure_code_hint)
        setattr(job, "retry_count_hint", agent.retry_count_hint)
        setattr(job, "last_error_snippet_hint", agent.last_error_snippet_hint)
        setattr(job, "last_outcome_class_hint", agent.last_outcome_class_hint)
        setattr(job, "last_outcome_at_hint", agent.last_outcome_at_hint)
    except Exception:
        pass
    return success
