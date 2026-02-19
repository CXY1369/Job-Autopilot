from contextlib import asynccontextmanager
from html import unescape
from pathlib import Path
from collections import Counter
import json
import time
import os
import re
import yaml
import httpx
from openai import OpenAI

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from .db.database import init_db, get_session
from .models.job_post import JobStatus
from .models.job_log import JobLog
from .core.scheduler import scheduler
from .core.browser_manager import BrowserManager, BrowserSession

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"
CONFIG_PATH = BASE_DIR / "config.yaml"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据库等资源
    init_db()
    yield
    # 这里预留资源清理逻辑


app = FastAPI(title="Job Autopilot - Auto Application Agent", lifespan=lifespan)

_login_session: BrowserSession | None = None
_llm_health_cache: dict | None = None
_RESUME_MATCH_LOG_RE = re.compile(r"score=(\d+),\s*reason=(.*)\)$")
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _clean_html_text(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"\s+", " ", unescape(raw)).strip()
    return text or None


def _extract_meta_content(html: str, key: str) -> str | None:
    pattern = re.compile(
        rf'<meta[^>]+(?:property|name)\s*=\s*["\']{re.escape(key)}["\'][^>]*content\s*=\s*["\'](.*?)["\']',
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(html)
    if not m:
        return None
    return _clean_html_text(m.group(1))


def _fetch_job_meta_from_link(link: str) -> tuple[str | None, str | None]:
    """
    Best-effort metadata extraction from a job URL.
    - title: prefers og:title, fallback to <title>
    - company: prefers og:site_name/application-name, fallback lightweight title heuristic
    """
    try:
        resp = httpx.get(
            link,
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "JobAutopilot/1.0 (+metadata-fetch)"},
        )
    except Exception:
        return None, None

    if resp.status_code >= 400 or not resp.text:
        return None, None

    html = resp.text
    title = _extract_meta_content(html, "og:title")
    if not title:
        m = _HTML_TITLE_RE.search(html)
        title = _clean_html_text(m.group(1)) if m else None

    company = (
        _extract_meta_content(html, "og:site_name")
        or _extract_meta_content(html, "application-name")
        or _extract_meta_content(html, "twitter:site")
    )

    # lightweight fallback: "Role - Company" / "Role | Company" / "Role at Company"
    if title and not company:
        for sep in [" - ", " | ", " at "]:
            if sep in title:
                left, right = title.split(sep, 1)
                left = _clean_html_text(left)
                right = _clean_html_text(right)
                if sep == " at ":
                    return left or title, right
                if left and right and len(right.split()) <= 6:
                    return left, right
                break
    return title, company


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR), html=True), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """
    返回前端单页应用。当前为极简原型。
    """
    index_file = UI_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Job Autopilot UI 未就绪</h1>", status_code=200)
    return HTMLResponse(index_file.read_text(encoding="utf-8"))


@app.get("/api/jobs")
def list_jobs(status: JobStatus | None = None):
    """
    列出岗位（MVP：简单分页/过滤，后续可扩展）。
    """
    from .db.database import SessionLocal
    from .models.job_post import JobPost

    with SessionLocal() as session:
        query = session.query(JobPost)
        if status is not None:
            query = query.filter(JobPost.status == status)
        jobs = query.order_by(JobPost.create_time.desc()).all()
        rows = [job.to_dict() for job in jobs]
        _attach_resume_match_info(rows, session)
        return rows


def _attach_resume_match_info(rows: list[dict], session) -> None:
    """
    Enrich job rows with resume match metadata parsed from logs:
    - resume_match_score
    - resume_match_reason
    """
    if not rows:
        return
    job_ids = [r.get("id") for r in rows if r.get("id") is not None]
    if not job_ids:
        return

    logs = (
        session.query(JobLog)
        .filter(JobLog.job_id.in_(job_ids))
        .order_by(JobLog.create_time.desc())
        .all()
    )

    match_map: dict[int, tuple[int | None, str | None]] = {}
    for log in logs:
        if log.job_id in match_map:
            continue
        msg = (log.message or "").strip()
        if "匹配结果:" not in msg or "(score=" not in msg:
            continue
        m = _RESUME_MATCH_LOG_RE.search(msg)
        if not m:
            match_map[log.job_id] = (None, None)
            continue
        try:
            score = int(m.group(1))
        except Exception:
            score = None
        reason = (m.group(2) or "").strip() or None
        match_map[log.job_id] = (score, reason)

    for row in rows:
        job_id = row.get("id")
        score, reason = match_map.get(job_id, (None, None))
        row["resume_match_score"] = score
        row["resume_match_reason"] = reason


