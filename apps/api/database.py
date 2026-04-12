"""Database helpers for the managed API skeleton."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import ApiSettings, get_settings
from .models import Base

_ENGINE_CACHE: dict[str, Engine] = {}


def _engine_kwargs(database_url: str) -> dict:
    kwargs = {"future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return kwargs


def get_engine(database_url: str) -> Engine:
    """Return a cached engine for the given database URL."""
    engine = _ENGINE_CACHE.get(database_url)
    if engine is None:
        engine = create_engine(database_url, **_engine_kwargs(database_url))
        _ENGINE_CACHE[database_url] = engine
    return engine


def reset_engine_cache() -> None:
    """Dispose and clear cached engines, primarily for tests."""
    for engine in _ENGINE_CACHE.values():
        engine.dispose()
    _ENGINE_CACHE.clear()


def session_factory_for_settings(settings: ApiSettings) -> sessionmaker[Session]:
    engine = get_engine(settings.database_url)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def create_all_tables(engine: Engine) -> None:
    Base.metadata.create_all(bind=engine)


def get_db_session(settings: ApiSettings = Depends(get_settings)) -> Generator[Session, None, None]:
    """FastAPI dependency for a managed API session."""
    session = session_factory_for_settings(settings)()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope(settings: ApiSettings) -> Generator[Session, None, None]:
    """Context-managed session helper used by tests and workers."""
    session = session_factory_for_settings(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
