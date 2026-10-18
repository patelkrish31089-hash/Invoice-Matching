from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from fastapi import Request
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    options: dict = {"echo": echo, "pool_pre_ping": True}
    is_sqlite = database_url.startswith("sqlite")
    if is_sqlite:
        # SQLite is used for local/test environments, not production. Acquire its
        # single-writer lock when a transaction starts so audit sequence reads and
        # appends cannot race after separate requests have read the same chain head.
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if ":memory:" in database_url:
            options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)
    if is_sqlite:
        @event.listens_for(engine, "connect")
        def _configure_sqlite_transaction(dbapi_connection, _connection_record) -> None:
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

        @event.listens_for(engine, "begin")
        def _begin_sqlite_transaction(connection) -> None:
            mode = str(connection.get_execution_options().get("sqlite_begin_mode", "IMMEDIATE")).upper()
            if mode not in {"DEFERRED", "IMMEDIATE", "EXCLUSIVE"}:
                raise ValueError("Unsupported SQLite transaction begin mode")
            connection.exec_driver_sql(f"BEGIN {mode}")
    return engine


def create_session_factory(engine: Engine, *, immediate: bool = True):
    """Create a session factory; SQLite write sessions serialize at BEGIN IMMEDIATE.

    Read-only request handlers may use a deferred factory so they do not reserve
    SQLite's single writer slot while a worker is committing document analysis.
    """
    mode = "IMMEDIATE" if immediate else "DEFERRED"
    session_engine = engine.execution_options(sqlite_begin_mode=mode)
    return sessionmaker(bind=session_engine, autoflush=False, autocommit=False, expire_on_commit=False)


def requires_write_transaction(endpoint):
    """Mark a safe-method route that records an audit event or other durable write."""
    endpoint.__requires_write_transaction__ = True
    return endpoint


def get_db(request: Request):
    method = request.method.upper()
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    route_writes = bool(getattr(endpoint, "__requires_write_transaction__", False))
    if method in {"GET", "HEAD", "OPTIONS"} and not route_writes:
        factory = getattr(request.app.state, "read_session_factory", request.app.state.session_factory)
    else:
        factory = request.app.state.session_factory
    db: Session = factory()
    try:
        yield db
    finally:
        db.close()
