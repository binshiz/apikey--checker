"""SQLite storage for API key check results and inventory records."""
import sqlite3
import os
import json
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Callable

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "keys.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with conn() as c:
        _create_legacy_schema(c)
        _create_migration_table(c)
        _run_migrations(c)


def _create_legacy_schema(c: sqlite3.Connection):
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


def _create_migration_table(c: sqlite3.Connection):
    c.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version     INTEGER PRIMARY KEY,
        name        TEXT NOT NULL,
        applied_at  INTEGER NOT NULL
    )
    """)


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _run_migrations(c: sqlite3.Connection):
    migrations: list[Migration] = [
        (1, "inventory_foundation", _migration_inventory_foundation),
    ]
    for version, name, fn in migrations:
        row = c.execute(
            "SELECT version FROM schema_migrations WHERE version=?",
            (version,),
        ).fetchone()
        if row:
            continue
        fn(c)
        c.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, now()),
        )


def _migration_inventory_foundation(c: sqlite3.Connection):
    _create_inventory_schema(c)
    _backfill_inventory_from_legacy(c)


def _create_inventory_schema(c: sqlite3.Connection):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS suppliers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        contact     TEXT,
        note        TEXT,
        created_at  INTEGER NOT NULL,
        updated_at  INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS purchase_batches (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id   INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
        name          TEXT NOT NULL,
        source        TEXT,
        channel       TEXT,
        quantity      INTEGER,
        total_cost    REAL,
        paid_status   TEXT,
        note          TEXT,
        tags          TEXT,
        purchased_at  INTEGER,
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_batches_supplier ON purchase_batches(supplier_id);
    CREATE INDEX IF NOT EXISTS idx_batches_purchased_at ON purchase_batches(purchased_at);

    CREATE TABLE IF NOT EXISTS api_key_inventory (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        api_key               TEXT NOT NULL UNIQUE,
        key_id                INTEGER REFERENCES keys(id) ON DELETE SET NULL,
        vault_id              INTEGER REFERENCES vault(id) ON DELETE SET NULL,
        supplier_id           INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
        batch_id              INTEGER REFERENCES purchase_batches(id) ON DELETE SET NULL,
        provider              TEXT,
        stock_status          TEXT NOT NULL DEFAULT 'pending_check',
        tier                  TEXT,
        rpm                   INTEGER,
        tpm                   INTEGER,
        extra                 TEXT,
        error                 TEXT,
        note                  TEXT,
        tags                  TEXT,
        risk_flag             TEXT,
        latest_check_run_id   INTEGER,
        first_seen_at         INTEGER NOT NULL,
        last_checked_at       INTEGER,
        created_at            INTEGER NOT NULL,
        updated_at            INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_inventory_key_id ON api_key_inventory(key_id);
    CREATE INDEX IF NOT EXISTS idx_inventory_vault_id ON api_key_inventory(vault_id);
    CREATE INDEX IF NOT EXISTS idx_inventory_provider ON api_key_inventory(provider);
    CREATE INDEX IF NOT EXISTS idx_inventory_status ON api_key_inventory(stock_status);
    CREATE INDEX IF NOT EXISTS idx_inventory_tier ON api_key_inventory(tier);
    CREATE INDEX IF NOT EXISTS idx_inventory_supplier ON api_key_inventory(supplier_id);
    CREATE INDEX IF NOT EXISTS idx_inventory_batch ON api_key_inventory(batch_id);

    CREATE TABLE IF NOT EXISTS stock_movements (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_id   INTEGER NOT NULL REFERENCES api_key_inventory(id),
        movement_type  TEXT NOT NULL,
        from_status    TEXT,
        to_status      TEXT,
        quantity       INTEGER NOT NULL DEFAULT 1,
        reason         TEXT,
        metadata       TEXT,
        created_at     INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_movements_inventory ON stock_movements(inventory_id);
    CREATE INDEX IF NOT EXISTS idx_movements_type ON stock_movements(movement_type);
    CREATE INDEX IF NOT EXISTS idx_movements_created ON stock_movements(created_at);

    CREATE TABLE IF NOT EXISTS check_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_id  INTEGER REFERENCES api_key_inventory(id) ON DELETE SET NULL,
        key_id        INTEGER REFERENCES keys(id) ON DELETE SET NULL,
        provider      TEXT,
        status        TEXT NOT NULL,
        tier          TEXT,
        rpm           INTEGER,
        tpm           INTEGER,
        extra         TEXT,
        error         TEXT,
        proxy         TEXT,
        source        TEXT NOT NULL DEFAULT 'checker',
        checked_at    INTEGER NOT NULL,
        created_at    INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_check_runs_inventory ON check_runs(inventory_id);
    CREATE INDEX IF NOT EXISTS idx_check_runs_key ON check_runs(key_id);
    CREATE INDEX IF NOT EXISTS idx_check_runs_status ON check_runs(status);
    CREATE INDEX IF NOT EXISTS idx_check_runs_checked ON check_runs(checked_at);
    """)


