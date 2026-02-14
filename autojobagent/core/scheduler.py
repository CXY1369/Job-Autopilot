"""
单线程任务调度器（MVP 占位实现）。

职责：
- 从 jobs 表中按顺序取出 pending 任务
- 将其标记为 in_progress 并调用自动投递执行模块
- 根据结果更新为 applied / manual_required / failed

当前文件仅提供接口骨架，便于后续接入 Playwright + Browser-Use。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Thread
from typing import Optional

from ..db.database import get_session
from ..models.job_post import JobPost, JobStatus
from .applier import apply_for_job


@dataclass
class SchedulerConfig:
    """
    调度器配置（可后续从 config.yaml 读取）。
    """

    poll_interval_seconds: float = 2.0


class JobScheduler:
    """
    极简单线程调度器骨架。

    注意：当前仅定义接口与主循环结构，尚未真正调用浏览器自动投递逻辑。
    """

    def __init__(self, config: Optional[SchedulerConfig] = None) -> None:
        self.config = config or SchedulerConfig()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._running = True

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False

    def _run_loop(self) -> None:
        import time

        while not self._stop_event.is_set():
            job = self._fetch_next_pending_job()
            if not job:
                time.sleep(self.config.poll_interval_seconds)
                continue

            self._process_job(job)

    def _fetch_next_pending_job(self) -> Optional[JobPost]:
        with get_session() as session:
            job = (
                session.query(JobPost)
                .filter(JobPost.status == JobStatus.PENDING)
                .order_by(JobPost.create_time.asc())
                .first()
            )
            if job:
                job.status = JobStatus.IN_PROGRESS
                session.add(job)
            return job

    def _process_job(self, job: JobPost) -> None:
        """
        调用自动投递执行模块处理一个岗位。
        """
        from datetime import datetime, timezone

        result = apply_for_job(job)
        
        # 使用 get_session() 确保自动 commit
        with get_session() as session:
            db_job = session.get(JobPost, job.id)
            if not db_job:
                return
            if result.resume_used:
                db_job.resume_used = result.resume_used
            if result.success:
                db_job.status = JobStatus.APPLIED
                db_job.fail_reason = None
                db_job.manual_reason = None
                print(f"[job={job.id}] ✓ 状态更新为: APPLIED")
            elif result.manual_required:
                db_job.status = JobStatus.MANUAL_REQUIRED
                db_job.fail_reason = None
                db_job.manual_reason = result.manual_reason
                print(f"[job={job.id}] ⚠ 状态更新为: MANUAL_REQUIRED")
            else:
                db_job.status = JobStatus.FAILED
                db_job.fail_reason = result.fail_reason
                db_job.manual_reason = None
                print(f"[job={job.id}] ❌ 状态更新为: FAILED")
            db_job.apply_time = datetime.now(timezone.utc)
            session.add(db_job)
            # get_session() 会自动 commit


# 全局单例调度器（MVP 简化处理）
scheduler = JobScheduler()


