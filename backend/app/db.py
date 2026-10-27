"""SQLite engine and session setup."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    )
    engine = create_engine(database_url, connect_args=connect_args, future=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def set_sqlite_pragmas(connection, _record) -> None:
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def database_url_for(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(data_dir / 'review.db').as_posix()}"


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(session_factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