def _backfill_inventory_from_legacy(c: sqlite3.Connection):
    rows = c.execute("SELECT * FROM keys ORDER BY id").fetchall()
    for row in rows:
        t = row["created_at"] or now()
        stock_status = _stock_status_from_check_status(row["status"])
        inventory_id, created = _ensure_inventory_conn(
            c,
            api_key=row["api_key"],
            provider=row["provider"],
            key_id=row["id"],
            stock_status=stock_status,
            tier=row["tier"],
            rpm=row["rpm"],
            tpm=row["tpm"],
            extra=row["extra"],
            error=row["error"],
            first_seen_at=row["created_at"] or t,
            last_checked_at=row["checked_at"],
            updated_at=row["updated_at"] or t,
        )
        if created:
            _insert_stock_movement_conn(
                c,
                inventory_id,
                "legacy_import",
                None,
                stock_status,
                "legacy_keys_backfill",
                {"key_id": row["id"]},
                t,
            )
        if row["checked_at"]:
            check_run_id = _insert_check_run_conn(
                c,
                inventory_id=inventory_id,
                key_id=row["id"],
                provider=row["provider"],
                status=row["status"],
                tier=row["tier"],
                rpm=row["rpm"],
                tpm=row["tpm"],
                extra=row["extra"],
                error=row["error"],
                source="legacy_keys",
                checked_at=row["checked_at"],
                created_at=row["checked_at"],
            )
            _set_latest_check_run_conn(c, inventory_id, check_run_id, row["checked_at"])

    vault_rows = c.execute("SELECT * FROM vault ORDER BY id").fetchall()
    for row in vault_rows:
        existing = c.execute(
            "SELECT id, key_id FROM api_key_inventory WHERE api_key=?",
            (row["api_key"],),
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE api_key_inventory
                   SET vault_id=COALESCE(vault_id, ?),
                       note=COALESCE(note, ?),
                       updated_at=?
                   WHERE id=?""",
                (row["id"], row["note"], row["last_verified_at"], existing["id"]),
            )
            continue

        t = row["first_verified_at"] or now()
        inventory_id, created = _ensure_inventory_conn(
            c,
            api_key=row["api_key"],
            provider=row["provider"],
            vault_id=row["id"],
            stock_status="pending_check",
            tier=row["tier"],
            rpm=row["rpm"],
            tpm=row["tpm"],
            extra=row["extra"],
            note=row["note"],
            first_seen_at=row["first_verified_at"] or t,
            last_checked_at=row["last_verified_at"],
            updated_at=row["last_verified_at"] or t,
        )
        if created:
            _insert_stock_movement_conn(
                c,
                inventory_id,
                "legacy_vault_import",
                None,
                "pending_check",
                "legacy_vault_backfill",
                {"vault_id": row["id"]},
                t,
            )
        if row["last_verified_at"]:
            check_run_id = _insert_check_run_conn(
                c,
                inventory_id=inventory_id,
                key_id=None,
                provider=row["provider"],
                status="valid",
                tier=row["tier"],
                rpm=row["rpm"],
                tpm=row["tpm"],
                extra=row["extra"],
                error=None,
                source="legacy_vault",
                checked_at=row["last_verified_at"],
                created_at=row["last_verified_at"],
            )
            _set_latest_check_run_conn(c, inventory_id, check_run_id, row["last_verified_at"])


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


def _stock_status_from_check_status(status: str | None, *, is_formal_inventory: bool = False) -> str:
    return {
        "valid": "in_stock" if is_formal_inventory else "pending_check",
        "no_quota": "no_quota",
        "invalid": "invalid",
        "error": "quarantined",
        "pending": "pending_check",
        "checking": "pending_check",
    }.get(status or "", "pending_check")


def _json_dumps(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


def _ensure_inventory_conn(
    c: sqlite3.Connection,
    *,
    api_key: str,
    provider: str | None = None,
    key_id: int | None = None,
    vault_id: int | None = None,
    supplier_id: int | None = None,
    batch_id: int | None = None,
    stock_status: str = "pending_check",
    tier: str | None = None,
    rpm: int | None = None,
    tpm: int | None = None,
    extra: str | None = None,
    error: str | None = None,
    note: str | None = None,
    tags: str | None = None,
    first_seen_at: int | None = None,
    last_checked_at: int | None = None,
    updated_at: int | None = None,
    overwrite_result_fields: bool = True,
) -> tuple[int, bool]:
    t = updated_at or now()
    first_seen = first_seen_at or t
    row = c.execute(
        "SELECT id FROM api_key_inventory WHERE api_key=?",
        (api_key,),
    ).fetchone()
    if row:
        c.execute(
            """UPDATE api_key_inventory SET
                key_id=COALESCE(key_id, ?),
                vault_id=COALESCE(vault_id, ?),
                supplier_id=COALESCE(supplier_id, ?),
                batch_id=COALESCE(batch_id, ?),
                provider=COALESCE(?, provider),
                stock_status=?,
                tier=CASE WHEN ? THEN ? ELSE tier END,
                rpm=CASE WHEN ? THEN ? ELSE rpm END,
                tpm=CASE WHEN ? THEN ? ELSE tpm END,
                extra=CASE WHEN ? THEN ? ELSE extra END,
                error=CASE WHEN ? THEN ? ELSE error END,
                note=COALESCE(note, ?),
                tags=COALESCE(tags, ?),
                last_checked_at=COALESCE(?, last_checked_at),
                updated_at=?
              WHERE id=?""",
            (
                key_id,
                vault_id,
                supplier_id,
                batch_id,
                provider,
                stock_status,
                1 if overwrite_result_fields else 0,
                tier,
                1 if overwrite_result_fields else 0,
                rpm,
                1 if overwrite_result_fields else 0,
                tpm,
                1 if overwrite_result_fields else 0,
                extra,
                1 if overwrite_result_fields else 0,
                error,
                note,
                tags,
                last_checked_at,
                t,
                row["id"],
            ),
        )
        return row["id"], False

    cur = c.execute(
        """INSERT INTO api_key_inventory (
            api_key, key_id, vault_id, supplier_id, batch_id, provider,
            stock_status, tier, rpm, tpm, extra, error, note, tags,
            first_seen_at, last_checked_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            api_key,
            key_id,
            vault_id,
            supplier_id,
            batch_id,
            provider,
            stock_status,
            tier,
            rpm,
            tpm,
            extra,
            error,
            note,
            tags,
            first_seen,
            last_checked_at,
            first_seen,
            t,
        ),
    )
    return cur.lastrowid, True


