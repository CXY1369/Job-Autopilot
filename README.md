# Job Autopilot – Auto Application Agent (个人版)

一个基于 Python + FastAPI + Playwright + Browser-Use 的全自动求职投递助手，用于批量自动填写招聘网站申请表单，并记录申请结果。

## 快速开始（MVP 骨架）

1. 创建虚拟环境并安装依赖：

```bash
pip install -r requirements.txt
```

2. 初始化 Playwright 浏览器（首次使用需要）：

```bash
playwright install
```

3. 本地启动 FastAPI 应用（后续会在 `autojobagent/app.py` 中实现）：

```bash
python -m autojobagent.app
```

4. 在浏览器中打开：

- http://127.0.0.1:8000  （Web UI，后续实现）

## 项目结构（MVP）

核心代码位于 `autojobagent/` 目录下：

- `app.py`：FastAPI 入口
- `ui/index.html`：简单 Web UI
- `core/`：调度器、自动投递逻辑、Simplify 集成、Browser-Use 封装
- `models/`：简历、用户信息、岗位模型
- `db/database.py`：SQLite 访问封装
- `storage/`：本地简历、上传文件、日志等
- `config.yaml`：全局配置

> 当前仓库处于 MVP 骨架阶段，核心业务逻辑会在后续迭代中逐步完善。


