import sqlite3
from contextlib import contextmanager

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS passkeys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    passkey_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_status (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    online INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS capture_requests (
    request_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','timeout')),
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    error_message TEXT,
    image_id TEXT
);

CREATE TABLE IF NOT EXISTS wifi_requests (
    request_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    ssid TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','connected','failed','timeout')),
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    request_id TEXT REFERENCES capture_requests(request_id),
    s3_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    captured_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_images_device_captured
    ON images(device_id, captured_at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn = _connect()


def init_db() -> None:
    _conn.executescript(SCHEMA)
    _conn.commit()


@contextmanager
def get_cursor():
    cur = _conn.cursor()
    try:
        yield cur
        _conn.commit()
    except Exception:
        _conn.rollback()
        raise
    finally:
        cur.close()
