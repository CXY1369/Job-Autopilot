# Project Progress Tracking

## Metadata

- `project_name`: `Job Autopilot - Auto Application Agent`
- `last_updated_utc`: `2026-02-19`
- `primary_language`: `Python`
- `runtime_stack`: `FastAPI + Playwright + OpenAI + SQLite`
- `document_purpose`: `维护进度、实现细节与后续规划（AI/人类共读）`

## Executive Snapshot

- 当前状态：`Active / Usable`
- CI 状态：`Green`（`Lint + Core Tests + Full Tests`）
- 自动化测试：`60 passed`
- 架构定位：`AI 决策 + 规则护栏`
- 关键新增能力：`提交结果分类、有限重试状态机、稳定语义熔断键、失败治理字段/API/看板`
- V2.1 蓝图：`docs/V2_ARCHITECTURE_BLUEPRINT.md`（语义优先、视觉兜底、Assist 可选）

## Scope and Goals

### Goal

在用户授权下，自动完成英文求职申请流程，减少重复操作并保留可追溯日志。

### Non-goals (Current)

- 不绕过验证码/登录挑战
- 不做跨站点专用硬编码脚本
- 不执行白名单外文件上传

## Architecture (Current)

### High-Level Flow

1. API 接收岗位，写入 SQLite（`pending`）
2. 调度器取任务 -> `in_progress`
3. 打开页面，尝试 Simplify 自动填充
4. Vision Agent 循环执行：观察 -> LLM 决策 -> 动作执行 -> 后验校验
5. 结束后写回终态：`applied / manual_required / failed`
6. 记录日志、截图、前端可视化状态

### Core Modules

- `autojobagent/core/applier.py`
  - 单岗位流程编排
  - 接入 JD 提取与简历匹配
  - 汇总 Agent 结果并生成 `ApplyResult`
- `autojobagent/core/vision_agent.py`
  - 核心动作循环与 LLM 调用
  - 页面快照、动作验证、失败重试
  - 前进门控与完成态二次验证
  - 新增 `refresh` 动作与“最多两次刷新重试”
- `autojobagent/core/resume_matcher.py`
  - JD 文本提取
  - 多简历匹配（LLM 优先，启发式回退）
- `autojobagent/core/browser_manager.py`
  - Playwright 持久化会话
  - Simplify 扩展加载
- `autojobagent/core/scheduler.py`
  - 状态流转与最终结果持久化

## Implemented Features

### 1) Job Lifecycle and API

- 岗位 CRUD、日志查询、状态过滤
- 调度控制：开始 / 暂停
- 前端列表与状态分组

### 2) AI-Driven Form Operation

- 多模态输入：截图 + 页面文本 + 可交互元素快照
- 动作集合：`click/fill/type/select/upload/scroll/refresh/wait/done/stuck`
- `ref` 优先执行，降低误定位

### 3) Robustness and Safety

- 登录/验证码检测 -> `manual_required`
- 前进门控（必填/错误提示存在时阻止 Next/Submit）
- 上传白名单验证
- 完成态二次验证（避免“误判已提交”）
- 连续失败恢复：卡住自动刷新，最多 2 次
- 刷新耗尽后给出明确人工原因
- 新增同名 `Yes/No` 的问题定向点击（`target_question` 绑定）
- 新增语义重复动作熔断（replan -> alternate -> stop）
- 前进门控新增修复提示联动日志（避免 submit 循环）

### 4) Resume Matching (Stage A)

- 从目标页面提取 JD 文本
- 候选简历扫描（项目目录白名单）
- LLM 打分选择最佳简历，失败时回退启发式
- 记录 `resume_used`、`score`、`reason`
- 前端仅在 `applied` 记录展示匹配结果

### 5) CI and Tests

- GitHub Actions：
  - `Lint (Ruff)`
  - `Core Tests (Fast Gate)`
  - `Full Tests (Regression)`
- 已修复 CI 导入路径问题（`PYTHONPATH`）
- 测试集当前全量通过

## Tech Stack Detail

- Backend: `FastAPI`, `Uvicorn`
- Browser Automation: `Playwright`
- LLM: `OpenAI Chat Completions`（图文输入）
- Storage: `SQLite + SQLAlchemy`
- Frontend: `HTML + Vanilla JS`
- QA: `pytest + ruff + GitHub Actions`

## Data and State Model

### Job Status

- `pending`
- `in_progress`
- `applied`
- `manual_required`
- `failed`

### Key Job Fields

