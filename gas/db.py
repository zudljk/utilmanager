import json
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
 id INTEGER PRIMARY KEY CHECK(id=1), capacity_liters TEXT NOT NULL,
 factor TEXT NOT NULL, opening_liters TEXT NOT NULL, start_month TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
 id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, request_key TEXT UNIQUE,
 month TEXT NOT NULL, filename TEXT NOT NULL, ocr_text TEXT NOT NULL,
 candidates TEXT NOT NULL, warning TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','confirmed','rejected')),
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS consumption (
 month TEXT PRIMARY KEY, kwh TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 upload_id TEXT UNIQUE REFERENCES uploads(id), revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY, month TEXT NOT NULL, delivery_date TEXT,
 liters TEXT NOT NULL, unit_price TEXT, note TEXT NOT NULL DEFAULT '',
 submission_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS historical_prices (
 month TEXT PRIMARY KEY, unit_price TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons (
 month TEXT PRIMARY KEY, kwh TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT NOT NULL,
 before_json TEXT, after_json TEXT
);
CREATE TABLE IF NOT EXISTS imports (
 sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version=1;
"""


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=10000')
    return db


def initialize(path):
    with connection(path) as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise RuntimeError(f'Unbekannte Datenbankversion: {version}')
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)


@contextmanager
def connection(path):
    db = connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def audit(db, action, entity, entity_id, before=None, after=None):
    encode = lambda value: json.dumps(dict(value), ensure_ascii=False) if value is not None else None
    db.execute('INSERT INTO audit(action,entity,entity_id,before_json,after_json) VALUES(?,?,?,?,?)',
               (action, entity, str(entity_id), encode(before), encode(after)))
