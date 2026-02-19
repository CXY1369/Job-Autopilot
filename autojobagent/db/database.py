from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, text
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
    _patch_jobs_table_schema()


def _patch_jobs_table_schema() -> None:
    """
    在无迁移框架下，幂等补齐 jobs 表缺失字段。
    """
    required_columns: dict[str, str] = {
        "failure_class": "VARCHAR(64)",
        "failure_code": "VARCHAR(128)",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "last_error_snippet": "TEXT",
        "last_outcome_class": "VARCHAR(64)",
        "last_outcome_at": "DATETIME",
    }
    try:
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(jobs)")).fetchall()
            existing = {str(row[1]) for row in rows}
            for col, ddl in required_columns.items():
                if col in existing:
                    continue
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}"))
    except Exception:
        # schema patching is best-effort; table may not exist yet in tests/startup races
        return


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
