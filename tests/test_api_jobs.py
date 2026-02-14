from __future__ import annotations

from fastapi.testclient import TestClient

from autojobagent.app import app
from autojobagent.models.job_log import JobLog
from autojobagent.models.job_post import JobPost, JobStatus


def test_list_jobs_contains_resume_match_fields(isolated_db):
    with isolated_db() as session:
        job = JobPost(
            company="Acme",
            title="Backend Engineer",
            link="https://example.com/job/1",
            status=JobStatus.APPLIED,
            resume_used="/tmp/alex_backend_resume.pdf",
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        session.add(
            JobLog(
                job_id=job.id,
                level="info",
                message="匹配结果: alex_backend_resume.pdf (score=88, reason=python backend fit)",
            )
        )
        session.commit()

    with TestClient(app) as client:
        resp = client.get("/api/jobs")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["resume_used"] == "/tmp/alex_backend_resume.pdf"
    assert rows[0]["resume_match_score"] == 88
    assert rows[0]["resume_match_reason"] == "python backend fit"


def test_clear_manual_and_failed_via_two_calls(isolated_db):
    with isolated_db() as session:
        session.add_all(
            [
                JobPost(
                    company="A",
                    title="x",
                    link="https://example.com/1",
                    status=JobStatus.MANUAL_REQUIRED,
                ),
                JobPost(
                    company="B",
                    title="y",
                    link="https://example.com/2",
                    status=JobStatus.FAILED,
                ),
                JobPost(
                    company="C",
                    title="z",
                    link="https://example.com/3",
                    status=JobStatus.PENDING,
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        r1 = client.delete("/api/jobs", params={"status": "manual_required"})
        r2 = client.delete("/api/jobs", params={"status": "failed"})
        r3 = client.get("/api/jobs")

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["ok"] is True and r1.json()["deleted"] == 1
    assert r2.json()["ok"] is True and r2.json()["deleted"] == 1

    remaining = r3.json()
    assert len(remaining) == 1
    assert remaining[0]["status"] == "pending"
