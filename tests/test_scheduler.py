from __future__ import annotations

from autojobagent.core.applier import ApplyResult
from autojobagent.core.scheduler import JobScheduler, SchedulerConfig
from autojobagent.models.job_post import JobPost, JobStatus


def test_scheduler_fetch_and_process_failed_job(monkeypatch, isolated_db):
    with isolated_db() as session:
        session.add(
            JobPost(
                company="Acme",
                title="Backend Engineer",
                link="https://example.com/job/1",
                status=JobStatus.PENDING,
                manual_reason="old-manual",
            )
        )
        session.commit()

    scheduler = JobScheduler(config=SchedulerConfig(poll_interval_seconds=0.01))
    fetched = scheduler._fetch_next_pending_job()
    assert fetched is not None
    assert fetched.status == JobStatus.IN_PROGRESS

    with isolated_db() as session:
        db_job = session.get(JobPost, fetched.id)
        assert db_job is not None
        assert db_job.status == JobStatus.IN_PROGRESS

    monkeypatch.setattr(
        "autojobagent.core.scheduler.apply_for_job",
        lambda _job: ApplyResult(
            success=False,
            manual_required=False,
            fail_reason="agent crashed",
            resume_used="/tmp/alex_backend_resume.pdf",
        ),
    )
    scheduler._process_job(fetched)

    with isolated_db() as session:
        db_job = session.get(JobPost, fetched.id)
        assert db_job is not None
        assert db_job.status == JobStatus.FAILED
        assert db_job.fail_reason == "agent crashed"
        assert db_job.manual_reason is None
        assert db_job.resume_used == "/tmp/alex_backend_resume.pdf"
        assert db_job.apply_time is not None