- `resume_used`
- `fail_reason`
- `manual_reason`
- `failure_class`
- `failure_code`
- `retry_count`
- `last_error_snippet`
- `last_outcome_class`
- `last_outcome_at`
- `apply_time`

## Decision Logic Profile (for maintainers)

### AI-Decided

- 每一步“下一动作”由 LLM 基于当前页面状态给出

### Rule-Gated

- 上传是否合法
- 是否允许继续 Next/Submit
- 是否真的提交成功
- 是否触发卡页恢复与终止

## Known Constraints

- CAPTCHA / 登录挑战仍需人工处理
- 页面语言和文案差异过大时，成功判定关键词可能需补充
- 站点极端复杂 DOM 下仍可能进入 `manual_required`

## Operations Checklist

### Before Real-Page Testing

1. `ruff check`
2. `ruff format --check`
3. `pytest --cache-clear`
4. push 到 GitHub，确认 CI 全绿

### Real-Page Smoke Test

1. 用 1-2 个低风险岗位验证
2. 检查状态闭环、日志、截图、原因字段
3. 通过后再放大批量

## Change Log (Milestone-Level)

### Milestone A - Core Agent Baseline

- FastAPI + Scheduler + Vision Agent 主循环建立

### Milestone B - Safety and Stability

- 上传白名单、前进门控、完成态二次验证、模型回退

### Milestone C - Resume Matching Stage A

- JD 提取 + 多简历匹配 + 结果落库与前端展示

### Milestone D - CI and Test Baseline

- 核心测试与全量测试接入，CI 全绿

### Milestone E - Refresh Recovery Enhancement

- 新增 `refresh` 动作
- 连续失败自动刷新重试（最多 2 次）
- 刷新耗尽写入明确 `manual_reason`

### Milestone F - Generalized Failure Governance

- 新增提交结果分类：`success_confirmed/validation_error/external_blocked/transient_network/unknown_blocked`
- `external_blocked/transient_network` 引入最多 3 次重试节奏，超限转人工
- 语义熔断键改为稳定域（`domain + normalized_path + intent + question_signature`）
- DB 增加失败治理字段并在启动时幂等补列（无 Alembic）
- API 新增：`/api/stats/failures`、`/api/jobs/{id}/diagnostics`
- 前端新增失败分类卡片、TopN 原因、失败筛选

### Milestone G - V2.1 Blueprint Baseline

- 输出架构蓝图：`docs/V2_ARCHITECTURE_BLUEPRINT.md`
- 明确“语义优先 + 视觉关键节点兜底”的执行原则
- 保留终端叙事链路（摘要/分析/计划/决策/结果）
- 将 Simplify 定位为 `opportunistic assist`（可选辅助，不是主依赖）

### Milestone H - Phase A Kickoff (Incremental)

- 新增 `SemanticSnapshot` / `SemanticElement` 内部结构并接入 `snapshot_generated` 事件
- 新增标准化步骤事件：`action_executed`、`action_verified`（保留 `action_verify` 兼容）
- 修复语义熔断在 `replan` 分支不升级的问题（新增 guard promote）
- 增加视觉兜底预算决策（`visual_fallback_decision`，默认预算 8）
- 快照层增加 Assist 面板降权隔离（避免 Simplify 侧栏污染主页面决策）
- 回归测试新增覆盖并通过（总计 `54 passed`）

### Milestone I - Diagnostics and Replay Hardening

- `GET /api/jobs/{id}/diagnostics` 新增 `visual_fallback` 统计摘要（used/budget/exhausted/recent decisions）
- `selector` 路径动作执行同样产出 `action_verified`，与 `ref` 路径对齐
- Assist 面板隔离支持环境变量开关：`SNAPSHOT_ASSIST_FILTER_MODE=exclude|off`
- 新增失败回放测试：
  - Stuut anti-spam 场景（3 次内停止并保留结构化原因）
  - Suno Yes/No 语义振荡场景（replan -> alternate -> stop）

### Milestone J - V2 Semantic-First Tightening

- 观察环节调整为“先语义、后按预算截图”，默认不做非必要截图采集
- 新增截图策略开关：`STEP_SCREENSHOT_MODE=vision_only|always|off`（默认 `vision_only`）
- Assist 预填加入字段增量验证（required filled before/after/delta），无效果时自动降级回 Agent 主流程
- 前端新增 Job Diagnostics 面板，可查看视觉预算摘要与最近决策原因
- 回归测试扩展到 `57 passed`

### Milestone K - V2 Module Split Kickoff

- 新增 `autojobagent/core/semantic_perception.py`：
  - `SemanticSnapshot/SemanticElement`
  - 语义快照构建与错误摘要提取