def _inventory_for_key_conn(c: sqlite3.Connection, key_id: int) -> sqlite3.Row | None:
    row = c.execute(
        """SELECT i.*
           FROM api_key_inventory i
           JOIN keys k ON k.api_key = i.api_key
           WHERE k.id=?""",
        (key_id,),
    ).fetchone()
    return row


def _insert_stock_movement_conn(
    c: sqlite3.Connection,
    inventory_id: int,
    movement_type: str,
    from_status: str | None,
    to_status: str | None,
    reason: str | None,
    metadata: dict | None,
    created_at: int,
):
    c.execute(
        """INSERT INTO stock_movements (
            inventory_id, movement_type, from_status, to_status, quantity,
            reason, metadata, created_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
        (
            inventory_id,
            movement_type,
            from_status,
            to_status,
            reason,
            _json_dumps(metadata),
            created_at,
        ),
    )


def _insert_movement_if_status_changed_conn(
    c: sqlite3.Connection,
    inventory_id: int,
    movement_type: str,
    from_status: str | None,
    to_status: str | None,
    reason: str | None,
    metadata: dict | None,
    created_at: int,
):
    if from_status == to_status:
        return
    _insert_stock_movement_conn(
        c,
        inventory_id,
        movement_type,
        from_status,
        to_status,
        reason,
        metadata,
        created_at,
    )


def _insert_check_run_conn(
    c: sqlite3.Connection,
    *,
    inventory_id: int,
    key_id: int | None,
    provider: str | None,
    status: str,
    tier: str | None,
    rpm: int | None,
    tpm: int | None,
    extra: str | None,
    error: str | None,
    source: str,
    checked_at: int,
    created_at: int,
    proxy: str | None = None,
) -> int:
    cur = c.execute(
        """INSERT INTO check_runs (
            inventory_id, key_id, provider, status, tier, rpm, tpm, extra,
            error, proxy, source, checked_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            inventory_id,
            key_id,
            provider,
            status,
            tier,
            rpm,
            tpm,
            extra,
            error,
            proxy,
            source,
            checked_at,
            created_at,
        ),
    )
    return cur.lastrowid


