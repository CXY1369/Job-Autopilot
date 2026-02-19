# V2.1 Architecture Blueprint

## Document Metadata

- `project`: `Job Autopilot - Auto Application Agent`
- `version`: `v2.1-blueprint`
- `date_utc`: `2026-02-19`
- `status`: `Approved for implementation`
- `owner`: `Agent + Human co-design`

## 1) Why V2.1

当前系统已具备可用闭环，但在跨站点稳定性上仍有两类系统性短板：

1. 提交阻断（anti-spam / risk flagged / transient failure）未能稳定止损，可能重复尝试。
2. 复杂表单（如重复 Yes/No）存在“语义定位成功但交互未真正生效”的错觉。

V2.1 的目标不是修单站点，而是建立可迁移的通用执行范式：

- 语义优先（DOM/ARIA/结构化状态）
- 视觉兜底（仅关键节点截图推理）
- 强后验（动作必须有状态变化证据）
- 有限状态机（可预期升级路径，避免盲重试）

## 2) Design Principles

1. `Semantic first`: 默认依赖结构化页面快照，不依赖逐步全屏视觉推理。
2. `Evidence-driven action`: 每步动作都必须有成功证据；无证据即失败。
3. `Bounded retries`: 可重试场景有明确上限；超限转人工并附结构化原因。
4. `Site-agnostic`: 不引入站点专属硬编码脚本作为主路径。
5. `Composable assist`: Simplify 作为可选外挂辅助，不是主流程依赖。
6. `Full observability`: 终端叙事 + NDJSON 结构化事件双轨记录。

## 3) System Layers

### 3.1 Browser Layer

- 技术：`Playwright + CDP`。
- 能力：页面导航、元素交互、A11y Tree、Network/Console 事件、Storage 访问。
- 执行风格：支持“虚拟光标式执行节奏”（hover/focus/scroll/typed input pacing），用于拟人稳定性，而不是作为主要感知源。

### 3.2 Semantic Perception Layer

统一产出 `SemanticSnapshot`：

- `page`: url/title/domain/path/hash
- `elements`: 交互节点（button/input/select/link/checkbox/radio）
- `groups`: 字段组/fieldset/radiogroup/question container
- `errors`: 错误提示（可见性、关联字段、文本）
- `required_unfilled`: 必填缺失字段签名
- `submit_candidates`: 可提交按钮及可用态

元素引用使用短生命周期 `ref_id`（页面结构变化后必须重建）。

### 3.3 Planning Layer

输入：

- 用户目标（申请岗位）
- FSM 当前状态
- SemanticSnapshot
- 历史失败证据（分类、错误摘要、熔断状态）

输出：`ActionPlan`（结构化动作 DSL），例如：

- `click_ref`
- `type_ref`
- `select_ref`
- `set_checkbox`
- `set_radio`
- `upload_file`
- `submit`
- `wait_for`
- `refresh_soft`

### 3.4 Assist Layer (Simplify Optional)

定位：外挂加速器，机会式调用。

- 触发时机：初次进入申请页且必填空值占比高。
- 成功判定：字段填充率提升，而不是“按钮点击成功”。
- 重试策略：最多 `1-2` 次，失败即降级回主流程。
- 约束：不得绑定插件 UI 结构作为关键路径。

### 3.5 Execution + Verification Layer

执行后立即验证（至少命中一项）：

- 目标元素状态变化（checked/selected/value）
- 关联错误提示减少
- 提交按钮状态变化（disabled->enabled）
- URL/路由状态变化

若都未变化，动作判定为失败，进入重规划或升级。

### 3.6 Outcome Classifier Layer

提交后统一分类：

- `success_confirmed`
- `validation_error`
- `external_blocked`
- `transient_network`
- `unknown_blocked`

输出结构：`class`, `code`, `confidence`, `evidence_snippet`, `retryable`。

### 3.7 FSM Orchestrator Layer

主状态：

1. `DISCOVER`
2. `ASSIST_PREFILL` (optional)
3. `VERIFY_PREFILL`
4. `FILL`
5. `VALIDATE`
6. `SUBMIT`
7. `VERIFY_OUTCOME`
8. `DONE | MANUAL_REQUIRED`

固定升级路径（语义熔断）：

- `fail=1 -> replan`
- `fail=2 -> alternate`
- `fail>=3 -> stop_to_manual`

语义键：

- `semantic_key = domain + normalized_path + intent + question_signature`

不再包含易抖动页面指纹。

### 3.8 Observability Layer

双轨记录：

1. 终端叙事（人类可读）
- 页面摘要
- 状态分析
- 候选动作序列
- 下一步决策
- 执行结果与证据

2. NDJSON 结构化事件（机器可分析）
- `snapshot_generated`
- `plan_proposed`
- `action_executed`
- `action_verified`
- `submission_outcome_classified`
- `retry_policy_applied`
- `semantic_loop_guard`
- `assist_invoked`
- `assist_effect_evaluated`

## 4) Interface Contracts (Python)

