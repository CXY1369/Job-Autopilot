from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import String, Integer, DateTime, Enum as SQLEnum, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base


class JobStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    APPLIED = "applied"
    MANUAL_REQUIRED = "manual_required"
    FAILED = "failed"
    PAUSED = "paused"


class JobPost(Base):
    """岗位记录，对应 jobs 表。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    link: Mapped[str] = mapped_column(String(1024), nullable=False, unique=False)
    status: Mapped[JobStatus] = mapped_column(
        SQLEnum(JobStatus),
        default=JobStatus.PENDING,
        index=True,
        nullable=False,
    )
    resume_used: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_outcome_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_outcome_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    apply_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company": self.company,
            "title": self.title,
            "link": self.link,
            "status": self.status.value
            if isinstance(self.status, JobStatus)
            else self.status,
            "resume_used": self.resume_used,
            "fail_reason": self.fail_reason,
            "manual_reason": self.manual_reason,
            "failure_class": self.failure_class,
            "failure_code": self.failure_code,
            "retry_count": self.retry_count,
            "last_error_snippet": self.last_error_snippet,
            "last_outcome_class": self.last_outcome_class,
            "last_outcome_at": self.last_outcome_at.isoformat()
            if self.last_outcome_at
            else None,
            "create_time": self.create_time.isoformat() if self.create_time else None,
            "apply_time": self.apply_time.isoformat() if self.apply_time else None,
        }
