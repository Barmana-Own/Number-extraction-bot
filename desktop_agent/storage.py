from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class AgentStorage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    batch_id TEXT PRIMARY KEY,
                    job_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(next_attempt_at, created_at);
                CREATE TABLE IF NOT EXISTS job_checkpoints (
                    job_id INTEGER PRIMARY KEY,
                    stats_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_numbers (
                    job_id INTEGER NOT NULL,
                    number TEXT NOT NULL,
                    pattern TEXT,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (job_id, number)
                );
                CREATE INDEX IF NOT EXISTS idx_pending_numbers_job ON pending_numbers(job_id, created_at);
                """
            )
            self.db.commit()

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def set_meta(self, key: str, value: Any) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self.db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def enqueue_batch(self, batch_id: str, job_id: int, payload: dict[str, Any]) -> bool:
        with self.lock:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO outbox(batch_id,job_id,payload_json,created_at) VALUES(?,?,?,?)",
                (batch_id, job_id, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            self.db.commit()
            return cursor.rowcount > 0

    def due_batches(self, limit: int = 5, job_id: int | None = None) -> list[sqlite3.Row]:
        with self.lock:
            safe_limit = max(1, min(20, limit))
            if job_id is None:
                return self.db.execute(
                    "SELECT batch_id,job_id,payload_json,attempts FROM outbox WHERE next_attempt_at<=? ORDER BY created_at LIMIT ?",
                    (time.time(), safe_limit),
                ).fetchall()
            return self.db.execute(
                "SELECT batch_id,job_id,payload_json,attempts FROM outbox WHERE job_id=? AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
                (job_id, time.time(), safe_limit),
            ).fetchall()

    def mark_batch_sent(self, batch_id: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE batch_id=?", (batch_id,))
            self.db.commit()

    def mark_batch_failed(self, batch_id: str, error: str, attempts: int) -> None:
        backoff = min(300.0, max(2.0, 2.0 ** min(attempts, 7)))
        with self.lock:
            self.db.execute(
                "UPDATE outbox SET attempts=?,next_attempt_at=?,last_error=? WHERE batch_id=?",
                (attempts, time.time() + backoff, error[:1000], batch_id),
            )
            self.db.commit()

    def retry_batches_now(self) -> None:
        with self.lock:
            self.db.execute("UPDATE outbox SET next_attempt_at=0")
            self.db.commit()

    def pending_count(self, job_id: int | None = None) -> int:
        with self.lock:
            if job_id is None:
                row = self.db.execute(
                    "SELECT (SELECT COUNT(*) FROM outbox) + (SELECT COUNT(*) FROM pending_numbers) AS count"
                ).fetchone()
            else:
                row = self.db.execute(
                    "SELECT (SELECT COUNT(*) FROM outbox WHERE job_id=?) + "
                    "(SELECT COUNT(*) FROM pending_numbers WHERE job_id=?) AS count",
                    (job_id, job_id),
                ).fetchone()
        return int(row["count"])

    def add_pending_number(self, job_id: int, number: str, pattern: str | None = None) -> bool:
        with self.lock:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO pending_numbers(job_id,number,pattern,created_at) VALUES(?,?,?,?)",
                (job_id, number, pattern, time.time()),
            )
            self.db.commit()
            return cursor.rowcount > 0

    def pending_numbers(self, job_id: int, limit: int = 50) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(
                "SELECT job_id,number,pattern FROM pending_numbers WHERE job_id=? ORDER BY created_at LIMIT ?",
                (job_id, max(1, min(100, int(limit)))),
            ).fetchall()

    def remove_pending_numbers(self, job_id: int, numbers: list[str]) -> None:
        if not numbers:
            return
        with self.lock:
            self.db.executemany(
                "DELETE FROM pending_numbers WHERE job_id=? AND number=?",
                [(job_id, number) for number in numbers],
            )
            self.db.commit()

    def save_checkpoint(self, job_id: int, stats: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO job_checkpoints(job_id,stats_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET stats_json=excluded.stats_json,updated_at=excluded.updated_at",
                (job_id, json.dumps(stats, ensure_ascii=False), time.time()),
            )
            self.db.commit()

    def load_checkpoint(self, job_id: int) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT stats_json FROM job_checkpoints WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(row["stats_json"])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}