- 新增 `autojobagent/core/verifier.py`：
  - 动作后验校验工具与输入状态读取
- `vision_agent.py` 保留兼容包装并委托新模块，行为不变
- 全量回归验证通过（`57 passed`）

### Milestone L - V2 Module Split (Second Cut)

- 新增 `autojobagent/core/outcome_classifier.py`：
  - `SubmissionOutcome` 结构
  - 提交结果分类与 manual reason 组装
- 新增 `autojobagent/core/loop_guard.py`：
  - 稳定页面 scope 计算
  - 语义熔断决策与 fail_count 推进
  - 动作结果记账
- `vision_agent.py` 的提交分类与语义熔断逻辑改为模块委托，外部行为与测试保持一致
- 全量回归验证通过（`57 passed`）

### Milestone M - V2 Module Split (Third Cut)

- 新增 `autojobagent/core/executor.py`：
  - `smart_click/smart_fill/smart_type/do_select`
  - upload 辅助：`locate_file_input/verify_upload_success`
  - `do_scroll`
- 新增 `autojobagent/core/planner.py`：
  - `safe_parse_json`
  - `sanitize_simplify_claims`
- `vision_agent.py` 的执行细节与解析/清洗逻辑改为模块委托，保留兼容包装
- 全量回归验证通过（`57 passed`）

### Milestone N - V2 Module Split (Fourth Cut)

- 新增 `autojobagent/core/prompt_builder.py`：
  - `build_system_prompt`
  - `build_user_prompt`
- 新增 `autojobagent/core/state_parser.py`：
  - `parse_agent_response_payload`
- `vision_agent.py` 的 prompt 构建与响应解析逻辑改为模块委托
- 主 Agent 继续收敛为编排层，外部行为与测试保持一致
- 全量回归验证通过（`57 passed`）

### Milestone O - V2 Module Split (Fifth Cut)

- 新增 `autojobagent/core/fsm_orchestrator.py`：
  - `decide_semantic_guard_path`
  - `decide_repeated_skip_path`
  - `decide_failure_recovery_path`
- `vision_agent.py` 的主循环关键分支（semantic guard / repeated skip / failure recovery）改为状态机决策函数委托
- 新增 `tests/test_fsm_orchestrator.py` 覆盖状态机决策优先级
- 全量回归验证通过（`60 passed`）

### Milestone P - V2 Module Split (Sixth Cut)

- 新增 `autojobagent/core/llm_runtime.py`：
  - `run_chat_with_fallback`
  - `LLMCallResult`
- `vision_agent.py` 的模型回退/限流/能力不匹配处理改为模块委托
- 新增 `tests/test_llm_runtime.py`，覆盖：
  - 限流触发模型切换
  - 普通错误快速失败
  - 能力不匹配耗尽后终止
- 全量回归验证通过（`63 passed`）

### Milestone Q - V2 Module Split (Seventh Cut)

- 新增 `autojobagent/core/intent_engine.py`：
  - label/text 意图推断与缓存键生成
  - 快照 ref->intent 映射
- 新增 `autojobagent/core/manual_gate.py`：
  - manual-required 证据采集
  - 页面状态分类
  - Apply 入口候选筛选
- `vision_agent.py` 对应逻辑改为模块委托，继续收敛为编排层
- 新增测试：
  - `tests/test_intent_engine.py`
  - `tests/test_manual_gate.py`
- 全量回归验证通过（`70 passed`）

### Milestone R - V2.5 Phase A (Terminal Guard + Semantic Question Blocks)

- 新增 `autojobagent/core/semantic_tree.py`：
  - `QuestionBlock/OptionNode` 结构
  - 从 DOM 抽取问题块（question + options + selected/error）
  - 生成问题块摘要文本，供 LLM 规划时绑定问题语义
- 新增 `autojobagent/core/terminal_guard.py`：
  - `raw_response_implies_completion`（非 JSON 回复的终态兜底）
- `vision_agent.py` 关键改造：
  - `_observe_and_think` 增加 LLM 前“终态硬判定”（命中成功证据直接 `done`）
  - LLM parse fail 时，若原始文本表达“提交成功”且页面验证通过，直接收敛到 `done`
  - 增加 `question_blocks_detected` 与 `terminal_success_detected` 事件
  - `build_user_prompt` 注入语义问题块摘要
  - 修复非 dict JSON 解析结果导致的运行时异常风险（如 `'list' object has no attribute 'get'`）
- 新增测试：
  - `tests/test_semantic_tree.py`
  - `tests/test_terminal_guard.py`
  - `tests/test_vision_agent_error_gate.py` 新增 parse-fallback 成功收敛用例