```python
from dataclasses import dataclass, field
from typing import Optional, Literal

OutcomeClass = Literal[
    "success_confirmed",
    "validation_error",
    "external_blocked",
    "transient_network",
    "unknown_blocked",
]

@dataclass
class SemanticElement:
    ref_id: str
    role: str
    name: str
    label: Optional[str] = None
    value: Optional[str] = None
    required: bool = False
    disabled: bool = False
    checked: Optional[bool] = None
    visible: bool = True
    group_signature: Optional[str] = None

@dataclass
class SemanticSnapshot:
    page_id: str
    url: str
    domain: str
    normalized_path: str
    title: str
    elements: list[SemanticElement] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    required_unfilled: list[str] = field(default_factory=list)
    submit_candidates: list[str] = field(default_factory=list)

@dataclass
class ActionDSL:
    action: str
    ref_id: Optional[str] = None
    value: Optional[str] = None
    target_question: Optional[str] = None
    intent: Optional[str] = None
    success_criteria: list[str] = field(default_factory=list)
    timeout_ms: int = 6000

@dataclass
class SubmissionOutcome:
    outcome_class: OutcomeClass
    failure_code: Optional[str] = None
    confidence: float = 0.0
    evidence_snippet: Optional[str] = None
    retryable: bool = False
```

## 5) Action Verification Matrix

1. `set_radio/set_checkbox`
- 验证：同组目标项 `checked=true`，且冲突项取消。
- 若绑定问题存在，校验对应问题块错误提示下降。

2. `type_ref`
- 验证：value 非空且与目标值匹配（允许归一化）。

3. `upload_file`
- 验证：文件名展示或 input files count > 0。

4. `submit`
- 验证：进入 `VERIFY_OUTCOME`，必须产出 `SubmissionOutcome`。

## 6) Retry and Stop Policy

1. `validation_error`
- 禁止直接重复 submit。
- 必须优先修复 `required_unfilled/errors`。

2. `external_blocked` / `transient_network`
- 最多 `3` 次有限重试。
- 重试动作：合法的节奏等待、轻滚动、焦点重置、soft refresh 后重新进入提交。
- 第 3 次失败：`manual_required`，写结构化 `manual_reason`。

3. `unknown_blocked`
- 允许一次 replan；仍失败则人工。

## 7) Simplify Integration Policy

1. 默认策略：`assist_mode=opportunistic`。
2. 调用条件：页面为空字段较多且插件可用。
3. 评估指标：
- `filled_required_before`
- `filled_required_after`
- `delta`
4. 生效判定：仅当 `delta > 0` 或 `required_empty == 0`，才认为 prefill verified。
5. 失败处理：
- 标记 `assist_failure_code`
- 不阻塞主流程
6. 风险控制：
- 不把插件面板元素作为核心业务依赖
- 不因插件失败进入无限重试

## 8) Visual Fallback Budget

截图推理仅用于以下节点：

1. 提交前最终校验
2. 提交后结果确认
3. 语义快照冲突（无法判定关键元素）
4. 异常页面（challenge / blank / fatal error）

预算建议：每 job 全流程截图推理调用不超过 `8` 次。

实现开关建议：

- `VISION_FALLBACK_BUDGET`：视觉兜底预算（默认 `8`）
- `STEP_SCREENSHOT_MODE`：`vision_only|always|off`（默认 `vision_only`）

## 9) Storage and API Alignment

与现有字段保持一致并继续使用：

- `failure_class`
- `failure_code`
- `retry_count`
- `last_error_snippet`
- `last_outcome_class`
- `last_outcome_at`

现有 API 继续保留：

- `GET /api/jobs`
- `GET /api/stats/failures`
- `GET /api/jobs/{id}/diagnostics`

新增仅建议（非强制）：

- `GET /api/runs/{id}/trace-summary`（压缩版事件摘要）

## 10) Implementation Roadmap

### Phase A (Stop-loss First)

1. 引入 `SemanticSnapshot` 生产器（从现有 `ui_snapshot + DOM` 统一）。
2. 将执行动作切换为 `ref_id` 主路径，并强化动作后验。
3. 把提交后判定统一接入 `SubmissionOutcomeClassifier`。
4. 将现有熔断逻辑迁移到稳定语义键 FSM。

### Phase B (Productization)

1. 完整终端叙事模板和 NDJSON schema 对齐。
2. Assist Layer（Simplify）策略化接入。
3. UI 看板新增失败聚类 + 策略效果趋势。
4. 回放测试集（基于真实失败日志）常态化。

## 11) Acceptance Criteria (V2.1)

1. 任意站点出现提交阻断时，最多 `3` 次后终止并给出结构化原因。
2. 同语义动作不会长期停留在 `fail_count=1`。
3. Yes/No 等回答动作必须有可验证状态变化，否则不计成功。
4. 终端输出稳定包含：`摘要/分析/计划/决策/结果`。
5. Simplify 不可用时主流程仍能独立执行，不退化为死循环。

## 12) Out-of-Scope / Compliance Boundary

- 不实现验证码绕过
- 不实现浏览器指纹伪装
- 不规避站点安全机制

本蓝图仅做合规自动化与鲁棒性治理。

## 13) External Review Addendum

以第三方工程审查视角，建议在实现阶段同步落实以下约束：

1. `PII-safe logs`
- NDJSON 与终端叙事默认脱敏邮箱、手机号、链接 token，仅在 debug 模式输出原文。

2. `Feature flags`
- 新能力（semantic snapshot、assist、visual fallback 策略）均挂在可回滚开关后上线。

3. `Deterministic replay set`
- 固化失败日志回放集（anti-spam / validation oscillation / duplicated yes-no），每次改动必须回放通过。

4. `Cost guardrails`
- 每个 job 的视觉调用次数、LLM token、总耗时必须有上限并在 UI 可见。

5. `Plugin isolation`
- Simplify 面板区域在快照中降权或隔离，避免污染主页面交互决策。
