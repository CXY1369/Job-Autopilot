# Project Progress Tracking

## Metadata

- `project_name`: `Job Autopilot - Auto Application Agent`
- `last_updated_utc`: `2026-02-14`
- `primary_language`: `Python`
- `runtime_stack`: `FastAPI + Playwright + OpenAI + SQLite`
- `document_purpose`: `维护进度、实现细节与后续规划（AI/人类共读）`

## Executive Snapshot

- 当前状态：`Active / Usable`
- CI 状态：`Green`（`Lint + Core Tests + Full Tests`）
- 自动化测试：`11 passed`
- 架构定位：`AI 决策 + 规则护栏`
- 关键新增能力：`refresh 动作与卡页重试、简历匹配 Stage A、上传白名单、前进门控`

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

## Suggested Next Steps

- 增加 `press/check/uncheck/hover` 等动作以提升复杂页面泛化
- 扩充“成功/错误文案词库”的可配置化
- 增加针对 `manual_reason` 的统计和聚类分析，持续降人工率
- 建立更细粒度的 e2e 回归场景库
