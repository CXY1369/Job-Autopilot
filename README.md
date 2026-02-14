# Job Autopilot - Auto Application Agent

一个基于 `Python + FastAPI + Playwright + OpenAI` 的自动化求职投递助手。

项目当前采用“规则护栏 + 视觉 AI 决策”的混合架构：
- AI 负责基于当前页面状态决定下一步操作
- 规则负责上传安全、前进门控、完成验证、异常恢复等稳定性保障

## Current Status

- 核心功能已可运行：岗位入队、调度执行、自动表单填写、状态追踪、日志与截图
- 已支持 Stage A 简历匹配：JD 提取 -> 多简历打分 -> 选择最佳简历上传
- 已接入 CI（GitHub Actions）：`lint` -> `core-tests` -> `full-tests`
- 当前测试基线：`pytest` 全量 11 个测试用例通过

详细进度请见：`docs/PROJECT_PROGRESS_TRACKING.md`

## Quick Start

1) 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) 初始化 Playwright（首次）

```bash
playwright install chromium
```

3) 配置环境变量

- 复制 `.env.example`，至少设置 `OPENAI_API_KEY`

4) 启动服务

```bash
python -m autojobagent.app
```

5) 打开前端

- `http://127.0.0.1:8000`

## Testing

本地建议顺序：

```bash
python -m ruff check autojobagent tests
python -m ruff format --check autojobagent tests
python -m pytest -q --cache-clear
```

CI 在 push 到 `main` 后自动运行：
- `Lint (Ruff)`
- `Core Tests (Fast Gate)`
- `Full Tests (Regression)`

## Project Structure

- `autojobagent/app.py`: FastAPI API 与页面入口
- `autojobagent/core/applier.py`: 单岗位投递主流程编排
- `autojobagent/core/vision_agent.py`: 视觉 AI Agent（观察-思考-行动）
- `autojobagent/core/browser_manager.py`: 浏览器会话与扩展管理
- `autojobagent/core/resume_matcher.py`: JD 提取与简历匹配
- `autojobagent/core/scheduler.py`: 单线程调度与状态迁移
- `autojobagent/ui/index.html`: 前端控制台与岗位列表
- `autojobagent/models/`: 数据模型（jobs、logs、resumes 等）
- `autojobagent/db/database.py`: SQLite 与会话管理
- `tests/`: 单元与轻量集成测试
- `.github/workflows/ci.yml`: CI 工作流


