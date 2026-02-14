from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..db.database import Base


class UserProfile(Base):
    """用户基础信息与通用回答模板。单用户场景下仅保留一条记录。"""

    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    years_of_experience: Mapped[str | None] = mapped_column(String(64), nullable=True)
    education: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 可复用问答模板（JSON 字符串或纯文本，后续可演进为 JSON）
    templates: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="可选问题模板与常用回答（JSON 或多行文本），如 why_company / salary / visa 等",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "city": self.city,
            "country": self.country,
            "years_of_experience": self.years_of_experience,
            "education": self.education,
            "templates": self.templates,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