@app.get("/api/stats/failures")
def get_failure_stats():
    """失败分类/原因聚合统计。"""
    from .db.database import SessionLocal
    from .models.job_post import JobPost

    with SessionLocal() as session:
        rows = (
            session.query(
                JobPost.failure_class,
                JobPost.failure_code,
                JobPost.status,
            )
            .filter(
                JobPost.status.in_([JobStatus.MANUAL_REQUIRED, JobStatus.FAILED]),
            )
            .all()
        )
    by_class: Counter[str] = Counter()
    by_code: Counter[str] = Counter()
    for failure_class, failure_code, _status in rows:
        cls = (failure_class or "unknown").strip() or "unknown"
        code = (failure_code or "unknown").strip() or "unknown"
        by_class[cls] += 1
        by_code[f"{cls}:{code}"] += 1
    top_codes = [{"key": k, "count": v} for k, v in by_code.most_common(8)]
    return {
        "ok": True,
        "total_failed_jobs": len(rows),
        "by_class": dict(by_class),
        "top_failure_codes": top_codes,
    }


@app.post("/api/jobs")
def add_job(payload: dict):
    """
    添加待申请岗位（MVP：只接收 link/title/company，后续用 Pydantic 模型强化）。
    """
    from .db.database import SessionLocal
    from .models.job_post import JobPost

    link = payload.get("link", "").strip()
    if not link:
        return {"ok": False, "error": "link is required"}

    title = payload.get("title", "").strip()
    company = payload.get("company", "").strip()
    if not title or not company:
        fetched_title, fetched_company = _fetch_job_meta_from_link(link)
        if not title and fetched_title:
            title = fetched_title
        if not company and fetched_company:
            company = fetched_company

    job = JobPost(
        company=company or None,
        title=title or None,
        link=link,
        status=JobStatus.PENDING,
    )

    with SessionLocal() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
        return {"ok": True, "job": job.to_dict()}


@app.get("/api/jobs/{job_id}/diagnostics")
def get_job_diagnostics(job_id: int):
    """返回 job 的关键诊断信息（最近日志摘要 + 截图路径 + trace 事件摘要）。"""
    from .db.database import SessionLocal
    from .models.job_post import JobPost

    with SessionLocal() as session:
        job = session.get(JobPost, job_id)
        if not job:
            return {"ok": False, "error": f"Job {job_id} not found"}
        logs = (
            session.query(JobLog)
            .filter(JobLog.job_id == job_id)
            .order_by(JobLog.create_time.desc())
            .limit(40)
            .all()
        )
        log_summary = [log.to_dict() for log in reversed(logs)]

    screenshots_root = BASE_DIR / "storage" / "screenshots"
    traces_root = BASE_DIR / "storage" / "logs"
    screenshot_dirs = sorted(
        screenshots_root.glob(f"job_{job_id}_*"),
        key=lambda p: p.name,
        reverse=True,
    )
    latest_screenshot_dir = screenshot_dirs[0] if screenshot_dirs else None
    screenshot_paths = []
    if latest_screenshot_dir and latest_screenshot_dir.is_dir():
        for p in sorted(latest_screenshot_dir.glob("*.jpg"))[:40]:
            screenshot_paths.append(str(p))

    trace_files = sorted(
        traces_root.glob(f"agent_trace_job_{job_id}_*.ndjson"),
        key=lambda p: p.name,
        reverse=True,
    )
    trace_events: list[dict] = []
    if trace_files:
        try:
            lines = trace_files[0].read_text(encoding="utf-8").splitlines()
            for raw in lines[-120:]:
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                if evt.get("event") in {
                    "submission_outcome_classified",
                    "retry_policy_applied",
                    "semantic_loop_guard",
                    "answer_binding_attempt",
                    "progression_block_with_fix_hint",
                }:
                    trace_events.append(evt)
        except Exception:
            trace_events = []

    return {
        "ok": True,
        "job": job.to_dict(),
        "logs": log_summary,
        "latest_screenshot_dir": str(latest_screenshot_dir) if latest_screenshot_dir else None,
        "screenshots": screenshot_paths,
        "trace_file": str(trace_files[0]) if trace_files else None,
        "trace_events": trace_events[-40:],
    }


@app.post("/api/control/start")
def start_applying():
    """
    启动投递调度
    """
    scheduler.start()
    return {"ok": True, "message": "scheduler started"}


@app.post("/api/control/pause")
def pause_applying():
    """
    暂停投递调度
    """
    scheduler.stop()
    return {"ok": True, "message": "paused"}


