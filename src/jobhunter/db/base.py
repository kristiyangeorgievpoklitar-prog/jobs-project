"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Engine, MetaData, String, TypeDecorator, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class StrEnumType(TypeDecorator):
    """Stores a StrEnum as text and returns the enum member on load.

    Without this, SQLAlchemy hands back a bare ``str`` and a column declared as
    ``Mapped[JobState]`` would not actually contain a ``JobState``.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type, length: int = 30) -> None:
        self.enum_class = enum_class
        super().__init__(length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value))

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            # Tolerate rows written by an older version of the schema.
            return value


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used for every column default."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds created_at / updated_at to a model."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


def _enable_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Foreign keys are off by default in SQLite; WAL improves concurrent reads."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """Build an engine, special-casing in-memory SQLite used by the tests."""
    is_sqlite = url.startswith("sqlite")
    is_memory = ":memory:" in url

    kwargs: dict[str, Any] = {"echo": echo, "future": True}
    if is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    if is_memory:
        kwargs["poolclass"] = StaticPool

    engine = create_engine(url, **kwargs)
    if is_sqlite:
        event.listen(engine, "connect", _enable_sqlite_pragmas)
    return engine


class Database:
    """Owns an engine and hands out sessions."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine = create_db_engine(url, echo=echo)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, class_=Session
        )

    def create_all(self) -> None:
        """Create the schema directly. Migrations are preferred outside tests."""
        from jobhunter.db import models  # noqa: F401  (register mappers)

        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
