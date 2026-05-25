"""SQLite storage for API key check results."""
import sqlite3
import os
import json
import time
from contextlib import contextmanager
from typing import Iterable

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "keys.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS keys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key         TEXT NOT NULL UNIQUE,
            provider        TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',  -- pending|checking|valid|invalid|no_quota|error
            tier            TEXT,
            rpm             INTEGER,
            tpm             INTEGER,
            extra           TEXT,   -- JSON: provider-specific info (image models, sora, models list, etc)
            error           TEXT,
            checked_at      INTEGER,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_provider ON keys(provider);
        CREATE INDEX IF NOT EXISTS idx_status ON keys(status);
        CREATE INDEX IF NOT EXISTS idx_tier ON keys(tier);

        CREATE TABLE IF NOT EXISTS vault (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key           TEXT NOT NULL UNIQUE,
            provider          TEXT NOT NULL,
            tier              TEXT,
            rpm               INTEGER,
            tpm               INTEGER,
            extra             TEXT,
            note              TEXT,
            first_verified_at INTEGER NOT NULL,
            last_verified_at  INTEGER NOT NULL,
            check_count       INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_vault_provider ON vault(provider);
        CREATE INDEX IF NOT EXISTS idx_vault_tier ON vault(tier);

        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            status          TEXT NOT NULL DEFAULT 'running',  -- running|done|cancelled
            total           INTEGER NOT NULL,
            done            INTEGER NOT NULL DEFAULT 0,
            concurrency     INTEGER NOT NULL,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );
        """)


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def now() -> int:
    return int(time.time())


def upsert_keys(keys: Iterable[str], providers: dict[str, str | None]) -> list[int]:
    """Insert new keys, return list of key IDs (existing + newly inserted)."""
    ids = []
    t = now()
    with conn() as c:
        for k in keys:
            k = k.strip()
            if not k:
                continue
            prov = providers.get(k)
            row = c.execute("SELECT id FROM keys WHERE api_key = ?", (k,)).fetchone()
            if row:
                ids.append(row["id"])
            else:
                cur = c.execute(
                    "INSERT INTO keys (api_key, provider, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
                    (k, prov, t, t),
                )
                ids.append(cur.lastrowid)
    return ids


def set_key_status(key_id: int, status: str):
    with conn() as c:
        c.execute("UPDATE keys SET status=?, updated_at=? WHERE id=?", (status, now(), key_id))


def save_result(key_id: int, result: dict):
    t = now()
    extra_json = json.dumps(result.get("extra") or {}, ensure_ascii=False)
    status = result.get("status", "error")
    with conn() as c:
        c.execute(
            """UPDATE keys SET
                status=?, tier=?, rpm=?, tpm=?, extra=?, error=?,
                checked_at=?, updated_at=?
              WHERE id=?""",
            (
                status,
                result.get("tier"),
                result.get("rpm"),
                result.get("tpm"),
                extra_json,
                result.get("error"),
                t, t, key_id,
            ),
        )
        # Auto-vault on valid status.
        if status == "valid":
            row = c.execute("SELECT api_key, provider FROM keys WHERE id=?", (key_id,)).fetchone()
            if row and row["provider"]:
                _vault_upsert_conn(
                    c, row["api_key"], row["provider"],
                    result.get("tier"), result.get("rpm"), result.get("tpm"),
                    extra_json, t,
                )


def _vault_upsert_conn(c, api_key, provider, tier, rpm, tpm, extra_json, t):
    existing = c.execute("SELECT id, check_count FROM vault WHERE api_key=?", (api_key,)).fetchone()
    if existing:
        c.execute(
            """UPDATE vault SET tier=?, rpm=?, tpm=?, extra=?, last_verified_at=?,
                check_count=check_count+1 WHERE id=?""",
            (tier, rpm, tpm, extra_json, t, existing["id"]),
        )
    else:
        c.execute(
            """INSERT INTO vault (api_key, provider, tier, rpm, tpm, extra,
                first_verified_at, last_verified_at, check_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (api_key, provider, tier, rpm, tpm, extra_json, t, t),
        )


def list_vault(provider: str | None = None, tier: str | None = None) -> list[dict]:
    sql = "SELECT * FROM vault WHERE 1=1"
    args = []
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    if tier:
        sql += " AND tier=?"
        args.append(tier)
    sql += " ORDER BY last_verified_at DESC"
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def get_vault_entries(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with conn() as c:
        rows = c.execute(f"SELECT * FROM vault WHERE id IN ({placeholders})", ids).fetchall()
        return [dict(r) for r in rows]


def delete_vault(ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with conn() as c:
        cur = c.execute(f"DELETE FROM vault WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def update_vault_note(vault_id: int, note: str):
    with conn() as c:
        c.execute("UPDATE vault SET note=? WHERE id=?", (note, vault_id))


def vault_stats() -> dict:
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM vault").fetchone()[0]
        by_prov = {r[0]: r[1] for r in c.execute(
            "SELECT provider, COUNT(*) FROM vault GROUP BY provider").fetchall()}
        by_tier = {r[0] or "?": r[1] for r in c.execute(
            "SELECT tier, COUNT(*) FROM vault GROUP BY tier").fetchall()}
    return {"total": total, "by_provider": by_prov, "by_tier": by_tier}


def get_key(key_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM keys WHERE id=?", (key_id,)).fetchone()
        return dict(row) if row else None


def list_keys(provider: str | None = None, status: str | None = None, tier: str | None = None) -> list[dict]:
    sql = "SELECT * FROM keys WHERE 1=1"
    args = []
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    if status:
        sql += " AND status=?"
        args.append(status)
    if tier:
        sql += " AND tier=?"
        args.append(tier)
    sql += " ORDER BY id DESC"
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def delete_keys(ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    with conn() as c:
        cur = c.execute(f"DELETE FROM keys WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def create_job(total: int, concurrency: int) -> int:
    t = now()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO jobs (total, concurrency, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (total, concurrency, t, t),
        )
        return cur.lastrowid


def bump_job(job_id: int, done_delta: int = 1):
    with conn() as c:
        c.execute("UPDATE jobs SET done=done+?, updated_at=? WHERE id=?", (done_delta, now(), job_id))


def finish_job(job_id: int):
    with conn() as c:
        c.execute("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (now(), job_id))


def get_job(job_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_running_jobs() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM jobs WHERE status='running' ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
