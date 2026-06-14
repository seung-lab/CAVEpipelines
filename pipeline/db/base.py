"""Engine + transactional session for a pipeline database.

Backend-agnostic: a database is just a SQLAlchemy URL, so the same models run on SQLite or a
server. The only backend-specific code is the local-SQLite seam: NullPool (connect per op) so a
deleted file is reopened by path rather than pinned as a dead inode; WAL + a lock wait so
concurrent CLI processes coexist; and creating the file's directory. A server URL gets a pooled
connection with pre_ping to recover one the server has dropped.
"""

import functools
import os
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

_SQLITE_FILE = "sqlite:///"
_init_lock = threading.Lock()
_initialized = set()  # (url, base) whose schema this process has ensured


def _resolve(url: str) -> str:
    """Create a local SQLite file's parent directory; pass any other URL through."""
    if url.startswith(_SQLITE_FILE):
        parent = os.path.dirname(url[len(_SQLITE_FILE) :])
        if parent:
            os.makedirs(parent, exist_ok=True)
    return url


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
    cur.execute("PRAGMA busy_timeout=5000")  # wait on a lock instead of erroring
    cur.close()


@functools.lru_cache(maxsize=None)
def _engine(url: str):
    if url.startswith("sqlite"):
        # connect per op so a deleted local file is reopened by path, never a pinned inode
        engine = create_engine(_resolve(url), poolclass=NullPool)
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine
    return create_engine(url, pool_pre_ping=True)  # recover a dropped server connection


@contextmanager
def session(url: str, base):
    """A session on `url` for `base`'s tables; commit on success, roll back on error. Schema
    is ensured once per process, lock-guarded so concurrent first calls don't race on DDL.
    `expire_on_commit=False` keeps loaded rows readable after the session closes."""
    engine = _engine(url)
    key = (url, base)
    if key not in _initialized:
        with _init_lock:
            if key not in _initialized:
                base.metadata.create_all(engine)
                _initialized.add(key)
    s = Session(engine, expire_on_commit=False)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