- 全量回归验证通过（`75 passed`）

### Milestone S - V2.5 Phase B (Macro-Plan Execution Wiring)

- `vision_agent.py` 主循环完成 Phase B 接线：
  - 宏任务动作执行后，统一回写 `_on_macro_action_result(action, success)`
  - 提交阻断提前终止分支同样回写失败结果，避免宏任务状态“悬空”
- 保障“初始全局计划 + 每步状态检测 + 局部调整”执行语义一致，不再出现宏任务选中后无结果回写的问题
- 新增回归测试（`tests/test_vision_agent_error_gate.py`）：
  - `test_run_reports_macro_action_result_after_execution`
  - `test_run_reports_macro_result_when_submission_branch_stops`
- 全量回归验证通过（`77 passed`）

### Milestone T - V2.5 General Option Mapper (Semantic Tree + Profile Rules)

- `autojobagent/core/macro_tasks.py` 升级为通用选项映射器：
  - 内置规则覆盖常见申请题：work authorization / visa sponsorship / relocate / over-18 / driver's license / background check / drug test / relative / previous employment / remote preference / demographics / referral source
  - 布尔题不再依赖固定 `Yes/No` 文案，支持 `I do not require sponsorship` 等变体
  - 保留并强化 office 多选映射（preferred_locations 与页面选项做模糊交集）
  - 支持 profile 自定义 `option_rules`（可处理 A/B/C 等站点自定义标签）
- `MacroTask` 新增 `mapping_reason`，并在 `macro_task_selected` 日志事件中输出，提升可诊断性。
- `autojobagent/config/user_profile.yaml.example` 增加 `option_rules` 示例配置。
- 新增测试：`tests/test_macro_tasks.py`
  - 非 Yes/No 布尔文案映射
  - 远程办公偏好枚举映射
  - 办公地点多选映射
  - A/B/C 自定义规则映射
  - 无可靠匹配时跳过（避免误选）
- 全量回归验证通过（`82 passed`）

### Milestone U - V2.6 Engine Hardening Bundle (State Machine + FormGraph + Completion Scoring)

- 执行状态机升级（`autojobagent/core/fsm_orchestrator.py`）：
  - 新增 `derive_execution_phase`（`observe/plan/execute_chain/verify_action/repair/submit/finalize/manual_stop`）
  - 新增 `decide_local_adjustment_path`（动作失败后本地调整路径）
  - `vision_agent.py` 每步输出 `workflow_phase`，支持稳定复盘
- 语义树升级为可导航表单图（`autojobagent/core/semantic_tree.py`）：
  - 新增 `FieldNode/FormGraph`
  - 新增 `build_form_graph/format_form_graph`
  - `_observe_and_think` 注入 `form_graph_text` 到 prompt，并记录 `form_graph_generated`
- 终态判定升级（`autojobagent/core/outcome_classifier.py` + `vision_agent.py`）：
  - 新增 `CompletionAssessment` 与 `assess_completion_confidence`
  - `_verify_completion` 改为多信号评分（成功文案、Submit可见性、错误信号、URL成功线索、外部阻断线索）
  - 新增 `terminal_completion_assessed` 事件，降低“已提交仍继续操作”概率
- 宏任务链稳定性增强（`vision_agent.py`）：
  - 宏任务动作默认走“局部调整”，不触发全局语义 guard 重规划
  - 增加 precondition 等待与 `macro_task_waiting_precondition`
  - 全阻断时返回 `stuck`（`macro_plan_blocked`），避免空转
- 提交阻断恢复策略增强：
  - `external_blocked/transient_network` 第2次失败增加一次“刷新恢复”尝试（有 `reload` 能力时）
  - 保留最多3次上限后转人工
- 可观测性标准化：
  - 新增/统一事件：`plan_created`、`task_selected`、`submission_classified`、`finalized`
  - `GET /api/jobs/{id}/diagnostics` 事件摘要加入上述关键事件
- 新增测试：
  - `tests/test_outcome_classifier.py`
  - `tests/test_semantic_tree.py` 扩展 FormGraph 覆盖
  - `tests/test_fsm_orchestrator.py` 扩展执行阶段与本地调整路径覆盖
- 全量回归验证通过（`88 passed`）

## Suggested Next Steps

- 增加 `press/check/uncheck/hover` 等动作以提升复杂页面泛化
- 扩充“成功/错误文案词库”的可配置化
- 增加针对 `manual_reason` 的统计和聚类分析，持续降人工率
- 建立更细粒度的 e2e 回归场景库
