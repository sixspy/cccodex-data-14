import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=15,
            isolation_level=None,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 15000")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction(immediate=False):
    """显式事务。immediate=True 先拿写锁，用于交班 / 接手的争用判定。"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                creator TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                successor TEXT,
                mode TEXT,
                window_start TEXT,
                window_end TEXT,
                handover_id INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                code TEXT,
                shift_id INTEGER REFERENCES shifts(id),
                created_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS handovers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER NOT NULL REFERENCES shifts(id),
                to_shift_id INTEGER REFERENCES shifts(id),
                successor TEXT NOT NULL,
                mode TEXT NOT NULL,
                registered_on TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                code_count INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS handover_codes (
                handover_id INTEGER NOT NULL REFERENCES handovers(id) ON DELETE CASCADE,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY (handover_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS day_registrations (
                day TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                registered_by TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS shift_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER,
                handover_id INTEGER,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                actor TEXT,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        _ensure_job_columns(db)
        db.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_code
                ON crawl_jobs(code) WHERE code IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_jobs_shift ON crawl_jobs(shift_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_shifts_single_active
                ON shifts(status) WHERE status = 'active';
            CREATE UNIQUE INDEX IF NOT EXISTS ux_handovers_one_pending
                ON handovers(shift_id) WHERE status = 'pending';
            CREATE INDEX IF NOT EXISTS ix_handovers_shift ON handovers(shift_id);
            """
        )
        db.execute(
            "UPDATE crawl_jobs SET code = 'CJ-' || printf('%04d', id) WHERE code IS NULL"
        )
        db.commit()


def _ensure_job_columns(db):
    """兼容旧库：为 crawl_jobs 补 code / shift_id / created_by 列。"""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(crawl_jobs)")}
    if "code" not in cols:
        db.execute("ALTER TABLE crawl_jobs ADD COLUMN code TEXT")
    if "shift_id" not in cols:
        db.execute("ALTER TABLE crawl_jobs ADD COLUMN shift_id INTEGER")
    if "created_by" not in cols:
        db.execute("ALTER TABLE crawl_jobs ADD COLUMN created_by TEXT")
