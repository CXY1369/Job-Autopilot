from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, Text, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base


class JobLog(Base):
    """AI 执行步骤日志，按 job_id 追踪。"""

    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    create_time: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "level": self.level,
            "message": self.message,
            "create_time": self.create_time.isoformat() if self.create_time else None,
        }
