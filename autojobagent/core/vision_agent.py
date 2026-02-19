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
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal
from urllib.parse import urlsplit

from openai import OpenAI
from playwright.sync_api import Page
from PIL import Image

from ..db.database import SessionLocal
from ..models.job_log import JobLog
from ..config import (
    get_user_info_for_prompt,
    load_agent_guidelines,
    list_upload_candidates,
    is_upload_path_allowed,
    resolve_upload_candidate,
)
from .browser_manager import BrowserManager
from .debug_probe import append_debug_log
from .ui_snapshot import build_ui_snapshot, SnapshotItem
from .heuristics import assess_manual_required


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
class SubmissionOutcome:
    classification: Literal[
        "success_confirmed",
        "validation_error",
        "external_blocked",
        "transient_network",
        "unknown_blocked",
    ]
    reason_code: str
    evidence_snippet: str


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
            and has_enabled_submit
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
        self._intent_cache: dict[str, dict[str, list[str]]] = {}
        self._last_snapshot_intents: dict[str, set[str]] = {}
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
        self._last_submission_outcome: SubmissionOutcome | None = None
        self.failure_class_hint: str | None = None
        self.failure_code_hint: str | None = None
        self.retry_count_hint: int = 0
        self.last_error_snippet_hint: str | None = None
        self.last_outcome_class_hint: str | None = None
        self.last_outcome_at_hint: datetime | None = None

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
            self._log(f"📋 状态: {state.summary}")
            if state.page_overview:
                self._log(f"🧭 页面概览: {state.page_overview}")
            if state.field_audit:
                self._log(f"🧾 字段审计: {state.field_audit}")
            if state.action_plan:
                self._log(f"🗺 计划序列: {' -> '.join(state.action_plan[:5])}")
            if state.risk_or_blocker:
                self._log(f"⚠ 风险/阻塞: {state.risk_or_blocker}")

            # 3. 检查是否完成（带二次验证）
            if state.status == "done":
                if self.pre_nav_only:
                    self._log("✓ 预导航完成：已进入申请页")
                    self._log("========== AI Agent 运行结束 ==========")
                    return True
                self._log("🔍 Agent 判断任务完成，进行二次验证...")

                # 二次验证：检查页面是否真的显示成功信息
                is_really_done, verification_msg = self._verify_completion()

                if is_really_done:
                    self._log(f"✓ 二次验证通过: {verification_msg}")
                    self._log("========== AI Agent 运行结束 ==========")
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
                return False

            # 4. 执行下一步操作
            if state.next_action:
                action = state.next_action
                fp = state.page_fingerprint or self._last_observed_fingerprint
                semantic_guard = self._semantic_loop_guard_decision(fp, action)
                if semantic_guard == "replan":
                    if fp:
                        self._state_cache_by_fingerprint.pop(fp, None)
                    self.history.append(
                        f"步骤{self.step_count}: 语义动作重复失败，清理缓存并强制重规划 {action.action}({action.ref or action.selector or ''})"
                    )
                    self.consecutive_failures += 1
                    continue
                if semantic_guard == "alternate":
                    alternate_action = self._build_alternate_action(action)
                    if alternate_action is not None:
                        self._log("⚠ 语义动作重复失败，改用替代动作", "warn")
                        action = alternate_action
                    else:
                        if fp:
                            self._state_cache_by_fingerprint.pop(fp, None)
                        self.history.append(
                            f"步骤{self.step_count}: 语义动作重复失败，暂无替代动作，强制重规划 {action.action}({action.ref or action.selector or ''})"
                        )
                        self.consecutive_failures += 1
                        continue
                if semantic_guard == "stop":
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
                    return False
                if self._should_skip_repeated_action(fp, action):
                    skip_key = self._action_fail_key(fp, action)
                    skip_count = self._repeated_skip_counts.get(skip_key, 0) + 1
                    self._repeated_skip_counts[skip_key] = skip_count
                    self._log(
                        "⚠ 检测到同页面重复失败动作，触发重规划而不重复执行",
                        "warn",
                    )
                    alternate_action = self._build_alternate_action(action)
                    if alternate_action is not None:
                        self._log("   ↪ 尝试同页替代动作以打破循环")
                        action = alternate_action
                    elif skip_count == 1:
                        # 第一次跳过时清理该页缓存，强制下一步重规划。
                        if fp:
                            self._state_cache_by_fingerprint.pop(fp, None)
                        self.history.append(
                            f"步骤{self.step_count}: 跳过重复失败动作后清理页面计划缓存 {action.action}({action.ref or action.selector or ''})"
                        )
                        self.consecutive_failures += 1
                        continue
                    elif skip_count >= 3:
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
                if self._is_progression_action(action, item=source_item):
                    success, should_stop = self._handle_submission_outcome(
                        action, success
                    )
                    if should_stop:
                        self._record_action_result(fp, action, False)
                        self._set_manual_reason_hint(
                            self._build_submission_manual_reason(action)
                        )
                        self._log(
                            "⚠ 提交阻断达到重试上限，停止执行并转人工处理", "warn"
                        )
                        self._log("========== AI Agent 运行结束（提交阻断）==========")
                        return False
                self._record_action_result(fp, action, success)

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
                    self.consecutive_failures += 1  # 增加连续失败计数

                if success:
                    self._log("   ✓ 执行成功")
                else:
                    self._log(
                        f"   ❌ 执行失败 (连续失败: {self.consecutive_failures}/{self.max_consecutive_failures})",
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

                    # 智能终止：连续失败次数过多
                    if self.consecutive_failures >= 3:
                        if self.refresh_attempts < self.max_refresh_attempts:
                            self._log(
                                f"⚠ 连续失败达到 {self.consecutive_failures} 次，触发页面刷新重试",
                                "warn",
                            )
                            refreshed = self._do_refresh(trigger="auto_stuck_recovery")
                            if refreshed:
                                self.consecutive_failures = 0
                                continue
                        elif self.refresh_exhausted:
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
                            return False

                    if self.consecutive_failures >= self.max_consecutive_failures:
                        self._set_manual_reason_hint(
                            "连续操作失败达到上限，需要人工处理"
                        )
                        self._log(
                            f"⚠ 连续 {self.consecutive_failures} 次操作失败，停止执行",
                            "warn",
                        )
                        self._log("========== AI Agent 运行结束（智能终止）==========")
                        return False

                # 等待页面响应后立即截图（让 AI 看到实时变化）
                # 短暂等待让页面 UI 更新（如下拉框出现）
                self.page.wait_for_timeout(500)
            else:
                self._log("⚠ LLM 没有给出下一步操作", "warn")

        self._log(f"⚠ 已达到最大步数 {self.max_steps}，停止执行", "warn")
        self._set_manual_reason_hint("已达到最大步数仍未完成，需要人工处理")
        self._log("========== AI Agent 运行结束 ==========")
        return False

    def _observe_and_think(self) -> AgentState:
        """
        观察当前页面状态，让 LLM 思考下一步。
        """
        # 1. 截图（压缩优化）
        screenshot_b64 = None
        try:
            png_bytes = self.page.screenshot(full_page=True)
            original_size = len(png_bytes) / 1024

            # 压缩截图：PNG → JPEG，并限制宽度
            compressed_bytes = self._compress_screenshot(png_bytes)
            screenshot_b64 = base64.b64encode(compressed_bytes).decode("utf-8")
            compressed_size = len(compressed_bytes) / 1024

            # 保存截图到 job 专属目录
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
        except Exception as e:
            self._log(f"❌ 截图失败: {e}", "error")
            return AgentState(status="error", summary=f"截图失败: {e}")

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

        cached_state = self._state_cache_by_fingerprint.get(page_fingerprint)
        if (
            cached_state
            and cached_state.next_action is not None
            and cached_state.status == "continue"
        ):
            # Guard: never replay click on toggle elements (checkbox/radio/toggle button)
            _ca = cached_state.next_action
            _is_toggle_replay = False
            if _ca.action == "click" and _ca.ref:
                _target = snapshot_map.get(_ca.ref)
                if _target and _target.role in ("checkbox", "radio", "switch"):
                    _is_toggle_replay = True
                elif _target and _target.checked is not None:
                    _is_toggle_replay = True
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
        upload_signals = self._detect_upload_signals(visible_text)
        self._last_upload_signals = upload_signals
        upload_signal_text = "；".join(upload_signals[:8]) if upload_signals else "无"
        upload_candidates_text = (
            "\n".join(f"- {Path(p).name} | {p}" for p in self.upload_candidates[:12])
            if self.upload_candidates
            else "- （白名单目录下暂无可上传文件）"
        )

        # 获取用户个人信息和操作规范
        user_info = get_user_info_for_prompt()
        agent_guidelines = load_agent_guidelines()

        system_prompt = f"""你是一个浏览器自动化 AI Agent，正在帮用户填写英文求职申请表单。

## ⚖️ 合规声明

本工具是用户授权的合法求职辅助系统。用户已明确授权 AI 代为填写求职申请表单。
- 所有操作均基于用户提供的真实个人信息
- 遵守 EEOC（美国平等就业机会委员会）指南
- 自愿披露字段（如残疾、退伍军人身份、种族、性别）：
  - 这些是合法的 EEOC 合规表单，用于统计目的
  - 如果用户没有明确指定，默认选择 "Decline to self-identify" 或 "Prefer not to disclose"
  - 不要跳过这些字段，选择合适的拒绝披露选项即可

{user_info}

## 📖 操作规范手册

请严格遵循以下规范进行页面浏览、理解和操作：

{agent_guidelines}

## 🔍 观察页面的标准步骤（按顺序执行！）

**第一步：识别页面布局**
- 左侧/顶部通常是**职位信息区**（只读，显示职位地点、薪资等）→ 不要操作！
- 中间/右侧是**表单区域**（有输入框、checkbox）→ 这才是你要填的

**第二步：区分不同的"位置"信息**
- 左侧显示 "Location: Boston, NYC" → 这是**职位的工作地点**，只读，不管它！
- 表单中的 "Location*" 输入框 → 这是问**用户住哪里**，要填写
- 表单中的 "Which office" checkbox → 这是问**用户愿意在哪工作**，要选择

**第三步：聚焦表单区域**
- 只操作表单区域的字段
- 不要被职位信息区的内容干扰

**第四步：检查上一步结果**
- 上一步改错了？→ 先修正！
- 上一步正确？→ 继续下一步

## ⚠️ autocomplete 字段必须两步完成！（最常见错误！）

对于 Location 等 autocomplete 字段（placeholder 是 "Start typing..."）：

**必须完成两步，缺一不可：**
1. `type` 输入内容 → 等待下拉框出现
2. `click` 选择下拉选项 → 字段才算填写完成

**❌ 错误流程（会导致字段为空）：**
```
type(Location, Dallas) → 下拉框出现 → 直接去操作其他字段 → Location 变空！
```

**✅ 正确流程：**
```
type(Location, Dallas) → 下拉框出现 → click(Dallas, Texas, United States) → 完成！
```

**🔍 关键判断规则：**
| 你看到什么 | 下一步必须做什么 |
|-----------|-----------------|
| 下拉框出现，有选项列表 | **必须 click 选择选项！不能跳过！** |
| autocomplete 字段显示 "Start typing..." | 需要 type 输入 |
| autocomplete 字段显示完整地址（如 "Dallas, Texas, United States"） | 已完成，可以跳过 |

**⚠️ 绝对禁止：在下拉框出现时去操作其他字段或点击 Submit！**

## checkbox 多选逻辑（重要！）

**取交集原则：**
1. 查看页面提供的所有选项
2. 对比用户偏好（从用户信息中获取）
3. 交集 = 用户偏好中有的 AND 页面也提供的

**模糊匹配：**
- Boston = Boston (Cambridge) ✓
- New York = New York City (Chelsea) = NYC ✓
- SF = San Francisco ✓
- 推理判断是同一事物 → 使用**页面显示的完整名称**

**示例：**
```
用户偏好: [Boston, New York, SF, LA, Dallas]
页面选项: [Boston (Cambridge), NYC (Chelsea), LA (Venice), SF, Remote only]
交集: Boston (Cambridge), NYC (Chelsea), LA (Venice), SF
→ 排除 Remote only（用户偏好里没有）
```

**全部执行规则：**
- 交集有 N 个选项，就必须勾选 N 个
- 规划了选 4 个城市 → 全部勾选后再继续
- 不要选一个就认为完成！

## 开放式问题处理

当页面只有问题没有选项（如"你的技能是什么？"）：
- 从用户资料提取相关信息
- 默认填写 3 个有效值
- 用逗号分隔
- 示例：fill("Python, Machine Learning, Deep Learning")

## 观察当前截图并决定操作

- **下拉框出现** → **立即点击正确选项**（最高优先级！）
- **空的必填字段** → 填写内容
- **checkbox 多选** → 按交集规划**逐个勾选**，全部完成再继续
- **Submit 按钮且没有错误提示** → 点击提交
- **感谢信息** → 返回 done

## 可用操作

| 操作 | 使用场景 | selector/ref | value |
|------|----------|--------------|-------|
| click | 按钮、Yes/No选项、checkbox、radio、下拉选项 | 元素文本或 ref | - |
| fill | 普通输入框（Name、Email等） | 字段标签或 ref | 内容 |
| type | autocomplete 输入框（Location等） | 字段标签或 ref | 内容 |
| upload | 上传简历/附件（仅在页面有上传信号时） | 上传控件文本或 ref | 候选文件名或完整路径 |
| scroll | 滚动页面 | - | up/down |
| refresh | 当前页面卡住/多次无进展时刷新重试 | - | - |
| done | 任务完成 | - | - |
| stuck | 无法继续 | - | - |

**重要区分：**
- Yes/No 按钮 → 用 **click**，selector 填 "Yes" 或 "No"
- 文本输入框 → 用 fill 或 type
- 看到 "Start typing..." → 用 type
- 同名 Yes/No 出现多个时，必须返回 target_question 绑定到对应问题

## 返回 JSON（优先使用 ref）
{{
  "status": "continue/done/stuck",
  "summary": "当前看到什么（中文）",
  "page_overview": "页面结构与关键信息概览（可选）",
  "field_audit": "必填项已完成/未完成清单（可选）",
  "action_plan": ["计划步骤1", "计划步骤2"],
  "risk_or_blocker": "当前潜在风险或阻塞（可选）",
  "next_action": {{
    "action": "操作",
    "ref": "可交互元素 ref（优先使用）",
    "element_type": "button/link/checkbox/radio/input/option",
    "selector": "目标",
    "value": "值",
    "target_question": "若是 Yes/No 等回答型按钮，填写对应问题文本（可选）",
    "reason": "为什么"
  }}
}}

## 规则
1. 使用用户真实信息，不编造
2. 所有内容用英文填写
3. 已上传的文件不重复上传
4. 只有在页面存在上传信号时才允许使用 upload 动作
5. refresh 最多使用 2 次；若两次后仍无进展，返回 stuck
6. 同名 Yes/No 出现多个时，必须先绑定 target_question 后再点击
7. 若提交被阻止，先修复报错字段，不得立即重复提交

## 什么时候返回 stuck？（重要！不要轻易放弃！）

**只有这些情况才返回 stuck：**
- 需要登录但没有账号
- 出现验证码（CAPTCHA）
- 页面完全无法加载
- 需要付费
- 只有看到 sign in/login 文案还不够，必须有密码框或验证码等强证据

**这些情况不是 stuck，要继续操作：**
- 某个字段填错了 → 点击正确选项修复
- checkbox 选错了 → 点击正确的 checkbox
- 有错误提示 → 修复对应字段
- 页面有多个选项 → 选择最合适的

**核心原则：能操作就操作，不要轻易放弃！**"""

        # 构建 user_prompt（根据是否是新页面调整引导）
        new_page_hint = "[新页面] " if is_new_page else ""

        user_prompt = f"""历史:
{history_text}

## 页面可见文本（截断）
{visible_text}

## 可交互元素快照（ref → 元素）
{snapshot_text}

## 上传信号检测
{upload_signal_text}

## Simplify 系统探针状态（以此为准）
- state: {self.simplify_state}
- message: {self.simplify_message or "n/a"}
- 规则：若 state 为 unavailable/unknown，不得声称“Simplify 已自动填写”

## 白名单可上传候选文件（仅可从以下文件中选择）
{upload_candidates_text}

## {new_page_hint}请按以下步骤处理当前页面：

**1. 完整扫描并规划（列出所有空缺！）**
- 仅当上方 Simplify state=completed/running 时，才能提及 Simplify 已填写
- 列出**所有**空缺必填字段，不要只说第一个！
- 每个字段给出**具体值**（从用户信息查找）
- checkbox 多选：取"用户偏好 ∩ 页面选项"的交集（模糊匹配）
- 开放式问题（无选项）：默认填 3 个相关值
- 示例：" 空缺 3 项：1. Location → Dallas；2. Which office → 交集4个(Boston/NYC/LA/SF)；3. Skills → Python, ML, DL"

**规则：规划的选项必须全部执行！**
- checkbox 规划了 4 个 → 选完 4 个再继续
- 不要选一个就认为完成

**2. 检查下拉框（最高优先级！）**
- 有下拉框出现？→ **立即 click 选择！**
- 不要跳过下拉框去操作其他字段

**3. 识别页面布局**
- 左侧/顶部的职位信息区（只读）→ 不管它！
- 中间的表单区域 → 这才是要操作的

**4. 区分位置信息（最容易混淆！）**
- 左侧 "Location: XXX" → 这是**职位地点**，不管它！
- 表单 "Location*" 输入框 → 问**用户住哪里**
- 表单 "Which office" checkbox → 问**用户愿意在哪工作**

**5. 检查上一步结果**
- 上一步操作的字段是否正确？
- autocomplete 下拉框出现但没选中？→ 必须先 click 选择！
- 如果改错了 → 先修正！

**6. 按规划顺序执行**
- **下拉框出现** → 立即 click 选择
- autocomplete 显示 "Start typing..." → type 输入
- 空的普通必填字段 → fill 填写
- 页面有上传信号且需要简历/CV 时 → 使用 upload（value 填候选文件名或完整路径）
- checkbox 多选 → 按规划**逐个勾选**，全部完成再继续
- 如果当前是职位详情页且有“进入申请流程”的按钮/链接（同义表达也算）→ 先点击进入申请页，不要误判 stuck
- 都填好了且无错误提示 → Submit
- 感谢/确认信息 → done"""

        # 5. 调用 LLM（带模型降级机制）
        self._log(f"🤔 正在思考... (模型: {self.model})")

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{screenshot_b64}"
                        },
                    },
                ],
            },
        ]
        # region agent log
        self._ndjson_log(
            hypothesis_id="H1",
            location="vision_agent:_observe_and_think:before_llm",
            message="pre LLM call",
            data={
                "model": self.model,
                "step": self.step_count,
                "screenshot_b64_len": len(screenshot_b64 or ""),
                "visible_text_len": len(visible_text),
                "upload_signals": upload_signals[:5],
                "upload_candidates_count": len(self.upload_candidates),
            },
        )
        # endregion

        raw = None
        while self.model_index < len(self.fallback_models):
            try:
                completion = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.llm_cfg.get("temperature", 0.2),
                    top_p=0.8,
                    max_tokens=self.llm_cfg.get("max_tokens", 1000),
                    messages=messages,
                )
                raw = completion.choices[0].message.content or ""
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
                break  # 成功则跳出
            except Exception as e:
                error_str = str(e)
                error_lower = error_str.lower()
                # 检测 429 Rate Limit 错误
                if "429" in error_str or "rate_limit" in error_lower:
                    self._log(f"⚠️ 模型 {self.model} 遇到速率限制", "warn")
                    # 尝试切换到下一个模型
                    self.model_index += 1
                    if self.model_index < len(self.fallback_models):
                        self.model = self.fallback_models[self.model_index]
                        self._log(f"🔄 切换到模型: {self.model}")
                        time.sleep(1)  # 短暂等待后重试
                    else:
                        self._log("❌ 所有模型都遇到速率限制，请稍后重试", "error")
                        return AgentState(
                            status="error", summary="所有模型都遇到速率限制"
                        )
                # 模型能力不匹配（如不支持图片输入）时也尝试回退到下一个模型
                elif any(
                    kw in error_lower
                    for kw in [
                        "does not support",
                        "unsupported",
                        "multimodal",
                        "vision",
                        "image_url",
                        "invalid model",
                        "model_not_found",
                        "not found",
                    ]
                ):
                    self._log(
                        f"⚠️ 模型 {self.model} 能力不匹配或不可用，尝试回退", "warn"
                    )
                    self.model_index += 1
                    if self.model_index < len(self.fallback_models):
                        self.model = self.fallback_models[self.model_index]
                        self._log(f"🔄 切换到模型: {self.model}")
                        time.sleep(1)
                    else:
                        self._log("❌ 所有候选模型都不支持当前请求", "error")
                        return AgentState(
                            status="error", summary="所有候选模型都不支持当前请求"
                        )
                else:
                    self._log(f"❌ LLM 调用失败: {e}", "error")
                    return AgentState(status="error", summary=f"LLM 调用失败: {e}")

        if raw is None:
            return AgentState(status="error", summary="LLM 未返回结果")

        # 6. 解析返回
        data = self._safe_parse_json(raw)
        if not data:
            self._log(f"❌ LLM 返回格式错误: {raw[:300]}", "error")
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
            return AgentState(
                status="error", summary="LLM 返回格式错误", raw_response=raw
            )

        status = data.get("status", "continue")
        summary = data.get("summary", "")
        page_overview = data.get("page_overview")
        field_audit = data.get("field_audit")
        action_plan = data.get("action_plan")
        risk_or_blocker = data.get("risk_or_blocker")
        if not isinstance(page_overview, str):
            page_overview = None
        if not isinstance(field_audit, str):
            field_audit = None
        if not isinstance(action_plan, list):
            action_plan = None
        else:
            action_plan = [str(x) for x in action_plan[:8]]
        if not isinstance(risk_or_blocker, str):
            risk_or_blocker = None
        if self.simplify_state.lower() in ("unavailable", "unknown"):
            summary = self._sanitize_simplify_claims(summary)
            page_overview = self._sanitize_simplify_claims(page_overview)
            field_audit = self._sanitize_simplify_claims(field_audit)
            risk_or_blocker = self._sanitize_simplify_claims(risk_or_blocker)
            if action_plan:
                action_plan = [
                    self._sanitize_simplify_claims(x) or "" for x in action_plan
                ]

        next_action = None
        if status == "continue" and data.get("next_action"):
            act = data["next_action"]
            target_question = act.get("target_question")
            if target_question is not None and not isinstance(target_question, str):
                target_question = str(target_question)
            next_action = AgentAction(
                action=act.get("action", ""),
                ref=act.get("ref"),
                selector=act.get("selector"),
                value=act.get("value"),
                target_question=target_question,
                element_type=act.get("element_type"),
                reason=act.get("reason"),
            )

        result_state = AgentState(
            status=status,
            summary=summary,
            next_action=next_action,
            raw_response=raw,
            page_overview=page_overview,
            field_audit=field_audit,
            action_plan=action_plan,
            risk_or_blocker=risk_or_blocker,
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

            if action.action == "click":
                if self._is_answer_click_action(action):
                    bound = self._try_answer_binding_click(action)
                    if bound is True:
                        return True
                    if bound is False:
                        return False
                if self._is_progression_action(action):
                    blocked_reason = self._get_progression_block_reason()
                    if blocked_reason:
                        self._log(f"⚠ 阻止盲目前进：{blocked_reason}", "warn")
                        return False
                return self._smart_click(action.selector, action.element_type)

            elif action.action == "fill":
                return self._smart_fill(action.selector, action.value)

            elif action.action == "type":
                return self._smart_type(action.selector, action.value)

            elif action.action == "select":
                return self._do_select(action.selector, action.value)

            elif action.action == "upload":
                return self._do_upload(action)

            elif action.action == "scroll":
                direction = action.value or action.selector or "down"
                return self._do_scroll(direction)

            elif action.action == "refresh":
                return self._do_refresh(trigger="llm_action")

            elif action.action == "wait":
                seconds = int(action.value or 2)
                self.page.wait_for_timeout(seconds * 1000)
                return True

            elif action.action in ("done", "stuck"):
                return True

            else:
                self._log(f"未知操作类型: {action.action}", "warn")
                return False

        except Exception as e:
            self._log(f"执行异常: {e}", "error")
            return False

    def _execute_ref_action(self, action: AgentAction) -> bool:
        """基于快照 ref 执行动作（确定性定位）。"""
        item = self._last_snapshot_map.get(action.ref or "")
        if not item:
            self._log(f"ref 不存在: {action.ref}", "warn")
            return False

        locator = self._locator_from_snapshot_item(item)
        if locator is None:
            return False

        try:
            if action.action == "click":
                if self._is_answer_click_action(action, item=item):
                    bound = self._try_answer_binding_click(action)
                    if bound is True:
                        self._step_log(
                            "action_verify",
                            {"action": action.action, "ref": action.ref, "ok": True},
                        )
                        return True
                    if bound is False:
                        self._step_log(
                            "action_verify",
                            {"action": action.action, "ref": action.ref, "ok": False},
                        )
                        return False
                if self._is_progression_action(action, item=item):
                    blocked_reason = self._get_progression_block_reason()
                    if blocked_reason:
                        self._log(f"⚠ 阻止盲目前进：{blocked_reason}", "warn")
                        return False
                locator.click(timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log(
                        "action_verify",
                        {"action": action.action, "ref": action.ref, "ok": True},
                    )
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log(
                    "action_verify",
                    {"action": action.action, "ref": action.ref, "ok": ok},
                )
                return ok
            if action.action == "fill":
                if action.value is None:
                    return False
                locator.fill(str(action.value), timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log(
                        "action_verify",
                        {"action": action.action, "ref": action.ref, "ok": True},
                    )
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log(
                    "action_verify",
                    {"action": action.action, "ref": action.ref, "ok": ok},
                )
                return ok
            if action.action == "type":
                if action.value is None:
                    return False
                locator.click(timeout=800)
                locator.type(str(action.value), delay=40)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log(
                        "action_verify",
                        {"action": action.action, "ref": action.ref, "ok": True},
                    )
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log(
                    "action_verify",
                    {"action": action.action, "ref": action.ref, "ok": ok},
                )
                return ok
            if action.action == "select":
                if action.value is None:
                    return False
                try:
                    locator.select_option(label=str(action.value), timeout=2000)
                except Exception:
                    locator.click(timeout=1500)
                if self._verify_ref_action_effect(action, locator, item):
                    self._step_log(
                        "action_verify",
                        {"action": action.action, "ref": action.ref, "ok": True},
                    )
                    return True
                ok = self._retry_ref_action(action, locator, item)
                self._step_log(
                    "action_verify",
                    {"action": action.action, "ref": action.ref, "ok": ok},
                )
                return ok
            if action.action == "upload":
                return self._do_upload(action, locator=locator)
            if action.action == "scroll":
                direction = action.value or action.selector or "down"
                return self._do_scroll(direction)
            if action.action == "refresh":
                return self._do_refresh(trigger="llm_action")
            if action.action in ("wait", "done", "stuck"):
                if action.action == "wait":
                    seconds = int(action.value or 2)
                    self.page.wait_for_timeout(seconds * 1000)
                return True
        except Exception as e:
            self._log(f"ref 执行失败: {e}", "warn")
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

    def _collect_manual_required_evidence(
        self,
        visible_text: str,
        snapshot_map: dict[str, SnapshotItem],
        snapshot_intents: dict[str, set[str]],
    ) -> dict[str, int | bool]:
        """收集登录/验证码判定所需 DOM+文本证据。"""
        password_input_count = self._safe_locator_count("input[type='password']")
        captcha_selectors = [
            "iframe[src*='recaptcha']",
            ".g-recaptcha",
            "iframe[src*='hcaptcha']",
            ".h-captcha",
            "[data-sitekey][data-callback]",
            "iframe[title*='captcha' i]",
        ]
        captcha_element_count = self._count_visible_captcha_challenge(captcha_selectors)
        lower_text = (visible_text or "").lower()
        captcha_challenge_phrases = [
            "i am not a robot",
            "verify you are human",
            "security check",
            "complete the challenge",
            "select all images",
            "are you human",
        ]
        has_captcha_challenge_text = any(
            p in lower_text for p in captcha_challenge_phrases
        )

        has_login_button = any(
            ref in snapshot_map
            and snapshot_map[ref].role in ("button", "link")
            and "login_action" in intents
            for ref, intents in snapshot_intents.items()
        )
        has_apply_cta = any(
            ref in snapshot_map
            and snapshot_map[ref].role in ("button", "link")
            and "apply_entry" in intents
            for ref, intents in snapshot_intents.items()
        )
        page_text_intents = self._infer_text_intents(visible_text, limit=1200)
        has_login_button = has_login_button or ("login_action" in page_text_intents)

        captcha_selector_details = self._collect_selector_details(captcha_selectors)
        # region agent log
        append_debug_log(
            location="vision_agent.py:_collect_manual_required_evidence:captcha",
            message="captcha selector diagnostics",
            data={
                "job_id": self.job_id,
                "url": getattr(self.page, "url", ""),
                "captcha_selector_details": captcha_selector_details,
                "password_input_count": password_input_count,
                "has_captcha_challenge_text": has_captcha_challenge_text,
                "has_login_button": has_login_button,
                "has_apply_cta": has_apply_cta,
                "page_text_intents": sorted(page_text_intents),
            },
            run_id="pre-fix-debug",
            hypothesis_id="H1",
        )
        # endregion

        return {
            "password_input_count": password_input_count,
            "captcha_element_count": captcha_element_count,
            "has_captcha_challenge_text": has_captcha_challenge_text,
            "has_login_button": has_login_button,
            "has_apply_cta": has_apply_cta,
        }

    def _classify_page_state(
        self,
        snapshot_map: dict[str, SnapshotItem],
        evidence: dict[str, int | bool],
        manual_assessment,
    ) -> str:
        """轻量页面状态分类：login/captcha、职位详情页、申请页。"""
        if manual_assessment.manual_required:
            return "manual_gate"

        form_roles = {"textbox", "combobox", "checkbox", "radio", "file_input"}
        form_item_count = sum(
            1
            for item in snapshot_map.values()
            if item.role in form_roles and (item.in_form or item.required)
        )
        has_form_fields = form_item_count >= 2
        has_apply_cta = bool(evidence.get("has_apply_cta", False))
        current_url = ""
        try:
            current_url = (self.page.url or "").lower()
        except Exception:
            current_url = ""
        looks_like_application_url = (
            "/application" in current_url
            or "/apply" in current_url
            or "greenhouse.io" in current_url
        )
        # region agent log
        append_debug_log(
            location="vision_agent.py:_classify_page_state:inputs",
            message="page state classification inputs",
            data={
                "job_id": self.job_id,
                "url": current_url,
                "form_item_count": form_item_count,
                "has_form_fields": has_form_fields,
                "has_apply_cta": has_apply_cta,
                "looks_like_application_url": looks_like_application_url,
            },
            run_id="pre-fix-debug",
            hypothesis_id="H5",
        )
        # endregion

        if looks_like_application_url:
            return "application_or_form_page"

        if has_apply_cta and not has_form_fields:
            return "job_detail_with_apply"
        return "application_or_form_page"

    def _build_apply_entry_action(
        self,
        snapshot_map: dict[str, SnapshotItem],
        snapshot_intents: dict[str, set[str]],
    ) -> AgentAction | None:
        """在职位详情页中优先定位进入申请流程的 Apply 按钮。"""
        current_url = ""
        try:
            current_url = (self.page.url or "").lower()
        except Exception:
            current_url = ""
        if "/application" in current_url or "/apply" in current_url:
            return None

        candidates: list[SnapshotItem] = []
        for ref, item in snapshot_map.items():
            if item.role not in ("button", "link"):
                continue
            intents = snapshot_intents.get(ref, set())
            if "apply_entry" in intents:
                label = (item.name or "").lower()
                # 明确排除非“进入申请页”的按钮，避免把 Replace/Upload 当作 Apply
                if any(
                    bad in label
                    for bad in [
                        "replace",
                        "upload",
                        "autofill",
                        "tailor",
                        "settings",
                        "profile",
                        "close",
                    ]
                ):
                    continue
                candidates.append(item)
        if not candidates:
            return None
        # 优先 button，名称更具体者优先，避免点到噪声链接
        candidates.sort(
            key=lambda it: (
                it.role != "button",
                len(it.name),
            )
        )
        picked = candidates[0]
        return AgentAction(
            action="click",
            ref=picked.ref,
            selector=picked.name,
            element_type=picked.role,
            reason="职位详情页检测到 Apply 入口，先进入申请页",
        )

    def _safe_locator_count(self, selector: str) -> int:
        try:
            return self.page.locator(selector).count()
        except Exception:
            return 0

    def _collect_selector_details(self, selectors: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for selector in selectors:
            try:
                details = self.page.evaluate(
                    """
                    (sel) => {
                      const nodes = Array.from(document.querySelectorAll(sel));
                      const isVisible = (el) => {
                        const st = window.getComputedStyle(el);
                        if (!st) return false;
                        if (st.display === "none" || st.visibility === "hidden") return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const samples = nodes.slice(0, 3).map((el) => ({
                        tag: (el.tagName || "").toLowerCase(),
                        id: el.id || "",
                        className: String(el.className || "").slice(0, 80),
                        text: String(el.textContent || "").trim().slice(0, 120),
                        visible: isVisible(el),
                        rect: (() => {
                          const r = el.getBoundingClientRect();
                          return { w: Math.round(r.width), h: Math.round(r.height) };
                        })()
                      }));
                      return {
                        total: nodes.length,
                        visible: samples.filter((s) => s.visible).length,
                        samples,
                      };
                    }
                    """,
                    selector,
                )
            except Exception as exc:
                details = {"error": str(exc)}
            out[selector] = details
        return out

    def _count_visible_captcha_challenge(self, selectors: list[str]) -> int:
        """只统计可见验证码挑战节点，排除 recaptcha 法律声明文本。"""
        total = 0
        for selector in selectors:
            try:
                count = self.page.evaluate(
                    """
                    (sel) => {
                      const nodes = Array.from(document.querySelectorAll(sel));
                      const isVisible = (el) => {
                        const st = window.getComputedStyle(el);
                        if (!st) return false;
                        if (st.display === "none" || st.visibility === "hidden") return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                      };
                      const isLegalNotice = (el) => {
                        const cls = String(el.className || "").toLowerCase();
                        const text = String(el.textContent || "").toLowerCase();
                        return (
                          cls.includes("recaptchalegal") ||
                          (text.includes("protected by recaptcha") &&
                           text.includes("privacy policy") &&
                           text.includes("terms of service"))
                        );
                      };
                      return nodes.filter((el) => isVisible(el) && !isLegalNotice(el)).length;
                    }
                    """,
                    selector,
                )
                total += int(count or 0)
            except Exception:
                continue
        return total

    def _infer_snapshot_intents(
        self,
        snapshot_map: dict[str, SnapshotItem],
        visible_text: str,
    ) -> dict[str, set[str]]:
        """为当前快照中的按钮/链接推断语义意图。"""
        ref_to_label: dict[str, str] = {}
        for ref, item in snapshot_map.items():
            if item.role not in ("button", "link"):
                continue
            name = (item.name or "").strip()
            if not name:
                continue
            ref_to_label[ref] = name

        if not ref_to_label:
            return {}

        label_intents = self._infer_label_intents(
            list(ref_to_label.values()),
            context=visible_text[:800],
        )
        ref_intents: dict[str, set[str]] = {}
        for ref, label in ref_to_label.items():
            ref_intents[ref] = label_intents.get(label, set())
        return ref_intents

    def _infer_label_intents(
        self,
        labels: list[str],
        context: str = "",
    ) -> dict[str, set[str]]:
        """
        对一组 UI 文本做语义意图分类。
        优先使用低成本文本模型，失败回退到强共识关键词。
        """
        cleaned = []
        seen = set()
        for label in labels:
            text = (label or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        if not cleaned:
            return {}

        cache_key = self._intent_cache_key(cleaned, context)
        cached = self._intent_cache.get(cache_key)
        if cached is not None:
            return {k: set(v) for k, v in cached.items()}

        result = self._infer_label_intents_with_llm(cleaned, context)
        if result is None:
            result = {label: self._fallback_label_intents(label) for label in cleaned}
        else:
            for label in cleaned:
                result.setdefault(label, self._fallback_label_intents(label))

        # 存 list 以便 JSON 可序列化和轻量缓存
        self._intent_cache[cache_key] = {k: sorted(v) for k, v in result.items()}
        return result

    def _infer_label_intents_with_llm(
        self,
        labels: list[str],
        context: str,
    ) -> dict[str, set[str]] | None:
        if not self.client:
            return None

        payload = [
            {"id": f"l{i + 1}", "text": text}
            for i, text in enumerate(labels[:40])  # 控制成本
        ]
        if not payload:
            return {}

        system_prompt = (
            "You classify browser UI label intents for job application automation. "
            "Return strict JSON only."
        )
        user_prompt = (
            "Classify each UI label into zero or more intents.\n"
            "Allowed intents:\n"
            "- apply_entry: enter/start job application\n"
            "- login_action: sign in/authenticate/account access\n"
            "- progression_action: next/continue/review/submit/proceed steps\n"
            "- upload_request: upload/attach file or resume\n"
            "Rules:\n"
            "1) Use semantic meaning, not literal keyword matching.\n"
            "2) Support variants and other languages.\n"
            "3) Be conservative; if uncertain, return empty intents for that label.\n"
            f"Page context (may help): {context[:600]}\n"
            f"Labels JSON:\n{json.dumps(payload, ensure_ascii=False)}\n"
            'Return JSON: {"items":[{"id":"l1","intents":["apply_entry"]}]}'
        )
        try:
            completion = self.client.chat.completions.create(
                model=self.intent_model,
                temperature=0.0,
                max_tokens=500,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw = completion.choices[0].message.content or ""
            data = self._safe_parse_json(raw)
            if not data or not isinstance(data.get("items"), list):
                return None
            id_to_text = {item["id"]: item["text"] for item in payload}
            allowed = {
                "apply_entry",
                "login_action",
                "progression_action",
                "upload_request",
            }
            out: dict[str, set[str]] = {v: set() for v in id_to_text.values()}
            for item in data["items"]:
                if not isinstance(item, dict):
                    continue
                label_id = str(item.get("id", "")).strip()
                text = id_to_text.get(label_id)
                if not text:
                    continue
                intents = item.get("intents", [])
                if not isinstance(intents, list):
                    continue
                normalized = {str(x).strip() for x in intents}
                out[text].update(i for i in normalized if i in allowed)
            return out
        except Exception:
            return None

    def _infer_text_intents(self, text: str, limit: int = 1200) -> set[str]:
        """
        对整页文本做语义意图分类（低频、可缓存）。
        只输出少量全局意图。
        """
        snippet = (text or "").strip()
        if not snippet:
            return set()
        snippet = snippet[:limit]
        cache_key = f"text::{hashlib.sha1(snippet.encode('utf-8')).hexdigest()}"
        cached = self._intent_cache.get(cache_key)
        if cached is not None:
            intents = cached.get("__text__", [])
            return set(intents)

        intents: set[str] = set()
        if self.client:
            try:
                completion = self.client.chat.completions.create(
                    model=self.intent_model,
                    temperature=0.0,
                    max_tokens=220,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Classify page text intents for job application flow. "
                                "Return strict JSON."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "Allowed intents: login_action, upload_request.\n"
                                "Use semantic meaning and multilingual understanding.\n"
                                f"Text:\n{snippet}\n"
                                'Return JSON: {"intents":["login_action"]}'
                            ),
                        },
                    ],
                )
                raw = completion.choices[0].message.content or ""
                data = self._safe_parse_json(raw)
                if data and isinstance(data.get("intents"), list):
                    allowed = {"login_action", "upload_request"}
                    intents = {
                        str(x).strip()
                        for x in data["intents"]
                        if str(x).strip() in allowed
                    }
            except Exception:
                intents = set()

        # LLM 不可用/失败时回退到强共识词（兜底）
        if not intents:
            lower = snippet.lower()
            if any(k in lower for k in ["upload", "attach", "resume", "cv"]):
                intents.add("upload_request")
            if any(k in lower for k in ["sign in", "log in", "login"]):
                intents.add("login_action")

        self._intent_cache[cache_key] = {"__text__": sorted(intents)}
        return intents

    def _fallback_label_intents(self, label: str) -> set[str]:
        """当语义模型不可用时，使用极小硬规则集合兜底。"""
        text = (label or "").strip().lower()
        intents: set[str] = set()
        if not text:
            return intents

        # 强共识短词，仅做兜底，不作为主策略
        if any(k in text for k in ["apply", "application", "candidature"]):
            intents.add("apply_entry")
            intents.add("progression_action")
        if any(k in text for k in ["next", "continue", "submit", "proceed", "review"]):
            intents.add("progression_action")
        if any(k in text for k in ["sign in", "log in", "login", "authenticate"]):
            intents.add("login_action")
        if any(k in text for k in ["upload", "attach", "resume", "cv", "file"]):
            intents.add("upload_request")
        return intents

    def _intent_cache_key(self, labels: list[str], context: str = "") -> str:
        stable = "\n".join(sorted(labels))
        base = f"{stable}\n--ctx--\n{context[:600]}"
        return f"labels::{hashlib.sha1(base.encode('utf-8')).hexdigest()}"

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
        try:
            if action.action == "click":
                if item.role in ("checkbox", "radio"):
                    return locator.is_checked()
                if self._is_answer_click_action(action, item=item):
                    if action.target_question:
                        expected = self._normalize_answer_label(
                            action.selector or item.name
                        )
                        return self._verify_question_answer_state(
                            action.target_question, expected
                        )
                    return False
                return True
            if action.action in ("fill", "type", "select"):
                if action.value is None:
                    return True
                current = self._get_input_value(locator)
                target = str(action.value).strip()
                if target and target in (current or ""):
                    return True
                if action.action == "type" and item.role in ("combobox", "textbox"):
                    return self._is_dropdown_open(locator)
                return False
            if action.action == "upload":
                # upload 的 value 可能是文件名或完整路径；由 _verify_upload_success 统一确认
                if action.value:
                    ordered = resolve_upload_candidate(
                        action.value, self.upload_candidates
                    )
                    if ordered:
                        return self._verify_upload_success(ordered[0])
                return False
        except Exception:
            return False
        return True

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
                    return True
                except Exception:
                    return False
        except Exception:
            return False
        return False

    def _get_input_value(self, locator) -> str:
        """尽力获取输入框当前值。"""
        try:
            return locator.input_value(timeout=500)
        except Exception:
            try:
                return locator.evaluate("(el) => el.value || el.textContent || ''")
            except Exception:
                return ""

    def _is_dropdown_open(self, locator) -> bool:
        """检测 autocomplete 下拉是否打开（aria-expanded）。"""
        try:
            expanded = locator.get_attribute("aria-expanded")
            return str(expanded).lower() == "true"
        except Exception:
            return False

    def _normalize_answer_label(self, text: str | None) -> str:
        normalized = (text or "").strip().lower()
        if normalized in ("yes", "y"):
            return "yes"
        if normalized in ("no", "n"):
            return "no"
        return ""

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

    def _try_answer_binding_click(self, action: AgentAction) -> bool | None:
        """
        对同名 Yes/No 优先执行“问题绑定点击”。
        返回：
        - True：绑定点击成功且后验通过
        - False：绑定点击已执行但后验失败
        - None：不适用或定位失败，回退原有点击路径
        """
        answer = self._normalize_answer_label(action.selector)
        question = (action.target_question or "").strip()
        if not answer or not question:
            return None
        payload = self._click_answer_with_question_binding(question, answer)
        self._step_log(
            "answer_binding_attempt",
            {
                "step": self.step_count,
                "classification": "validation_error",
                "reason_code": "answer_binding",
                "evidence_snippet": str(payload.get("reason", ""))[:220],
                "question": question,
                "answer": answer,
                "ok": bool(payload.get("ok", False)),
                "reason": payload.get("reason", ""),
            },
        )
        if not bool(payload.get("ok", False)):
            return None
        verified = self._verify_question_answer_state(question, answer)
        return bool(verified)

    def _click_answer_with_question_binding(
        self, question: str, answer: str
    ) -> dict[str, str | bool]:
        """
        在包含问题文本的容器内点击指定答案（yes/no）。
        """
        try:
            result = self.page.evaluate(
                """
                ({ question, answer }) => {
                  const norm = (v) => String(v || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const q = norm(question);
                  const a = norm(answer);
                  if (!q || !a) return { ok: false, reason: "missing_question_or_answer" };
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
                  const answerNodes = Array.from(
                    document.querySelectorAll("button, [role='button'], label, input[type='radio'], input[type='checkbox']")
                  ).filter((el) => isVisible(el));
                  const answerCandidates = answerNodes.filter((el) => {
                    const t = textOf(el);
                    return t === a || t.startsWith(a + " ");
                  });
                  if (!answerCandidates.length) {
                    return { ok: false, reason: "answer_candidates_not_found" };
                  }
                  const containerHints = ["fieldset", "[role='group']", "[role='radiogroup']", "form", ".question", ".application-question", "section", "li", "div"];
                  let best = null;
                  let bestScore = -1;
                  for (const candidate of answerCandidates) {
                    let cur = candidate;
                    let depth = 0;
                    while (cur && depth < 8) {
                      const scoreText = textOf(cur);
                      if (scoreText.includes(q)) {
                        const score = 100 - depth;
                        if (score > bestScore) {
                          bestScore = score;
                          best = candidate;
                        }
                        break;
                      }
                      let next = null;
                      for (const sel of containerHints) {
                        const found = cur.closest(sel);
                        if (found && found !== cur) {
                          next = found.parentElement;
                          break;
                        }
                      }
                      cur = next || cur.parentElement;
                      depth += 1;
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
                {"question": question, "answer": answer},
            )
        except Exception as e:
            return {"ok": False, "reason": f"binding_eval_error:{type(e).__name__}"}
        if isinstance(result, dict):
            return {
                "ok": bool(result.get("ok", False)),
                "reason": str(result.get("reason", ""))[:120],
            }
        return {"ok": False, "reason": "binding_eval_unexpected_payload"}

    def _verify_question_answer_state(
        self, question: str, expected_answer: str
    ) -> bool:
        """
        校验目标问题的答案是否已落在预期选项上。
        """
        if not question or expected_answer not in ("yes", "no"):
            return False
        try:
            result = self.page.evaluate(
                """
                ({ question, expected }) => {
                  const norm = (v) => String(v || "").toLowerCase().replace(/\\s+/g, " ").trim();
                  const q = norm(question);
                  const expectedNorm = norm(expected);
                  if (!q) return { matched: false, selected: [] };
                  const isVisible = (el) => {
                    if (!el) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === "none" || st.visibility === "hidden") return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                  };
                  const textOf = (el) => norm(el?.innerText || el?.textContent || el?.getAttribute("aria-label") || "");
                  const selected = [];
                  let matched = false;
                  const scopes = Array.from(document.querySelectorAll("fieldset,[role='group'],[role='radiogroup'],form,section,li,div"))
                    .filter((el) => isVisible(el) && textOf(el).includes(q));
                  for (const scope of scopes.slice(0, 12)) {
                    matched = true;
                    const checkedInputs = Array.from(scope.querySelectorAll("input[type='radio']:checked,input[type='checkbox']:checked"));
                    for (const el of checkedInputs) {
                      const label = textOf(el.closest("label")) || textOf(el);
                      if (label) selected.push(label);
                    }
                    const pressed = Array.from(scope.querySelectorAll("button,[role='button']"))
                      .filter((el) => {
                        const pressed = String(el.getAttribute("aria-pressed") || "").toLowerCase();
                        const checked = String(el.getAttribute("aria-checked") || "").toLowerCase();
                        const cls = String(el.className || "").toLowerCase();
                        return pressed === "true" || checked === "true" || cls.includes("selected") || cls.includes("active") || cls.includes("checked");
                      });
                    for (const el of pressed) {
                      const t = textOf(el);
                      if (t) selected.push(t);
                    }
                  }
                  const dedup = Array.from(new Set(selected));
                  const ok = dedup.some((s) => s === expectedNorm || s.startsWith(expectedNorm + " "));
                  return { matched, selected: dedup, ok };
                }
                """,
                {"question": question, "expected": expected_answer},
            )
        except Exception:
            return False
        if not isinstance(result, dict):
            return False
        return bool(result.get("matched")) and bool(result.get("ok"))

    def _classify_submission_outcome(
        self, action: AgentAction, action_success: bool
    ) -> SubmissionOutcome:
        evidence = self._extract_outcome_text_evidence()
        lower = evidence.lower()
        if self._looks_like_completion_text(lower):
            return SubmissionOutcome(
                classification="success_confirmed",
                reason_code="completion_detected",
                evidence_snippet=evidence[:220],
            )
        if any(
            k in lower
            for k in [
                "flagged as possible spam",
                "suspicious activity",
                "anti-spam",
                "risk",
                "rate limit",
                "too many requests",
                "try again later",
            ]
        ):
            return SubmissionOutcome(
                classification="external_blocked",
                reason_code="anti_spam_or_risk_blocked",
                evidence_snippet=evidence[:220],
            )
        if any(
            k in lower
            for k in [
                "network error",
                "temporarily unavailable",
                "timeout",
                "timed out",
                "connection error",
                "server error",
                "5xx",
            ]
        ):
            return SubmissionOutcome(
                classification="transient_network",
                reason_code="network_or_server_transient",
                evidence_snippet=evidence[:220],
            )
        block_reason = self._get_progression_block_reason()
        if block_reason:
            snippets = " | ".join(self._last_progression_block_snippets[:2])
            snippet = snippets or block_reason
            return SubmissionOutcome(
                classification="validation_error",
                reason_code="missing_required_field",
                evidence_snippet=snippet[:220],
            )
        if action_success:
            return SubmissionOutcome(
                classification="unknown_blocked",
                reason_code="submit_clicked_without_confirmed_transition",
                evidence_snippet=evidence[:220],
            )
        return SubmissionOutcome(
            classification="unknown_blocked",
            reason_code="submit_action_failed",
            evidence_snippet=evidence[:220],
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
        self._sync_failure_hints(outcome)
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
            if retry_count >= self._submission_retry_limit:
                return False, True
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

    def _sync_failure_hints(self, outcome: SubmissionOutcome) -> None:
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

    def _build_submission_manual_reason(self, action: AgentAction) -> str:
        outcome = self._last_submission_outcome
        if not outcome:
            return "提交连续失败达到重试上限，需要人工处理"
        return (
            "提交连续受阻达到重试上限；"
            f"classification={outcome.classification}; "
            f"code={outcome.reason_code}; "
            f"action={action.action}:{action.selector or action.ref or 'unknown'}; "
            f"evidence={outcome.evidence_snippet[:160]}"
        )

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

    def _normalized_action_intent(self, action: AgentAction) -> str | None:
        if action.action != "click":
            return None
        label = self._normalize_answer_label(action.selector)
        if not label and action.ref:
            item = self._last_snapshot_map.get(action.ref)
            if item:
                label = self._normalize_answer_label(item.name)
        if label in ("yes", "no"):
            question = (action.target_question or "").strip().lower()
            return f"answer::{question or 'unknown'}::{label}"
        source_item = self._last_snapshot_map.get(action.ref or "")
        if self._is_progression_action(action, item=source_item):
            return "progression::submit_apply"
        return None

    def _stable_page_scope(self) -> str:
        try:
            current = self.page.url or ""
        except Exception:
            current = ""
        parsed = urlsplit(current)
        domain = (parsed.netloc or "unknown").lower()
        path = (parsed.path or "/").lower()
        stable_parts = [p for p in path.split("/") if p and p not in {"jobs", "job"}]
        normalized_path = "/" + "/".join(stable_parts[:3]) if stable_parts else "/"
        return f"{domain}{normalized_path}"

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
        decision = "none"
        if fail_count == 1:
            decision = "replan"
        elif fail_count == 2:
            decision = "alternate"
        elif fail_count >= 3:
            decision = "stop"
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
        if success:
            self._action_fail_counts[key] = 0
            self._repeated_skip_counts[key] = 0
            if semantic_key:
                self._semantic_fail_counts[semantic_key] = 0
            return
        self._action_fail_counts[key] = self._action_fail_counts.get(key, 0) + 1
        if semantic_key:
            self._semantic_fail_counts[semantic_key] = (
                self._semantic_fail_counts.get(semantic_key, 0) + 1
            )

    def _sanitize_simplify_claims(self, text: str | None) -> str | None:
        if not text:
            return text
        lowered = text.lower()
        if "simplify" not in lowered:
            return text
        claim_markers = [
            "已自动填写",
            "自动填写完成",
            "simplify 已",
            "simplify已",
            "autofill complete",
            "autofilled",
        ]
        if any(marker in lowered for marker in claim_markers):
            return text.replace("Simplify", "页面").replace("simplify", "页面")
        return text

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
        """
        智能点击：根据元素类型选择最佳策略。
        支持模糊匹配和滚动重试。
        """
        if not selector:
            return False

        timeout = 1000  # 每个策略 1 秒
        check_timeout = 200  # 可见性检查 200ms

        # 清理 selector：去除重复词（如 "Dallas Dallas" → "Dallas"）
        words = selector.split()
        seen = set()
        unique_words = []
        for w in words:
            if w.lower() not in seen:
                seen.add(w.lower())
                unique_words.append(w)
        clean_selector = " ".join(unique_words)

        # 从复杂 selector 中提取简短关键词（如 "Yes"、"No"）
        short_selector = clean_selector
        if " " in clean_selector and len(clean_selector) > 20:
            # 如果 selector 很长，尝试取最后一个词（通常是 Yes/No）
            if unique_words[-1] in ["Yes", "No", "yes", "no"]:
                short_selector = unique_words[-1]

        # 提取第一个关键词用于模糊匹配（如 "Dallas" 匹配 "Dallas, TX"）
        first_word = unique_words[0] if unique_words else clean_selector

        # 根据元素类型选择策略
        if element_type == "button":
            strategies = [
                lambda: self.page.get_by_role("button", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
            ]
        elif element_type == "link":
            strategies = [
                lambda: self.page.get_by_role("link", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
            ]
        elif element_type in ("checkbox", "radio"):
            # checkbox/radio 支持多种匹配方式
            strategies = [
                # 1. 直接点击短文本（Yes/No）
                lambda: self.page.get_by_role("button", name=short_selector).first,
                lambda: self.page.get_by_text(short_selector, exact=True).first,
                # 2. 尝试 radio/checkbox 角色
                lambda: self.page.get_by_role(element_type, name=short_selector).first,
                lambda: self.page.get_by_label(short_selector).first,
                # 3. 用清理后的 selector
                lambda: self.page.get_by_text(clean_selector, exact=True).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
                # 4. 模糊匹配：用第一个词（如 Dallas 匹配 "Dallas, TX"）
                lambda: self.page.get_by_text(first_word, exact=False).first,
                lambda: self.page.get_by_label(first_word, exact=False).first,
                # 5. CSS 选择器模糊匹配
                lambda: self.page.locator(f"label:has-text('{first_word}')").first,
                lambda: self.page.locator(f"[data-testid*='{first_word}' i]").first,
            ]
        elif element_type == "option":
            strategies = [
                lambda: self.page.get_by_role("option", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=True).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
                lambda: self.page.locator(f"li:has-text('{clean_selector}')").first,
                # 模糊匹配
                lambda: self.page.get_by_role("option", name=first_word).first,
                lambda: self.page.get_by_text(first_word, exact=False).first,
                lambda: self.page.locator(f"li:has-text('{first_word}')").first,
            ]
        else:
            strategies = [
                lambda: self.page.get_by_text(clean_selector, exact=True).first,
                lambda: self.page.get_by_role("button", name=clean_selector).first,
                lambda: self.page.get_by_text(clean_selector, exact=False).first,
                # 模糊匹配
                lambda: self.page.get_by_text(first_word, exact=False).first,
            ]

        # 尝试点击（带滚动重试）
        max_scroll_attempts = 2
        for scroll_attempt in range(max_scroll_attempts + 1):
            for strategy in strategies:
                try:
                    locator = strategy()
                    if locator and locator.is_visible(timeout=check_timeout):
                        locator.click(timeout=timeout)
                        return True
                except Exception:
                    continue

            # 如果所有策略都失败，尝试滚动页面后重试
            if scroll_attempt < max_scroll_attempts:
                try:
                    self.page.evaluate("window.scrollBy(0, 300)")
                    self.page.wait_for_timeout(300)
                    self._log(
                        f"   🔄 滚动页面，重试定位 ({scroll_attempt + 1}/{max_scroll_attempts})"
                    )
                except Exception:
                    break

        return False

    def _smart_fill(self, selector: str, value: str) -> bool:
        """智能填写：通过 label 精确定位输入框并填写"""
        if not selector or value is None:
            return False

        timeout = 1500
        value_str = str(value)
        clean_selector = selector.replace("*", "").strip()

        # 只用基于 label 的精确定位，避免误定位到其他输入框
        strategies = [
            lambda: self.page.get_by_label(selector, exact=False).first,
            lambda: self.page.get_by_label(clean_selector, exact=False).first,
            lambda: self.page.get_by_role("textbox", name=selector).first,
            lambda: self.page.get_by_role("textbox", name=clean_selector).first,
            # 通过 label 文本找相邻输入框
            lambda: (
                self.page.locator(f"label:has-text('{clean_selector}')")
                .locator("..")
                .locator("input")
                .first
            ),
        ]
        # 注意：不要用 get_by_placeholder 这种宽泛匹配，容易定位到错误字段

        for strategy in strategies:
            try:
                locator = strategy()
                if locator.is_visible(timeout=200):
                    locator.fill(value_str, timeout=timeout)
                    return True
            except Exception:
                continue

        return False

    def _smart_type(self, selector: str, value: str) -> bool:
        """
        智能输入：逐字输入触发 autocomplete 下拉框。

        处理各种 autocomplete 输入框：
        - 带 * 的 label（如 "Location*"）
        - placeholder 提示（如 "Start typing..."）
        - combobox 类型的输入框
        """
        if not selector or value is None:
            return False

        # 清理 selector（去掉可能的 * 和多余空格）
        clean_selector = selector.replace("*", "").strip()

        # 快速找到输入框 - 优先使用 label 匹配，避免误定位
        input_elem = None
        strategies = [
            # 1. 精确 label 匹配（最可靠）
            lambda: self.page.get_by_label(selector, exact=False).first,
            # 2. 清理后的 label 匹配
            lambda: self.page.get_by_label(clean_selector, exact=False).first,
            # 3. combobox 角色（autocomplete 通常是 combobox）
            lambda: self.page.get_by_role("combobox", name=selector).first,
            lambda: self.page.get_by_role("combobox", name=clean_selector).first,
            # 4. textbox 角色
            lambda: self.page.get_by_role("textbox", name=selector).first,
            lambda: self.page.get_by_role("textbox", name=clean_selector).first,
            # 5. 通过包含 selector 文本的 label 元素找相邻输入框
            lambda: (
                self.page.locator(f"label:has-text('{clean_selector}')")
                .locator("..")
                .locator("input, [role='combobox']")
                .first
            ),
            # 6. 直接通过 aria-label
            lambda: self.page.locator(f"[aria-label*='{clean_selector}' i]").first,
        ]
        # 注意：不要用 get_by_placeholder("type") 这种宽泛匹配，容易定位到错误字段

        for strategy in strategies:
            try:
                elem = strategy()
                if elem.is_visible(timeout=300):
                    input_elem = elem
                    self._log(f"   📍 定位成功: {selector}")
                    break
            except Exception:
                continue

        if not input_elem:
            self._log(f"   ⚠️ 无法定位输入框: {selector}", "warn")
            return False

        try:
            # 1. 点击激活输入框
            input_elem.click(timeout=800)
            self.page.wait_for_timeout(100)

            # 2. 清空现有内容（全选后删除，更可靠）
            input_elem.press("Control+a")
            self.page.wait_for_timeout(30)
            input_elem.press("Backspace")
            self.page.wait_for_timeout(50)

            # 3. 逐字输入，触发 autocomplete
            input_elem.type(str(value), delay=40)  # 逐字输入触发下拉

            # 4. 短暂等待让下拉框出现（主循环会截图让 AI 看到变化）
            self.page.wait_for_timeout(600)
            return True
        except Exception as e:
            self._log(f"   ⚠️ 输入失败: {e}", "warn")
            return False

    def _do_select(self, selector: str, value: str) -> bool:
        """
        选择下拉框选项（仅处理原生 <select>）。
        对于非原生下拉框，AI 应该使用 type + click 组合。
        """
        if not selector or not value:
            return False

        # 只尝试原生 select，其他情况让 AI 用 type + click
        try:
            select = self.page.get_by_label(selector).first
            if select.is_visible(timeout=500):
                select.select_option(label=value, timeout=2000)
                return True
        except Exception:
            pass

        # 尝试直接点击已显示的选项（下拉框可能已经打开）
        try:
            option = self.page.get_by_role("option", name=value).first
            if option.is_visible(timeout=300):
                option.click(timeout=1500)
                return True
        except Exception:
            pass

        return False

    def _do_upload(self, action: AgentAction, locator=None) -> bool:
        """
        执行可控文件上传：
        - 必须先检测到上传信号
        - 仅允许白名单目录内文件
        - 上传失败可重试并尝试候选文件回退
        """
        if not self._last_upload_signals:
            self._log("⚠ 页面无上传信号，跳过 upload 动作", "warn")
            return False

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
        try:
            file_inputs = self.page.locator("input[type='file']")
            if file_inputs.count() > 0:
                return file_inputs.first
        except Exception:
            pass

        if selector:
            # 有些页面需要先点“Upload/Attach”按钮再出现 file input
            self._smart_click(selector, element_type="button")
            self.page.wait_for_timeout(300)
            try:
                file_inputs = self.page.locator("input[type='file']")
                if file_inputs.count() > 0:
                    return file_inputs.first
            except Exception:
                pass
        return None

    def _verify_upload_success(self, file_path: str) -> bool:
        """
        上传成功确认（多信号）：
        - input.files 非空且文件名匹配
        - 或页面文本出现文件名
        """
        filename = Path(file_path).name

        try:
            count = self.page.locator("input[type='file']").count()
        except Exception:
            count = 0

        for i in range(count):
            try:
                locator = self.page.locator("input[type='file']").nth(i)
                ok = locator.evaluate(
                    "(el, expected) => (el.files && el.files.length > 0 && el.files[0].name === expected)",
                    filename,
                )
                if ok:
                    return True
            except Exception:
                continue

        try:
            body_text = self.page.inner_text("body")
            if filename in body_text:
                return True
        except Exception:
            pass

        return False

    def _do_scroll(self, direction: str) -> bool:
        """滚动页面"""
        try:
            if "down" in direction.lower():
                self.page.evaluate("window.scrollBy(0, 500)")
            else:
                self.page.evaluate("window.scrollBy(0, -500)")
            return True
        except Exception:
            return False

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
            # 刷新后清理缓存，避免沿用旧页面动作计划。
            self._state_cache_by_fingerprint.clear()
            self._action_fail_counts.clear()
            self._action_cache_use_counts.clear()
            self._repeated_skip_counts.clear()
            self._semantic_fail_counts.clear()
            self._error_gate_cache.clear()
            self._last_observed_fingerprint = ""
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

    def _looks_like_completion_text(self, lower_text: str) -> bool:
        success_indicators = [
            "thank you for applying",
            "thanks for your application",
            "application submitted",
            "application received",
            "successfully submitted",
            "your application has been submitted",
            "application complete",
            "thanks for submitting",
        ]
        return any(token in lower_text for token in success_indicators)

    def _verify_completion(self) -> tuple[bool, str]:
        """
        二次验证：检查页面是否真的完成了申请。

        关键逻辑：
        1. 如果 Submit 按钮仍可见且没有成功消息 → 表单未提交
        2. 排除浏览器扩展消息（如 "Autofill complete!"）的干扰
        3. 必须有明确的成功消息才算完成

        返回:
            tuple[bool, str]: (是否真的完成, 验证信息)
        """
        try:
            # 获取页面文本
            body_text = self.page.inner_text("body").lower()

            # 1. 检查是否有真正的成功标志（必须是网站返回的，不是扩展）
            success_indicators = [
                "thank you for applying",
                "thanks for your application",
                "application submitted",
                "application received",
                "successfully submitted",
                "we have received your application",
                "your application has been submitted",
                "application complete",
                "thanks for submitting",
                "we'll be in touch",
                "we will review your application",
            ]

            has_success = any(
                indicator in body_text for indicator in success_indicators
            )

            # 2. 排除浏览器扩展的误报消息
            extension_false_positives = [
                "autofill complete",
                "simplify",
                "extension",
                "chrome extension",
            ]

            # 如果页面只有扩展相关的"成功"消息，不算真正成功
            if not has_success:
                for fp in extension_false_positives:
                    if fp in body_text and "complete" in body_text:
                        self._log(f"   ⚠ 检测到扩展消息 '{fp}'，不是真正的申请成功")

            # 3. 检查是否有错误标志
            error_indicators = [
                "this field is required",
                "please fill",
                "is required",
                "missing required",
                "please complete",
                "invalid",
            ]

            has_error = False
            for indicator in error_indicators:
                if indicator in body_text:
                    has_error = True
                    break

            # 4. 检查是否还有 Submit 按钮可见（关键检查！）
            has_submit_button = False
            submit_button_checks = [
                ("button", "Submit"),
                ("button", "Submit Application"),
                ("button", "Apply"),
                ("button", "Submit your application"),
            ]

            for role, name in submit_button_checks:
                try:
                    submit_btn = self.page.get_by_role(role, name=name).first
                    if submit_btn.is_visible(timeout=300):
                        has_submit_button = True
                        self._log(f"   🔍 检测到 Submit 按钮仍可见: '{name}'")
                        break
                except Exception:
                    continue

            # 也检查文本匹配
            if not has_submit_button:
                try:
                    submit_text = self.page.get_by_text(
                        "Submit Application", exact=False
                    ).first
                    if submit_text.is_visible(timeout=300):
                        has_submit_button = True
                        self._log("   🔍 检测到 Submit Application 文本仍可见")
                except Exception:
                    pass

            # 5. 综合判断
            # 关键规则：如果 Submit 按钮仍可见且没有成功消息，表单肯定未提交
            if has_submit_button and not has_success:
                return False, "Submit 按钮仍可见，表单尚未提交"

            if has_error:
                return False, "页面仍有错误提示，表单未完成"

            if has_success and not has_error:
                return True, "页面显示申请成功信息，无错误提示"

            # 如果没有成功标志也没有 Submit 按钮，可能是跳转到了其他页面
            if not has_success and not has_submit_button:
                # 保守判断，可能需要继续观察
                return False, "未检测到明确的成功信息，可能需要继续"

            return False, "状态不确定，继续执行"

        except Exception as e:
            self._log(f"⚠ 二次验证出错: {e}", "warn")
            # 验证出错时，保守返回 False
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
        """安全解析 JSON"""
        # 直接解析
        try:
            return json.loads(raw)
        except Exception:
            pass

        # 从 markdown 代码块提取
        if "```" in raw:
            try:
                start = raw.find("```json")
                if start != -1:
                    start = raw.find("\n", start) + 1
                else:
                    start = raw.find("```") + 3
                    start = raw.find("\n", start) + 1
                end = raw.find("```", start)
                if end != -1:
                    return json.loads(raw[start:end].strip())
            except Exception:
                pass

        # 提取 { ... }
        if "{" in raw and "}" in raw:
            try:
                start = raw.index("{")
                end = raw.rfind("}") + 1
                return json.loads(raw[start:end])
            except Exception:
                pass

        return None

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