@app.post("/api/browser/login/open")
def open_login_browser(url: str):
    """
    打开用于手动登录的浏览器窗口（复用 profile），由用户自行完成登录。
    """
    global _login_session
    if _login_session is not None:
        return {"ok": False, "error": "login session already running"}
    manager = BrowserManager(
        log_fn=lambda msg, level="info": print(f"[login] [{level.upper()}] {msg}")
    )
    _login_session = manager.launch()
    target = url.strip() if url else "about:blank"
    try:
        _login_session.page.goto(target, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return {"ok": False, "error": f"open login page failed: {e}"}
    return {
        "ok": True,
        "message": "login browser opened",
        "url": _login_session.page.url,
    }


@app.post("/api/browser/login/close")
def close_login_browser():
    """关闭手动登录浏览器窗口。"""
    global _login_session
    if _login_session is None:
        return {"ok": False, "error": "no login session"}
    try:
        _login_session.close()
    finally:
        _login_session = None
    return {"ok": True, "message": "login browser closed"}


@app.get("/api/browser/login/status")
def login_browser_status():
    """查询手动登录浏览器是否仍在运行。"""
    running = _login_session is not None
    return {"ok": True, "running": running}


@app.get("/api/llm/models")
def get_llm_models():
    """返回当前模型与可选模型列表。"""
    cfg = _load_config()
    llm_cfg = cfg.get("llm", {})
    models = llm_cfg.get("fallback_models") or []
    current = llm_cfg.get("model", "")
    return {"ok": True, "current": current, "models": models}


@app.post("/api/llm/model")
def set_llm_model(payload: dict):
    """切换当前模型（必须在 fallback_models 中）。"""
    model = (payload.get("model") or "").strip()
    if not model:
        return {"ok": False, "error": "model is required"}
    cfg = _load_config()
    llm_cfg = cfg.setdefault("llm", {})
    models = llm_cfg.get("fallback_models") or []
    if model not in models:
        return {"ok": False, "error": "model not in fallback_models"}
    llm_cfg["model"] = model
    # 选中模型优先
    llm_cfg["fallback_models"] = [model] + [m for m in models if m != model]
    _save_config(cfg)
    return {"ok": True, "model": model}


@app.get("/api/llm/health")
def llm_health_check():
    """对当前模型做一次轻量健康检查。"""
    cfg = _load_config()
    llm_cfg = cfg.get("llm", {})
    model = llm_cfg.get("model", "")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY 未设置"}
    if not model:
        return {"ok": False, "error": "model 未设置"}
    client = OpenAI(api_key=api_key)
    start = time.time()
    try:
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0.0,
        )
        latency_ms = int((time.time() - start) * 1000)
        global _llm_health_cache
        _llm_health_cache = {"model": model, "ok": True, "latency_ms": latency_ms}
        return {"ok": True, "model": model, "latency_ms": latency_ms}
    except Exception as e:
        return {"ok": False, "model": model, "error": str(e)}


@app.get("/api/jobs/{job_id}/logs")
def get_job_logs(job_id: int):
    """返回指定 job 的 AI 日志。"""
    with get_session() as session:
        logs = (
            session.query(JobLog)
            .filter(JobLog.job_id == job_id)
            .order_by(JobLog.create_time.asc())
            .all()
        )
        return [log.to_dict() for log in logs]


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int):
    """
    删除单条岗位记录及其关联的日志。
    """
    from .db.database import SessionLocal
    from .models.job_post import JobPost

    with SessionLocal() as session:
        # 先删除关联日志
        session.query(JobLog).filter(JobLog.job_id == job_id).delete()
        # 再删除岗位记录
        deleted = session.query(JobPost).filter(JobPost.id == job_id).delete()
        session.commit()

        if deleted:
            return {"ok": True, "message": f"Job {job_id} deleted"}
        else:
            return {"ok": False, "error": f"Job {job_id} not found"}


@app.delete("/api/jobs")
def clear_jobs(status: JobStatus):
    """
    清空指定状态的所有岗位记录及其关联的日志。

    例如：DELETE /api/jobs?status=applied 清空所有已申请的记录
    """
    from .db.database import SessionLocal
    from .models.job_post import JobPost

    with SessionLocal() as session:
        # 找出所有符合条件的 job_id
        job_ids = [
            job.id
            for job in session.query(JobPost.id).filter(JobPost.status == status).all()
        ]

        if not job_ids:
            return {
                "ok": True,
                "message": f"No jobs with status {status.value}",
                "deleted": 0,
            }

        # 删除关联日志
        session.query(JobLog).filter(JobLog.job_id.in_(job_ids)).delete(
            synchronize_session=False
        )
        # 删除岗位记录
        deleted = (
            session.query(JobPost)
            .filter(JobPost.status == status)
            .delete(synchronize_session=False)
        )
        session.commit()

        return {
            "ok": True,
            "message": f"Cleared {deleted} jobs with status {status.value}",
            "deleted": deleted,
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("autojobagent.app:app", host="127.0.0.1", port=8000, reload=True)
