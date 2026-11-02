"""SQLite connection helper and forward-only migration runner."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d+)_.*\.sql$")


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL, foreign keys and Row access."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def _pending_migrations(current: int) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for sql_file in MIGRATIONS_DIR.glob("*.sql"):
        match = _MIGRATION_NAME.match(sql_file.name)
        if match and int(match.group(1)) > current:
            found.append((int(match.group(1)), sql_file))
    return sorted(found)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order, one transaction each; return final version."""
    current = _current_version(conn)
    for version, sql_file in _pending_migrations(current):
        script = (
            "BEGIN;\n"
            + sql_file.read_text(encoding="utf-8")
            + "\nDELETE FROM schema_version;\n"
            + f"INSERT INTO schema_version (version) VALUES ({version});\n"
            + "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            logger.exception("Migration %s failed", sql_file.name)
            raise
        logger.info("Applied migration %s", sql_file.name)
        current = version
    return current
