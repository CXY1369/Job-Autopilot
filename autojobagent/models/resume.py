from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base


class Resume(Base):
    """简历元数据表，仅存简历文件路径与标签等信息。"""

    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    tags: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        doc="逗号分隔的技能/方向标签，如 backend,python,ml",
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    last_used_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def tag_list(self) -> list[str]:
        if not self.tags:
            return []
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "tags": self.tag_list(),
            "language": self.language,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_time": self.last_used_time.isoformat()
            if self.last_used_time
            else None,
        }