def _set_latest_check_run_conn(
    c: sqlite3.Connection,
    inventory_id: int,
    check_run_id: int,
    checked_at: int,
):
    c.execute(
        """UPDATE api_key_inventory
           SET latest_check_run_id=?, last_checked_at=?, updated_at=?
           WHERE id=?""",
        (check_run_id, checked_at, checked_at, inventory_id),
    )


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
                c.execute(
                    """UPDATE keys
                       SET provider=COALESCE(provider, ?),
                           status='pending',
                           updated_at=?
                       WHERE id=?""",
                    (prov, t, row["id"]),
                )
                ids.append(row["id"])
                continue

            cur = c.execute(
                "INSERT INTO keys (api_key, provider, status, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
                (k, prov, t, t),
            )
            key_id = cur.lastrowid
            ids.append(key_id)
            inventory_id, created = _ensure_inventory_conn(
                c,
                api_key=k,
                provider=prov,
                key_id=key_id,
                stock_status="pending_check",
                first_seen_at=t,
                updated_at=t,
            )
            if created:
                _insert_stock_movement_conn(
                    c,
                    inventory_id,
                    "inbound",
                    None,
                    "pending_check",
                    "key_import",
                    {"key_id": key_id},
                    t,
                )
    return ids


def set_key_status(key_id: int, status: str):
    t = now()
    with conn() as c:
        c.execute("UPDATE keys SET status=?, updated_at=? WHERE id=?", (status, t, key_id))
        row = c.execute("SELECT api_key, provider FROM keys WHERE id=?", (key_id,)).fetchone()
        if not row:
            return
        new_stock_status = _stock_status_from_check_status(status)
        current = c.execute(
            "SELECT id, stock_status FROM api_key_inventory WHERE api_key=?",
            (row["api_key"],),
        ).fetchone()
        inventory_id, created = _ensure_inventory_conn(
            c,
            api_key=row["api_key"],
            provider=row["provider"],
            key_id=key_id,
            stock_status=new_stock_status,
            updated_at=t,
            overwrite_result_fields=False,
        )
        if created:
            _insert_stock_movement_conn(
                c,
                inventory_id,
                "status_update",
                None,
                new_stock_status,
                status,
                {"key_id": key_id},
                t,
            )
        elif current:
            _insert_movement_if_status_changed_conn(
                c,
                inventory_id,
                "status_update",
                current["stock_status"],
                new_stock_status,
                status,
                {"key_id": key_id},
                t,
            )


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
        row = c.execute("SELECT api_key, provider FROM keys WHERE id=?", (key_id,)).fetchone()
        if not row:
            return

        current = c.execute(
            "SELECT id, stock_status, supplier_id, batch_id FROM api_key_inventory WHERE api_key=?",
            (row["api_key"],),
        ).fetchone()
        is_formal_inventory = bool(current and current["supplier_id"] and current["batch_id"])
        stock_status = _stock_status_from_check_status(status, is_formal_inventory=is_formal_inventory)
        inventory_id, created = _ensure_inventory_conn(
            c,
            api_key=row["api_key"],
            provider=row["provider"],
            key_id=key_id,
            stock_status=stock_status,
            tier=result.get("tier"),
            rpm=result.get("rpm"),
            tpm=result.get("tpm"),
            extra=extra_json,
            error=result.get("error"),
            last_checked_at=t,
            updated_at=t,
        )
        from_status = None if created else current["stock_status"] if current else None
        check_run_id = _insert_check_run_conn(
            c,
            inventory_id=inventory_id,
            key_id=key_id,
            provider=row["provider"],
            status=status,
            tier=result.get("tier"),
            rpm=result.get("rpm"),
            tpm=result.get("tpm"),
            extra=extra_json,
            error=result.get("error"),
            proxy=(result.get("extra") or {}).get("proxy"),
            source="checker",
            checked_at=t,
            created_at=t,
        )
        _set_latest_check_run_conn(c, inventory_id, check_run_id, t)
        _insert_movement_if_status_changed_conn(
            c,
            inventory_id,
            "check_result",
            from_status,
            stock_status,
            status,
            {"key_id": key_id, "check_run_id": check_run_id},
            t,
        )

        # Auto-vault on valid status.
        if status == "valid" and row["provider"]:
            vault_id = _vault_upsert_conn(
                c, row["api_key"], row["provider"],
                result.get("tier"), result.get("rpm"), result.get("tpm"),
                extra_json, t,
            )
            c.execute(
                "UPDATE api_key_inventory SET vault_id=?, updated_at=? WHERE id=?",
                (vault_id, t, inventory_id),
            )


