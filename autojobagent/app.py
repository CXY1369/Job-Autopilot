from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from .db.database import init_db, get_session
from .models.job_post import JobStatus
from .models.job_log import JobLog
from .core.scheduler import scheduler

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化数据库等资源
    init_db()
    yield
    # 这里预留资源清理逻辑


app = FastAPI(title="Job Autopilot - Auto Application Agent", lifespan=lifespan)

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
        return [job.to_dict() for job in jobs]


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

    job = JobPost(
        company=payload.get("company", "").strip() or None,
        title=payload.get("title", "").strip() or None,
        link=link,
        status=JobStatus.PENDING,
    )

    with SessionLocal() as session:
        session.add(job)
        session.commit()
        session.refresh(job)
        return {"ok": True, "job": job.to_dict()}


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
            job.id for job in 
            session.query(JobPost.id).filter(JobPost.status == status).all()
        ]
        
        if not job_ids:
            return {"ok": True, "message": f"No jobs with status {status.value}", "deleted": 0}
        
        # 删除关联日志
        session.query(JobLog).filter(JobLog.job_id.in_(job_ids)).delete(synchronize_session=False)
        # 删除岗位记录
        deleted = session.query(JobPost).filter(JobPost.status == status).delete(synchronize_session=False)
        session.commit()
        
        return {"ok": True, "message": f"Cleared {deleted} jobs with status {status.value}", "deleted": deleted}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("autojobagent.app:app", host="127.0.0.1", port=8000, reload=True)


