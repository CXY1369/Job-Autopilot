from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


DATABASE_URL = "sqlite:///./autojobagent/autojobagent.db"


class Base(DeclarativeBase):
    """SQLAlchemy Base."""


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

# 禁用 expire_on_commit，避免离开 Session 后对象属性失效导致 DetachedInstanceError
SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
)


def init_db() -> None:
    """初始化数据库表结构。"""
    from ..models.job_post import JobPost  # noqa: F401
    from ..models.resume import Resume  # noqa: F401
    from ..models.user_profile import UserProfile  # noqa: F401
    from ..models.job_log import JobLog  # noqa: F401

    Base.metadata.create_all(bind=engine)


@contextmanager
def get_session():
    """提供一个上下文管理的 Session，便于在业务代码中使用 with get_session()."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