def _vault_upsert_conn(c, api_key, provider, tier, rpm, tpm, extra_json, t) -> int:
    existing = c.execute("SELECT id, check_count FROM vault WHERE api_key=?", (api_key,)).fetchone()
    if existing:
        c.execute(
            """UPDATE vault SET tier=?, rpm=?, tpm=?, extra=?, last_verified_at=?,
                check_count=check_count+1 WHERE id=?""",
            (tier, rpm, tpm, extra_json, t, existing["id"]),
        )
        return existing["id"]

    cur = c.execute(
        """INSERT INTO vault (api_key, provider, tier, rpm, tpm, extra,
            first_verified_at, last_verified_at, check_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (api_key, provider, tier, rpm, tpm, extra_json, t, t),
    )
    return cur.lastrowid


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
        c.execute("UPDATE api_key_inventory SET note=? WHERE vault_id=?", (note, vault_id))


def vault_stats() -> dict:
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM vault").fetchone()[0]
        by_prov = {r[0]: r[1] for r in c.execute(
            "SELECT provider, COUNT(*) FROM vault GROUP BY provider").fetchall()}
        by_tier = {r[0] or "?": r[1] for r in c.execute(
            "SELECT tier, COUNT(*) FROM vault GROUP BY tier").fetchall()}
    return {"total": total, "by_provider": by_prov, "by_tier": by_tier}


def upsert_supplier(name: str) -> int:
    supplier_name = (name or "").strip()
    if not supplier_name:
        raise ValueError("supplier_name is required")
    t = now()
    with conn() as c:
        return _upsert_supplier_conn(c, supplier_name, t)


def _upsert_supplier_conn(c: sqlite3.Connection, name: str, t: int) -> int:
    supplier_name = (name or "").strip()
    if not supplier_name:
        raise ValueError("supplier_name is required")
    row = c.execute("SELECT id FROM suppliers WHERE name=?", (supplier_name,)).fetchone()
    if row:
        c.execute("UPDATE suppliers SET updated_at=? WHERE id=?", (t, row["id"]))
        return row["id"]
    cur = c.execute(
        "INSERT INTO suppliers (name, created_at, updated_at) VALUES (?, ?, ?)",
        (supplier_name, t, t),
    )
    return cur.lastrowid


def create_inbound_batch(
    supplier_id: int,
    quantity: int,
    total_cost: float | None = None,
    tags: str | None = None,
    note: str | None = None,
) -> dict:
    with conn() as c:
        return _create_inbound_batch_conn(
            c,
            supplier_id=supplier_id,
            quantity=quantity,
            total_cost=total_cost,
            tags=tags,
            note=note,
            t=now(),
        )


def _create_inbound_batch_conn(
    c: sqlite3.Connection,
    *,
    supplier_id: int,
    quantity: int,
    total_cost: float | None,
    tags: str | None,
    note: str | None,
    t: int,
) -> dict:
    name = f"入库批次 {datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M')}"
    cur = c.execute(
        """INSERT INTO purchase_batches (
            supplier_id, name, source, quantity, total_cost, note, tags,
            purchased_at, created_at, updated_at
        ) VALUES (?, ?, 'vault', ?, ?, ?, ?, ?, ?, ?)""",
        (supplier_id, name, quantity, total_cost, note, tags, t, t, t),
    )
    batch_id = cur.lastrowid
    supplier = c.execute("SELECT name FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    return {
        "id": batch_id,
        "name": name,
        "supplier_id": supplier_id,
        "supplier_name": supplier["name"] if supplier else None,
        "quantity": quantity,
        "total_cost": total_cost,
        "tags": tags,
        "note": note,
        "purchased_at": t,
    }


def inbound_from_vault(
    vault_ids: list[int],
    supplier_name: str,
    total_cost: float | None = None,
    tags: str | None = None,
    note: str | None = None,
) -> dict:
    if not vault_ids:
        return {"inbounded": 0, "skipped": 0, "batch": None}
    clean_supplier = (supplier_name or "").strip()
    if not clean_supplier:
        raise ValueError("supplier_name is required")

    t = now()
    placeholders = ",".join("?" for _ in vault_ids)
    with conn() as c:
        rows = c.execute(
            f"SELECT * FROM vault WHERE id IN ({placeholders}) ORDER BY id",
            vault_ids,
        ).fetchall()
        if not rows:
            return {"inbounded": 0, "skipped": len(vault_ids), "batch": None}

        inbound_rows = []
        skipped = len(vault_ids) - len(rows)
        for row in rows:
            inventory = c.execute(
                "SELECT id, supplier_id, batch_id, stock_status FROM api_key_inventory WHERE api_key=?",
                (row["api_key"],),
            ).fetchone()
            if inventory and inventory["supplier_id"] and inventory["batch_id"]:
                skipped += 1
                continue
            inbound_rows.append((row, inventory))

        if not inbound_rows:
            return {"inbounded": 0, "skipped": skipped, "batch": None}

        supplier_id = _upsert_supplier_conn(c, clean_supplier, t)
        batch = _create_inbound_batch_conn(
            c,
            supplier_id=supplier_id,
            quantity=len(rows),
            total_cost=total_cost,
            tags=tags,
            note=note,
            t=t,
        )

        inbounded = 0
        for row, inventory in inbound_rows:
            inventory_id, created = _ensure_inventory_conn(
                c,
                api_key=row["api_key"],
                provider=row["provider"],
                vault_id=row["id"],
                supplier_id=supplier_id,
                batch_id=batch["id"],
                stock_status="in_stock",
                tier=row["tier"],
                rpm=row["rpm"],
                tpm=row["tpm"],
                extra=row["extra"],
                note=note or row["note"],
                tags=tags,
                first_seen_at=row["first_verified_at"],
                last_checked_at=row["last_verified_at"],
                updated_at=t,
            )
            from_status = None if created else (inventory["stock_status"] if inventory and "stock_status" in inventory.keys() else None)
            _insert_stock_movement_conn(
                c,
                inventory_id,
                "inbound_from_vault",
                from_status,
                "in_stock",
                "vault_selected_inbound",
                {"vault_id": row["id"], "batch_id": batch["id"], "supplier_id": supplier_id},
                t,
            )
            inbounded += 1

        return {"inbounded": inbounded, "skipped": skipped, "batch": batch}


def list_inventory(
    provider: str | None = None,
    stock_status: str | None = None,
    tier: str | None = None,
    supplier_id: int | None = None,
    batch_id: int | None = None,
    risk_flag: str | None = None,
) -> list[dict]:
    sql = """
        SELECT
            i.*,
            s.name AS supplier_name,
            b.name AS batch_name,
            b.total_cost AS batch_total_cost,
            b.quantity AS batch_quantity,
            b.tags AS batch_tags,
            b.note AS batch_note,
            b.purchased_at AS purchased_at,
            CASE
                WHEN b.total_cost IS NOT NULL AND b.quantity IS NOT NULL AND b.quantity > 0
                THEN b.total_cost / b.quantity
                ELSE NULL
            END AS unit_cost,
            cr.status AS latest_check_status,
            cr.checked_at AS latest_check_at
        FROM api_key_inventory i
        JOIN suppliers s ON s.id = i.supplier_id
        JOIN purchase_batches b ON b.id = i.batch_id
        LEFT JOIN check_runs cr ON cr.id = i.latest_check_run_id
        WHERE i.supplier_id IS NOT NULL AND i.batch_id IS NOT NULL
    """
    args = []
    if provider:
        sql += " AND i.provider=?"
        args.append(provider)
    if stock_status:
        sql += " AND i.stock_status=?"
        args.append(stock_status)
    if tier:
        sql += " AND i.tier=?"
        args.append(tier)
    if supplier_id:
        sql += " AND i.supplier_id=?"
        args.append(supplier_id)
    if batch_id:
        sql += " AND i.batch_id=?"
        args.append(batch_id)
    if risk_flag:
        if risk_flag == "__empty__":
            sql += " AND (i.risk_flag IS NULL OR i.risk_flag='')"
        else:
            sql += " AND i.risk_flag=?"
            args.append(risk_flag)
    sql += " ORDER BY b.purchased_at DESC, i.id DESC"
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
        return [dict(r) for r in rows]


def _formal_inventory_select_sql() -> str:
    return """
        SELECT
            i.*,
            s.name AS supplier_name,
            b.name AS batch_name,
            b.total_cost AS batch_total_cost,
            b.quantity AS batch_quantity,
            b.tags AS batch_tags,
            b.note AS batch_note,
            b.purchased_at AS purchased_at,
            CASE
                WHEN b.total_cost IS NOT NULL AND b.quantity IS NOT NULL AND b.quantity > 0
                THEN b.total_cost / b.quantity
                ELSE NULL
            END AS unit_cost,
            cr.status AS latest_check_status,
            cr.checked_at AS latest_check_at
        FROM api_key_inventory i
        JOIN suppliers s ON s.id = i.supplier_id
        JOIN purchase_batches b ON b.id = i.batch_id
        LEFT JOIN check_runs cr ON cr.id = i.latest_check_run_id
    """


def get_inventory_detail(inventory_id: int) -> dict | None:
    with conn() as c:
        row = c.execute(
            _formal_inventory_select_sql() + """
            WHERE i.id=? AND i.supplier_id IS NOT NULL AND i.batch_id IS NOT NULL
            """,
            (inventory_id,),
        ).fetchone()
        if not row:
            return None
        check_runs = c.execute(
            """SELECT id, provider, status, tier, rpm, tpm, extra, error, proxy,
                      source, checked_at, created_at
               FROM check_runs
               WHERE inventory_id=?
               ORDER BY checked_at DESC, id DESC
               LIMIT 20""",
            (inventory_id,),
        ).fetchall()
        movements = c.execute(
            """SELECT id, movement_type, from_status, to_status, quantity,
                      reason, metadata, created_at
               FROM stock_movements
               WHERE inventory_id=?
               ORDER BY created_at DESC, id DESC
               LIMIT 50""",
            (inventory_id,),
        ).fetchall()
        return {
            "item": dict(row),
            "check_runs": [dict(r) for r in check_runs],
            "movements": [dict(r) for r in movements],
        }


def update_inventory_meta(
    inventory_id: int,
    *,
    note: str | None = None,
    tags: str | None = None,
    risk_flag: str | None = None,
) -> bool:
    t = now()
    clean_note = (note or "").strip() or None
    clean_tags = (tags or "").strip() or None
    clean_risk = (risk_flag or "").strip() or None
    with conn() as c:
        cur = c.execute(
            """UPDATE api_key_inventory
               SET note=?, tags=?, risk_flag=?, updated_at=?
               WHERE id=? AND supplier_id IS NOT NULL AND batch_id IS NOT NULL""",
            (clean_note, clean_tags, clean_risk, t, inventory_id),
        )
        return cur.rowcount > 0


def _target_status_for_inventory_action(action: str) -> str:
    targets = {
        "reserve": "reserved",
        "quarantine": "quarantined",
        "archive": "archived",
        "restore_to_stock": "in_stock",
    }
    if action not in targets:
        raise ValueError(f"unsupported inventory action: {action}")
    return targets[action]


def _can_apply_inventory_action(row: sqlite3.Row, action: str, target_status: str) -> bool:
    current = row["stock_status"]
    if current == target_status:
        return False
    if action == "reserve":
        return current == "in_stock"
    if action == "restore_to_stock":
        return current != "sold" and row["latest_check_status"] == "valid"
    if action in ("quarantine", "archive"):
        return current != "sold"
    return False


def update_inventory_status(ids: list[int], action: str, reason: str | None = None) -> dict:
    if not ids:
        return {"updated": 0, "skipped": 0}
    target_status = _target_status_for_inventory_action(action)
    placeholders = ",".join("?" for _ in ids)
    t = now()
    updated = 0
    skipped = 0
    with conn() as c:
        rows = c.execute(
            _formal_inventory_select_sql() + f"""
            WHERE i.id IN ({placeholders})
              AND i.supplier_id IS NOT NULL
              AND i.batch_id IS NOT NULL
            ORDER BY i.id
            """,
            ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        for inventory_id in ids:
            row = by_id.get(inventory_id)
            if not row or not _can_apply_inventory_action(row, action, target_status):
                skipped += 1
                continue
            c.execute(
                "UPDATE api_key_inventory SET stock_status=?, updated_at=? WHERE id=?",
                (target_status, t, inventory_id),
            )
            _insert_stock_movement_conn(
                c,
                inventory_id,
                action,
                row["stock_status"],
                target_status,
                reason or f"manual_{action}",
                {"action": action},
                t,
            )
            updated += 1
    return {"updated": updated, "skipped": skipped}


def prepare_inventory_recheck(ids: list[int]) -> dict:
    if not ids:
        return {"key_ids": [], "queued": 0, "skipped": 0}
    placeholders = ",".join("?" for _ in ids)
    t = now()
    key_ids: list[int] = []
    skipped = 0
    with conn() as c:
        rows = c.execute(
            f"""SELECT *
                FROM api_key_inventory
                WHERE id IN ({placeholders})
                  AND supplier_id IS NOT NULL
                  AND batch_id IS NOT NULL
                ORDER BY id""",
            ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        for inventory_id in ids:
            row = by_id.get(inventory_id)
            if not row or row["stock_status"] == "archived":
                skipped += 1
                continue

            key_row = c.execute(
                "SELECT id FROM keys WHERE api_key=?",
                (row["api_key"],),
            ).fetchone()
            if key_row:
                key_id = key_row["id"]
                c.execute(
                    "UPDATE keys SET status='pending', provider=COALESCE(provider, ?), updated_at=? WHERE id=?",
                    (row["provider"], t, key_id),
                )
            else:
                cur = c.execute(
                    """INSERT INTO keys (api_key, provider, status, created_at, updated_at)
                       VALUES (?, ?, 'pending', ?, ?)""",
                    (row["api_key"], row["provider"], t, t),
                )
                key_id = cur.lastrowid

            c.execute(
                """UPDATE api_key_inventory
                   SET key_id=?, stock_status='pending_check', updated_at=?
                   WHERE id=?""",
                (key_id, t, inventory_id),
            )
            _insert_movement_if_status_changed_conn(
                c,
                inventory_id,
                "inventory_recheck",
                row["stock_status"],
                "pending_check",
                "manual_recheck",
                {"key_id": key_id},
                t,
            )
            key_ids.append(key_id)
    return {"key_ids": key_ids, "queued": len(key_ids), "skipped": skipped}


def export_inventory_entries(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with conn() as c:
        rows = c.execute(
            _formal_inventory_select_sql() + f"""
            WHERE i.id IN ({placeholders})
              AND i.supplier_id IS NOT NULL
              AND i.batch_id IS NOT NULL
            ORDER BY i.id
            """,
            ids,
        ).fetchall()
        return [dict(r) for r in rows]


def inventory_stats() -> dict:
    with conn() as c:
        total = c.execute(
            """SELECT COUNT(*) FROM api_key_inventory
               WHERE supplier_id IS NOT NULL AND batch_id IS NOT NULL"""
        ).fetchone()[0]
        by_status = {r[0] or "?": r[1] for r in c.execute(
            """SELECT stock_status, COUNT(*) FROM api_key_inventory
               WHERE supplier_id IS NOT NULL AND batch_id IS NOT NULL
               GROUP BY stock_status"""
        ).fetchall()}
        by_provider = {r[0] or "?": r[1] for r in c.execute(
            """SELECT provider, COUNT(*) FROM api_key_inventory
               WHERE supplier_id IS NOT NULL AND batch_id IS NOT NULL
               GROUP BY provider"""
        ).fetchall()}
        by_risk_flag = {r[0] or "none": r[1] for r in c.execute(
            """SELECT COALESCE(NULLIF(risk_flag, ''), 'none') AS risk_flag, COUNT(*)
               FROM api_key_inventory
               WHERE supplier_id IS NOT NULL AND batch_id IS NOT NULL
               GROUP BY COALESCE(NULLIF(risk_flag, ''), 'none')"""
        ).fetchall()}
        suppliers = [dict(r) for r in c.execute(
            """SELECT s.id, s.name, COUNT(i.id) AS count
               FROM suppliers s
               JOIN api_key_inventory i ON i.supplier_id = s.id
               WHERE i.batch_id IS NOT NULL
               GROUP BY s.id, s.name
               ORDER BY s.name"""
        ).fetchall()]
        batches = [dict(r) for r in c.execute(
            """SELECT b.id, b.name, b.supplier_id, COUNT(i.id) AS count
               FROM purchase_batches b
               JOIN api_key_inventory i ON i.batch_id = b.id
               GROUP BY b.id, b.name, b.supplier_id
               ORDER BY b.purchased_at DESC"""
        ).fetchall()]
        risk_flags = [dict(r) for r in c.execute(
            """SELECT risk_flag, COUNT(*) AS count
               FROM api_key_inventory
               WHERE supplier_id IS NOT NULL
                 AND batch_id IS NOT NULL
                 AND risk_flag IS NOT NULL
                 AND risk_flag != ''
               GROUP BY risk_flag
               ORDER BY risk_flag"""
        ).fetchall()]
    return {
        "total": total,
        "by_status": by_status,
        "by_provider": by_provider,
        "by_risk_flag": by_risk_flag,
        "suppliers": suppliers,
        "batches": batches,
        "risk_flags": risk_flags,
    }


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
    sql += " ORDER BY updated_at DESC, checked_at DESC, id DESC"
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
