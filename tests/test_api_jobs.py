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
    assert "failure_class" in rows[0]
    assert "failure_code" in rows[0]
    assert "retry_count" in rows[0]
    assert "last_error_snippet" in rows[0]
    assert "last_outcome_class" in rows[0]
    assert "last_outcome_at" in rows[0]


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


def test_failure_stats_endpoint(isolated_db):
    with isolated_db() as session:
        session.add_all(
            [
                JobPost(
                    company="A",
                    title="x",
                    link="https://example.com/1",
                    status=JobStatus.MANUAL_REQUIRED,
                    failure_class="external_blocked",
                    failure_code="anti_spam_flagged",
                ),
                JobPost(
                    company="B",
                    title="y",
                    link="https://example.com/2",
                    status=JobStatus.FAILED,
                    failure_class="validation_error",
                    failure_code="missing_required_field",
                ),
            ]
        )
        session.commit()
    with TestClient(app) as client:
        resp = client.get("/api/stats/failures")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["by_class"]["external_blocked"] == 1
    assert data["by_class"]["validation_error"] == 1
    keys = [x["key"] for x in data["top_failure_codes"]]
    assert "external_blocked:anti_spam_flagged" in keys


def test_job_diagnostics_endpoint(isolated_db):
    with isolated_db() as session:
        job = JobPost(
            company="Acme",
            title="ML Engineer",
            link="https://example.com/job/diag",
            status=JobStatus.MANUAL_REQUIRED,
            failure_class="unknown",
            failure_code="semantic_loop_stop",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        session.add(JobLog(job_id=job.id, level="warn", message="need manual"))
        session.commit()
        job_id = job.id

    with TestClient(app) as client:
        resp = client.get(f"/api/jobs/{job_id}/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["job"]["id"] == job_id
    assert isinstance(data["logs"], list)
    assert "screenshots" in data
    assert "trace_events" in data
