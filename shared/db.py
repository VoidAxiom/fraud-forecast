from __future__ import annotations
from contextlib import contextmanager
from typing import Iterator, Literal
import os
from sqlalchemy import create_engine  # type: ignore[import-untyped]  # SQLAlchemy 1.4 has no type stubs
from sqlalchemy.engine import Engine  # type: ignore[import-untyped]  # SQLAlchemy 1.4 has no type stubs
from sqlalchemy.orm import sessionmaker, Session  # type: ignore[import-untyped]  # SQLAlchemy 1.4 has no type stubs

Role = Literal["app", "scoring", "simulator", "analyst"]

_DEFAULT_URLS: dict[Role, str] = {
    "app": "postgresql://app:app_dev_password@postgres:5432/fraud_platform",
    "scoring": "postgresql://scoring_user:scoring_dev_password@postgres:5432/fraud_platform",
    "simulator": "postgresql://simulator_user:simulator_dev_password@postgres:5432/fraud_platform",
    "analyst": "postgresql://analyst_user:analyst_dev_password@postgres:5432/fraud_platform",
}


def _resolve_url(role: Role) -> str:
    env_key = f"DATABASE_URL_{role.upper()}"
    if env_key in os.environ:
        return os.environ[env_key]
    if role == "app" and "DATABASE_URL" in os.environ:
        return os.environ["DATABASE_URL"]
    return _DEFAULT_URLS[role]


_engines: dict[Role, Engine] = {}


def get_engine(role: Role = "app") -> Engine:
    if role not in _engines:
        _engines[role] = create_engine(
            _resolve_url(role),
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return _engines[role]


_SessionLocal: dict[Role, sessionmaker[Session]] = {}


def _get_sessionmaker(role: Role) -> sessionmaker[Session]:
    if role not in _SessionLocal:
        _SessionLocal[role] = sessionmaker(bind=get_engine(role), expire_on_commit=False)
    return _SessionLocal[role]


@contextmanager
def get_session(role: Role = "app") -> Iterator[Session]:
    session = _get_sessionmaker(role)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
