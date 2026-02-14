from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    """
    Create an isolated sqlite database for API/scheduler integration tests.
    """
    from autojobagent import app as app_module
    from autojobagent.core import scheduler as scheduler_module
    from autojobagent.db import database as db_module
    from autojobagent.db.database import Base

    db_file = tmp_path / "test_autojobagent.db"
    test_engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=test_engine,
        expire_on_commit=False,
    )

    @contextmanager
    def testing_get_session():
        s = TestingSessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # patch db module symbols
    monkeypatch.setattr(db_module, "engine", test_engine, raising=True)
    monkeypatch.setattr(db_module, "SessionLocal", TestingSessionLocal, raising=True)
    monkeypatch.setattr(db_module, "get_session", testing_get_session, raising=True)

    # patch modules that imported these symbols directly
    monkeypatch.setattr(app_module, "get_session", testing_get_session, raising=True)
    monkeypatch.setattr(scheduler_module, "get_session", testing_get_session, raising=True)

    # create tables after patching engine/session factory
    Base.metadata.create_all(bind=test_engine)
    return TestingSessionLocal

