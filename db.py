"""SQLite storage for API key check results and inventory records."""
import sqlite3
import os
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterable, Callable

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "keys.db")


class InventoryConflictError(ValueError):
    """Raised when an atomic inventory sale/return cannot be applied."""

    def __init__(self, message: str, *, conflicts: dict[int, str] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or {}


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
        (2, "inventory_sales_and_check_state", _migration_inventory_sales_v2),
        (3, "job_detail_progress", _migration_job_detail_progress),
    ]
    for version, name, fn in migrations:
        row = c.execute(
            "SELECT version FROM schema_migrations WHERE version=?",
            (version,),
        ).fetchone()
        if row and version != 2:
            continue
        # v2 is deliberately reconciled on every startup.  Every operation in
        # the migration is idempotent, so a database left with only some of
        # the new columns/tables (including one with a prematurely inserted
        # migration marker) heals itself on the next init_db().
        fn(c)
        c.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, now()),
        )


def _migration_inventory_foundation(c: sqlite3.Connection):
    _create_inventory_schema(c)
    _backfill_inventory_from_legacy(c)


def _table_columns(c: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column_if_missing(
    c: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
):
    if column not in _table_columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


_SOCKS_PROXY_URL = re.compile(r"\bsocks5h?://[^\s\"']+", re.IGNORECASE)
_CREDENTIAL_URL = re.compile(
    r"\b(?:https?|socks5h?)://[^\s/@:\"']+:[^\s/@\"']+@[^\s\"']+",
    re.IGNORECASE,
)
_PROXY_EXTRA_KEYS = {"proxy", "proxy_url", "proxy_uri"}


def _redact_proxy_urls(value: str | None) -> str | None:
    if value is None:
        return None
    redacted = _CREDENTIAL_URL.sub("[proxy redacted]", str(value))
    return _SOCKS_PROXY_URL.sub("[proxy redacted]", redacted)


def _sanitize_check_extra(value):
    """Remove proxy locations from provider results before persistence."""
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            key_name = str(key)
            lowered = key_name.lower()
            if lowered in _PROXY_EXTRA_KEYS:
                continue
            if lowered == "proxy_dead" and isinstance(item, str):
                sanitized[key_name] = True
                continue
            sanitized[key_name] = _sanitize_check_extra(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_sanitize_check_extra(item) for item in value]
    if isinstance(value, str):
        return _redact_proxy_urls(value)
    return value


def _scrub_persisted_proxy_data(c: sqlite3.Connection):
    """Idempotently remove legacy proxy endpoints without exposing row data."""
    proxy_field_pattern = re.compile(r'"(?:proxy|proxy_url|proxy_uri|proxy_dead)"\s*:', re.I)
    for table in ("keys", "vault", "api_key_inventory", "check_runs"):
        columns = _table_columns(c, table)
        if "extra" not in columns:
            continue
        select_columns = "id, extra" + (", error" if "error" in columns else "")
        where_clause = (
            "extra IS NOT NULL OR error IS NOT NULL"
            if "error" in columns
            else "extra IS NOT NULL"
        )
        rows = c.execute(
            f"SELECT {select_columns} FROM {table} WHERE {where_clause}"
        ).fetchall()
        for row in rows:
            raw_extra = row["extra"]
            if raw_extra is not None:
                try:
                    parsed = json.loads(raw_extra)
                except (TypeError, ValueError, json.JSONDecodeError):
                    clean_extra = _redact_proxy_urls(str(raw_extra))
                    if proxy_field_pattern.search(clean_extra or ""):
                        clean_extra = "{}"
                else:
                    clean_extra = json.dumps(
                        _sanitize_check_extra(parsed),
                        ensure_ascii=False,
                    )
                if clean_extra != raw_extra:
                    c.execute(
                        f"UPDATE {table} SET extra=? WHERE id=?",
                        (clean_extra, row["id"]),
                    )

            if "error" in columns and row["error"] is not None:
                clean_error = _redact_proxy_urls(row["error"])
                if clean_error != row["error"]:
                    c.execute(
                        f"UPDATE {table} SET error=? WHERE id=?",
                        (clean_error, row["id"]),
                    )
    c.execute("UPDATE check_runs SET proxy=NULL WHERE proxy IS NOT NULL")


def _stock_movements_has_sale_fk(c: sqlite3.Connection) -> bool:
    return any(
        row["from"] == "sale_id"
        and row["table"] == "sales"
        and row["to"] == "id"
        and row["on_delete"].upper() == "SET NULL"
        for row in c.execute("PRAGMA foreign_key_list(stock_movements)").fetchall()
    )


def _rebuild_stock_movements_with_sale_fk(c: sqlite3.Connection):
    """Repair databases created with the early v2 sale_id column lacking an FK."""
    c.execute("DROP TABLE IF EXISTS stock_movements_v2_rebuild")
    c.execute("""
        CREATE TABLE stock_movements_v2_rebuild (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id   INTEGER NOT NULL REFERENCES api_key_inventory(id),
            movement_type  TEXT NOT NULL,
            from_status    TEXT,
            to_status      TEXT,
            quantity       INTEGER NOT NULL DEFAULT 1,
            reason         TEXT,
            metadata       TEXT,
            created_at     INTEGER NOT NULL,
            sale_id        INTEGER REFERENCES sales(id) ON DELETE SET NULL
        )
    """)
    c.execute("""
        INSERT INTO stock_movements_v2_rebuild (
            id, inventory_id, movement_type, from_status, to_status,
            quantity, reason, metadata, created_at, sale_id
        )
        SELECT sm.id, sm.inventory_id, sm.movement_type, sm.from_status,
               sm.to_status, sm.quantity, sm.reason, sm.metadata, sm.created_at,
               CASE
                   WHEN sm.sale_id IS NULL
                     OR EXISTS (SELECT 1 FROM sales s WHERE s.id=sm.sale_id)
                   THEN sm.sale_id
                   ELSE NULL
               END
          FROM stock_movements sm
         ORDER BY sm.id
    """)
    c.execute("DROP TABLE stock_movements")
    c.execute("ALTER TABLE stock_movements_v2_rebuild RENAME TO stock_movements")
    c.execute("CREATE INDEX idx_movements_inventory ON stock_movements(inventory_id)")
    c.execute("CREATE INDEX idx_movements_type ON stock_movements(movement_type)")
    c.execute("CREATE INDEX idx_movements_created ON stock_movements(created_at)")


def _migration_inventory_sales_v2(c: sqlite3.Connection):
    # Create the independent tables first so the movement FK can reference
    # sales even when recovering a partially-applied migration.
    c.executescript("""
    CREATE TABLE IF NOT EXISTS sales (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        inventory_id       INTEGER NOT NULL REFERENCES api_key_inventory(id),
        buyer              TEXT NOT NULL,
        unit_price_minor   INTEGER CHECK(unit_price_minor IS NULL OR unit_price_minor >= 0),
        currency           TEXT NOT NULL DEFAULT 'CNY',
        external_ref       TEXT,
        note               TEXT,
        status             TEXT NOT NULL DEFAULT 'sold' CHECK(status IN ('sold', 'returned')),
        sold_at            INTEGER NOT NULL,
        returned_at        INTEGER,
        return_reason      TEXT,
        created_at         INTEGER NOT NULL,
        updated_at         INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_sales_inventory ON sales(inventory_id);
    CREATE INDEX IF NOT EXISTS idx_sales_status ON sales(status);
    CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);

    CREATE TABLE IF NOT EXISTS audit_logs (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        actor        TEXT NOT NULL DEFAULT 'admin',
        ip_address   TEXT,
        action       TEXT NOT NULL,
        target_type  TEXT NOT NULL,
        target_id    INTEGER,
        metadata     TEXT NOT NULL DEFAULT '{}',
        created_at   INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
    CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_logs(target_type, target_id);
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
    """)

    _add_column_if_missing(
        c,
        "api_key_inventory",
        "current_check_status",
        "TEXT NOT NULL DEFAULT 'pending'",
    )
    _add_column_if_missing(
        c,
        "stock_movements",
        "sale_id",
        "INTEGER REFERENCES sales(id) ON DELETE SET NULL",
    )
    if not _stock_movements_has_sale_fk(c):
        _rebuild_stock_movements_with_sale_fk(c)
    _add_column_if_missing(
        c,
        "jobs",
        "mode",
        "TEXT NOT NULL DEFAULT 'quick'",
    )

    # Use the persisted latest run as source of truth.  The legacy keys row is
    # the fallback for inventories whose v1 backfill had no check run.
    c.execute("""
        UPDATE api_key_inventory
           SET current_check_status = COALESCE(
               (SELECT cr.status
                  FROM check_runs cr
                 WHERE cr.id = api_key_inventory.latest_check_run_id),
               (SELECT k.status
                  FROM keys k
                 WHERE k.id = api_key_inventory.key_id),
               CASE stock_status
                   WHEN 'in_stock' THEN 'valid'
                   WHEN 'no_quota' THEN 'no_quota'
                   WHEN 'invalid' THEN 'invalid'
                   ELSE 'pending'
               END
           )
         WHERE current_check_status IS NULL
            OR current_check_status = ''
            OR (
                current_check_status = 'pending'
                AND stock_status != 'returned'
                AND latest_check_run_id IS NOT NULL
                AND COALESCE(
                    (SELECT k.status
                       FROM keys k
                      WHERE k.id = api_key_inventory.key_id),
                    ''
                ) NOT IN ('pending', 'checking')
            )
    """)
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_inventory_check_status "
        "ON api_key_inventory(current_check_status)"
    )
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sales_one_active_sold
        ON sales(inventory_id)
        WHERE status='sold'
    """)
    _scrub_persisted_proxy_data(c)


def _migration_job_detail_progress(c: sqlite3.Connection):
    """Add backward-compatible region/stage progress for long deep checks."""
    _add_column_if_missing(c, "jobs", "detail_total", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(c, "jobs", "detail_done", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(c, "jobs", "detail_label", "TEXT")


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
        current_check_status  TEXT NOT NULL DEFAULT 'pending',
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
            current_check_status=row["status"],
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
            current_check_status="valid",
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


def _is_callable_valid_check(
    provider: str | None,
    status: str | None,
    extra: dict | str | None,
) -> bool:
    """Require runtime proof for providers whose discovery APIs over-report access."""
    if status != "valid":
        return False
    if provider not in {
        "aws_bedrock",
        "azure_openai",
        "gcp_service_account",
        "openrouter",
    }:
        return True
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (TypeError, ValueError):
            return False
    if provider == "gcp_service_account":
        return (
            isinstance(extra, dict)
            and extra.get("token_exchange") == "success"
            and extra.get("model_invocation_verification") == "success"
            and isinstance(extra.get("supported_models"), list)
            and bool(extra["supported_models"])
        )
    if provider == "openrouter":
        return (
            isinstance(extra, dict)
            and extra.get("credential_status") == "valid"
            and extra.get("invocation_verification") == "success"
        )
    if not isinstance(extra, dict) or extra.get("invocation_verification") != "success":
        return False
    if provider == "azure_openai":
        verified_targets = extra.get("verified_callable_targets")
        return isinstance(verified_targets, list) and bool(verified_targets)
    summary = extra.get("model_summary")
    return (
        isinstance(summary, dict)
        and isinstance(summary.get("successful_regions"), list)
        and bool(summary["successful_regions"])
    )


def _json_dumps(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False)


_SENSITIVE_METADATA_FIELD = re.compile(
    r"(^|_)(api_?key|secret|credential|password|token)(_|$)",
    re.IGNORECASE,
)
_AWS_ACCESS_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_AWS_CREDENTIAL_PAIR = re.compile(
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\s*\|\s*[A-Za-z0-9/+=]{40,}(?=$|\s|[,;}])"
)
_COMMON_API_KEY = re.compile(
    r"\b(?:sk-or-v1-[a-f0-9]{64}|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"sk-ant-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,})\b"
)


def _sanitize_metadata(value):
    """Return JSON-safe metadata with credentials removed.

    Audit and movement metadata is intentionally descriptive, never a secret
    transport.  Sensitive field names are redacted recursively and AWS access
    IDs embedded in free text are masked as a final guardrail.
    """
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_METADATA_FIELD.search(str(key))
                else _sanitize_metadata(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    if isinstance(value, str):
        value = _AWS_CREDENTIAL_PAIR.sub("[REDACTED_AWS_CREDENTIAL]", value)
        value = _AWS_ACCESS_ID.sub("AWS_ACCESS_KEY_REDACTED", value)
        return _COMMON_API_KEY.sub("[REDACTED_API_KEY]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _metadata_dumps(value: dict | None) -> str:
    return json.dumps(_sanitize_metadata(value or {}), ensure_ascii=False)


def _insert_audit_conn(
    c: sqlite3.Connection,
    *,
    action: str,
    target_type: str,
    target_id: int | None = None,
    metadata: dict | None = None,
    actor: str = "admin",
    ip_address: str | None = None,
    created_at: int | None = None,
) -> int:
    clean_action = (action or "").strip()
    clean_target_type = (target_type or "").strip()
    if not clean_action:
        raise ValueError("audit action is required")
    if not clean_target_type:
        raise ValueError("audit target_type is required")
    cur = c.execute(
        """INSERT INTO audit_logs (
               actor, ip_address, action, target_type, target_id, metadata, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            (actor or "admin").strip() or "admin",
            (ip_address or "").strip() or None,
            clean_action,
            clean_target_type,
            target_id,
            _metadata_dumps(metadata),
            created_at or now(),
        ),
    )
    return cur.lastrowid


def record_audit(
    action: str,
    target_type: str,
    target_id: int | None = None,
    metadata: dict | None = None,
    actor: str = "admin",
    ip_address: str | None = None,
) -> int:
    with conn() as c:
        return _insert_audit_conn(
            c,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
            actor=actor,
            ip_address=ip_address,
        )


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
    current_check_status: str = "pending",
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
                current_check_status=?,
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
                current_check_status,
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
            stock_status, current_check_status, tier, rpm, tpm, extra, error, note, tags,
            first_seen_at, last_checked_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            api_key,
            key_id,
            vault_id,
            supplier_id,
            batch_id,
            provider,
            stock_status,
            current_check_status,
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
    sale_id: int | None = None,
):
    if "sale_id" not in _table_columns(c, "stock_movements"):
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
                _metadata_dumps(metadata),
                created_at,
            ),
        )
        return
    c.execute(
        """INSERT INTO stock_movements (
            inventory_id, movement_type, from_status, to_status, quantity,
            reason, metadata, created_at, sale_id
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)""",
        (
            inventory_id,
            movement_type,
            from_status,
            to_status,
            reason,
            _metadata_dumps(metadata),
            created_at,
            sale_id,
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
                c.execute(
                    """UPDATE api_key_inventory
                       SET key_id=COALESCE(key_id, ?),
                           provider=COALESCE(provider, ?),
                           current_check_status='pending',
                           updated_at=?
                       WHERE api_key=?""",
                    (row["id"], prov, t, k),
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
                current_check_status="pending",
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
        current = c.execute(
            "SELECT id FROM api_key_inventory WHERE api_key=?",
            (row["api_key"],),
        ).fetchone()
        if current:
            c.execute(
                """UPDATE api_key_inventory
                   SET key_id=COALESCE(key_id, ?),
                       provider=COALESCE(provider, ?),
                       current_check_status=?,
                       updated_at=?
                   WHERE id=?""",
                (key_id, row["provider"], status, t, current["id"]),
            )
        else:
            _ensure_inventory_conn(
                c,
                api_key=row["api_key"],
                provider=row["provider"],
                key_id=key_id,
                stock_status="pending_check",
                current_check_status=status,
                updated_at=t,
                overwrite_result_fields=False,
            )


def save_result(key_id: int, result: dict, source: str = "checker"):
    t = now()
    extra_json = json.dumps(
        _sanitize_check_extra(result.get("extra") or {}),
        ensure_ascii=False,
    )
    clean_error = _redact_proxy_urls(result.get("error"))
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
                clean_error,
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
        protected_statuses = {"reserved", "sold", "returned", "quarantined", "archived"}
        if current and current["stock_status"] in protected_statuses:
            stock_status = current["stock_status"]
        else:
            stock_status = _stock_status_from_check_status(
                status,
                is_formal_inventory=is_formal_inventory,
            )
        inventory_id, created = _ensure_inventory_conn(
            c,
            api_key=row["api_key"],
            provider=row["provider"],
            key_id=key_id,
            stock_status=stock_status,
            current_check_status=status,
            tier=result.get("tier"),
            rpm=result.get("rpm"),
            tpm=result.get("tpm"),
            extra=extra_json,
            error=clean_error,
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
            error=clean_error,
            proxy=None,
            source=source or "checker",
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

        # Auto-vault only callable results. Bedrock model discovery and STS
        # validation alone are insufficient proof that runtime inference works.
        if row["provider"] and _is_callable_valid_check(
            row["provider"],
            status,
            result.get("extra"),
        ):
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


def list_vault(
    provider: str | None = None,
    tier: str | None = None,
    legacy_sale_candidate: bool = False,
) -> list[dict]:
    sql = """SELECT v.*,
                    k.status AS latest_check_status,
                    k.extra AS latest_check_extra,
                    CASE WHEN instr(COALESCE(v.note, ''), '售出') > 0 THEN 1 ELSE 0 END
                        AS legacy_sale_candidate,
                    CASE
                        WHEN i.supplier_id IS NOT NULL AND i.batch_id IS NOT NULL THEN 1
                        ELSE 0
                     END AS is_in_inventory,
                    CASE
                        WHEN i.supplier_id IS NOT NULL AND i.batch_id IS NOT NULL
                        THEN i.id
                        ELSE NULL
                     END AS inventory_id,
                    CASE
                        WHEN i.supplier_id IS NOT NULL AND i.batch_id IS NOT NULL
                        THEN i.stock_status
                        ELSE NULL
                     END AS inventory_status
             FROM vault v
             LEFT JOIN api_key_inventory i ON i.api_key = v.api_key
             LEFT JOIN keys k ON k.api_key = v.api_key
             WHERE 1=1"""
    args = []
    if provider:
        sql += " AND v.provider=?"
        args.append(provider)
    if tier:
        sql += " AND v.tier=?"
        args.append(tier)
    if legacy_sale_candidate:
        sql += " AND instr(COALESCE(v.note, ''), '售出') > 0"
    sql += " ORDER BY v.last_verified_at DESC"
    with conn() as c:
        rows = c.execute(sql, args).fetchall()
        results = []
        for row in rows:
            result = dict(row)
            latest_extra = result.pop("latest_check_extra", None)
            result["is_callable"] = int(_is_callable_valid_check(
                result.get("provider"),
                result.get("latest_check_status"),
                latest_extra,
            ))
            results.append(result)
        return results


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
        if cur.rowcount:
            _insert_audit_conn(
                c,
                action="vault_delete",
                target_type="vault",
                metadata={"requested_ids": _unique_ids(ids), "deleted": cur.rowcount},
            )
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
        legacy_sale_candidates = c.execute(
            """SELECT COUNT(*) FROM vault
               WHERE instr(COALESCE(note, ''), '售出') > 0"""
        ).fetchone()[0]
    return {
        "total": total,
        "by_provider": by_prov,
        "by_tier": by_tier,
        "legacy_sale_candidates": legacy_sale_candidates,
    }


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
            # The keys table is the source of truth for the current check state.
            # Vault-only legacy rows and stale/failed checks must be revalidated
            # before they can enter formal inventory.
            key_state = c.execute(
                """SELECT id, provider, status, tier, rpm, tpm, extra, error, checked_at
                   FROM keys WHERE api_key=?""",
                (row["api_key"],),
            ).fetchone()
            if not key_state or not _is_callable_valid_check(
                key_state["provider"],
                key_state["status"],
                key_state["extra"],
            ):
                skipped += 1
                continue
            inbound_rows.append((row, inventory, key_state))

        if not inbound_rows:
            return {"inbounded": 0, "skipped": skipped, "batch": None}

        supplier_id = _upsert_supplier_conn(c, clean_supplier, t)
        batch = _create_inbound_batch_conn(
            c,
            supplier_id=supplier_id,
            quantity=len(inbound_rows),
            total_cost=total_cost,
            tags=tags,
            note=note,
            t=t,
        )

        inbounded = 0
        for row, inventory, key_state in inbound_rows:
            check_status = key_state["status"] if key_state else "pending"
            stock_status = _stock_status_from_check_status(
                check_status,
                is_formal_inventory=True,
            )
            inventory_id, created = _ensure_inventory_conn(
                c,
                api_key=row["api_key"],
                provider=(key_state["provider"] if key_state else None) or row["provider"],
                key_id=key_state["id"] if key_state else None,
                vault_id=row["id"],
                supplier_id=supplier_id,
                batch_id=batch["id"],
                stock_status=stock_status,
                current_check_status=check_status,
                tier=key_state["tier"] if key_state else row["tier"],
                rpm=key_state["rpm"] if key_state else row["rpm"],
                tpm=key_state["tpm"] if key_state else row["tpm"],
                extra=key_state["extra"] if key_state else row["extra"],
                error=key_state["error"] if key_state else None,
                note=note or row["note"],
                tags=tags,
                first_seen_at=row["first_verified_at"],
                last_checked_at=(key_state["checked_at"] if key_state else None),
                updated_at=t,
            )
            from_status = None if created else (inventory["stock_status"] if inventory and "stock_status" in inventory.keys() else None)
            _insert_stock_movement_conn(
                c,
                inventory_id,
                "inbound_from_vault",
                from_status,
                stock_status,
                "vault_selected_inbound",
                {
                    "vault_id": row["id"],
                    "batch_id": batch["id"],
                    "supplier_id": supplier_id,
                    "current_check_status": check_status,
                },
                t,
            )
            inbounded += 1

        _insert_audit_conn(
            c,
            action="inventory_inbound",
            target_type="purchase_batch",
            target_id=batch["id"],
            metadata={
                "supplier_id": supplier_id,
                "quantity": inbounded,
                "skipped": skipped,
            },
            created_at=t,
        )

        return {"inbounded": inbounded, "skipped": skipped, "batch": batch}


def list_inventory(
    provider: str | None = None,
    stock_status: str | None = None,
    tier: str | None = None,
    supplier_id: int | None = None,
    batch_id: int | None = None,
    risk_flag: str | None = None,
    sale_view: str = "all",
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
            i.current_check_status AS latest_check_status,
            cr.checked_at AS latest_check_at,
            sl.id AS sale_id,
            sl.status AS sale_status,
            sl.buyer AS buyer,
            sl.unit_price_minor AS unit_price_minor,
            sl.currency AS currency,
            sl.external_ref AS external_ref,
            sl.sold_at AS sold_at,
            sl.returned_at AS returned_at
        FROM api_key_inventory i
        JOIN suppliers s ON s.id = i.supplier_id
        JOIN purchase_batches b ON b.id = i.batch_id
        LEFT JOIN check_runs cr ON cr.id = i.latest_check_run_id
        LEFT JOIN sales sl ON sl.id = (
            SELECT s2.id FROM sales s2
            WHERE s2.inventory_id = i.id
            ORDER BY CASE WHEN s2.status='sold' THEN 0 ELSE 1 END,
                     s2.sold_at DESC, s2.id DESC
            LIMIT 1
        )
        WHERE i.supplier_id IS NOT NULL AND i.batch_id IS NOT NULL
    """
    args = []
    if sale_view not in ("all", "sellable", "sold"):
        raise ValueError("sale_view must be 'all', 'sellable', or 'sold'")
    if sale_view == "sellable":
        sql += " AND i.stock_status='in_stock' AND i.current_check_status='valid'"
    elif sale_view == "sold":
        sql += " AND i.stock_status='sold'"
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
            i.current_check_status AS latest_check_status,
            cr.checked_at AS latest_check_at,
            sl.id AS sale_id,
            sl.status AS sale_status,
            sl.buyer AS buyer,
            sl.unit_price_minor AS unit_price_minor,
            sl.currency AS currency,
            sl.external_ref AS external_ref,
            sl.sold_at AS sold_at,
            sl.returned_at AS returned_at
        FROM api_key_inventory i
        JOIN suppliers s ON s.id = i.supplier_id
        JOIN purchase_batches b ON b.id = i.batch_id
        LEFT JOIN check_runs cr ON cr.id = i.latest_check_run_id
        LEFT JOIN sales sl ON sl.id = (
            SELECT s2.id FROM sales s2
            WHERE s2.inventory_id = i.id
            ORDER BY CASE WHEN s2.status='sold' THEN 0 ELSE 1 END,
                     s2.sold_at DESC, s2.id DESC
            LIMIT 1
        )
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
                      reason, metadata, created_at, sale_id
               FROM stock_movements
               WHERE inventory_id=?
               ORDER BY created_at DESC, id DESC
               LIMIT 50""",
            (inventory_id,),
        ).fetchall()
        sales = c.execute(
            """SELECT id, inventory_id, buyer, unit_price_minor, currency,
                      external_ref, note, status, sold_at, returned_at,
                      return_reason, created_at, updated_at
               FROM sales
               WHERE inventory_id=?
               ORDER BY sold_at DESC, id DESC""",
            (inventory_id,),
        ).fetchall()
        return {
            "item": dict(row),
            "check_runs": [dict(r) for r in check_runs],
            "movements": [dict(r) for r in movements],
            "sales": [dict(r) for r in sales],
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
    inventory_ids = _unique_ids(ids)
    if not inventory_ids:
        return {"updated": 0, "skipped": 0}
    target_status = _target_status_for_inventory_action(action)
    placeholders = ",".join("?" for _ in inventory_ids)
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
            inventory_ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        for inventory_id in inventory_ids:
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
            _insert_audit_conn(
                c,
                action=f"inventory_{action}",
                target_type="inventory",
                target_id=inventory_id,
                metadata={"from_status": row["stock_status"], "to_status": target_status},
                created_at=t,
            )
            updated += 1
    return {"updated": updated, "skipped": skipped}


def _unique_ids(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(int(item) for item in ids))


def sell_inventory(
    ids: list[int],
    buyer: str,
    unit_price_minor: int | None = None,
    currency: str = "CNY",
    external_ref: str | None = None,
    note: str | None = None,
) -> dict:
    inventory_ids = _unique_ids(ids)
    if not inventory_ids:
        return {"sold": 0, "sale_ids": []}
    clean_buyer = (buyer or "").strip()
    if not clean_buyer:
        raise ValueError("buyer is required")
    if unit_price_minor is not None:
        if isinstance(unit_price_minor, bool) or not isinstance(unit_price_minor, int):
            raise ValueError("unit_price_minor must be an integer")
        if unit_price_minor < 0:
            raise ValueError("unit_price_minor must be non-negative")
    clean_currency = (currency or "CNY").strip().upper()
    if clean_currency != "CNY":
        raise ValueError("currency must be CNY")
    clean_external_ref = (external_ref or "").strip() or None
    clean_note = (note or "").strip() or None
    placeholders = ",".join("?" for _ in inventory_ids)
    t = now()

    try:
        with conn() as c:
            # Serialize the read/validate/write sequence across processes.
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                f"""SELECT id, stock_status, current_check_status,
                           supplier_id, batch_id
                    FROM api_key_inventory
                    WHERE id IN ({placeholders})""",
                inventory_ids,
            ).fetchall()
            by_id = {row["id"]: row for row in rows}
            conflicts: dict[int, str] = {}
            for inventory_id in inventory_ids:
                row = by_id.get(inventory_id)
                if not row or not row["supplier_id"] or not row["batch_id"]:
                    conflicts[inventory_id] = "not_formal_inventory"
                elif row["stock_status"] not in ("in_stock", "reserved"):
                    conflicts[inventory_id] = f"stock_status:{row['stock_status']}"
                elif row["current_check_status"] != "valid":
                    conflicts[inventory_id] = "latest_check_not_valid"
            if conflicts:
                raise InventoryConflictError(
                    f"inventory sale conflict for {len(conflicts)} item(s)",
                    conflicts=conflicts,
                )

            sale_ids: list[int] = []
            for inventory_id in inventory_ids:
                row = by_id[inventory_id]
                cur = c.execute(
                    """INSERT INTO sales (
                           inventory_id, buyer, unit_price_minor, currency,
                           external_ref, note, status, sold_at, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, 'sold', ?, ?, ?)""",
                    (
                        inventory_id,
                        clean_buyer,
                        unit_price_minor,
                        clean_currency,
                        clean_external_ref,
                        clean_note,
                        t,
                        t,
                        t,
                    ),
                )
                sale_id = cur.lastrowid
                updated = c.execute(
                    """UPDATE api_key_inventory
                       SET stock_status='sold', updated_at=?
                       WHERE id=?
                         AND stock_status IN ('in_stock', 'reserved')
                         AND current_check_status='valid'""",
                    (t, inventory_id),
                )
                if updated.rowcount != 1:
                    raise InventoryConflictError(
                        "inventory sale conflict while committing",
                        conflicts={inventory_id: "concurrent_change"},
                    )
                _insert_stock_movement_conn(
                    c,
                    inventory_id,
                    "sale",
                    row["stock_status"],
                    "sold",
                    "inventory_sale",
                    {"sale_id": sale_id, "currency": clean_currency},
                    t,
                    sale_id=sale_id,
                )
                _insert_audit_conn(
                    c,
                    action="inventory_sell",
                    target_type="inventory",
                    target_id=inventory_id,
                    metadata={
                        "sale_id": sale_id,
                        "unit_price_minor": unit_price_minor,
                        "currency": clean_currency,
                    },
                    created_at=t,
                )
                sale_ids.append(sale_id)
            return {"sold": len(sale_ids), "sale_ids": sale_ids}
    except sqlite3.IntegrityError as exc:
        raise InventoryConflictError("inventory sale conflict") from exc


def return_inventory(ids: list[int], reason: str) -> dict:
    inventory_ids = _unique_ids(ids)
    if not inventory_ids:
        return {"returned": 0, "sale_ids": []}
    clean_reason = (reason or "").strip()
    if not clean_reason:
        raise ValueError("return reason is required")
    placeholders = ",".join("?" for _ in inventory_ids)
    t = now()

    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            f"""SELECT i.id, i.stock_status, s.id AS sale_id
                FROM api_key_inventory i
                LEFT JOIN sales s
                  ON s.inventory_id=i.id AND s.status='sold'
                WHERE i.id IN ({placeholders})
                  AND i.supplier_id IS NOT NULL
                  AND i.batch_id IS NOT NULL""",
            inventory_ids,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        conflicts: dict[int, str] = {}
        for inventory_id in inventory_ids:
            row = by_id.get(inventory_id)
            if not row:
                conflicts[inventory_id] = "not_formal_inventory"
            elif row["stock_status"] != "sold":
                conflicts[inventory_id] = f"stock_status:{row['stock_status']}"
            elif row["sale_id"] is None:
                conflicts[inventory_id] = "active_sale_not_found"
        if conflicts:
            raise InventoryConflictError(
                f"inventory return conflict for {len(conflicts)} item(s)",
                conflicts=conflicts,
            )

        sale_ids: list[int] = []
        for inventory_id in inventory_ids:
            sale_id = by_id[inventory_id]["sale_id"]
            updated_sale = c.execute(
                """UPDATE sales
                   SET status='returned', returned_at=?, return_reason=?, updated_at=?
                   WHERE id=? AND status='sold'""",
                (t, clean_reason, t, sale_id),
            )
            updated_inventory = c.execute(
                """UPDATE api_key_inventory
                   SET stock_status='returned', current_check_status='pending', updated_at=?
                   WHERE id=? AND stock_status='sold'""",
                (t, inventory_id),
            )
            if updated_sale.rowcount != 1 or updated_inventory.rowcount != 1:
                raise InventoryConflictError(
                    "inventory return conflict while committing",
                    conflicts={inventory_id: "concurrent_change"},
                )
            _insert_stock_movement_conn(
                c,
                inventory_id,
                "return",
                "sold",
                "returned",
                clean_reason,
                {"sale_id": sale_id},
                t,
                sale_id=sale_id,
            )
            _insert_audit_conn(
                c,
                action="inventory_return",
                target_type="inventory",
                target_id=inventory_id,
                metadata={"sale_id": sale_id},
                created_at=t,
            )
            sale_ids.append(sale_id)
        return {"returned": len(sale_ids), "sale_ids": sale_ids}


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
                   SET key_id=?, current_check_status='pending', updated_at=?
                   WHERE id=?""",
                (key_id, t, inventory_id),
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


def export_sold_entries(ids: list[int]) -> list[dict]:
    """Return full-key export rows for the requested currently-sold inventory IDs."""
    inventory_ids = _unique_ids(ids)
    if not inventory_ids:
        return []
    placeholders = ",".join("?" for _ in inventory_ids)
    with conn() as c:
        rows = c.execute(
            f"""SELECT i.id AS inventory_id, i.api_key, i.provider, i.tier, i.extra,
                       s.id AS sale_id, s.buyer, s.unit_price_minor, s.currency,
                       s.external_ref, s.note, s.status AS sale_status,
                       s.sold_at, s.returned_at
                FROM api_key_inventory i
                JOIN sales s
                  ON s.inventory_id=i.id AND s.status='sold'
                WHERE i.id IN ({placeholders})
                  AND i.stock_status='sold'
                  AND i.supplier_id IS NOT NULL
                  AND i.batch_id IS NOT NULL
                ORDER BY i.id""",
            inventory_ids,
        ).fetchall()
        return [dict(row) for row in rows]


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
        sellable = c.execute(
            """SELECT COUNT(*) FROM api_key_inventory
               WHERE supplier_id IS NOT NULL
                 AND batch_id IS NOT NULL
                 AND stock_status='in_stock'
                 AND current_check_status='valid'"""
        ).fetchone()[0]
        sold = c.execute(
            """SELECT COUNT(*) FROM api_key_inventory
               WHERE supplier_id IS NOT NULL
                 AND batch_id IS NOT NULL
                 AND stock_status='sold'"""
        ).fetchone()[0]
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
        "by_sale_state": {"sellable": sellable, "sold": sold},
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


def get_key_entries(ids: list[int]) -> list[dict]:
    key_ids = _unique_ids(ids)
    if not key_ids:
        return []
    placeholders = ",".join("?" for _ in key_ids)
    with conn() as c:
        rows = c.execute(
            f"SELECT * FROM keys WHERE id IN ({placeholders})",
            key_ids,
        ).fetchall()
        by_id = {row["id"]: dict(row) for row in rows}
        return [by_id[key_id] for key_id in key_ids if key_id in by_id]


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
        if cur.rowcount:
            _insert_audit_conn(
                c,
                action="keys_delete",
                target_type="keys",
                metadata={"requested_ids": _unique_ids(ids), "deleted": cur.rowcount},
            )
        return cur.rowcount


def create_job(
    total: int,
    concurrency: int,
    mode: str = "quick",
    detail_total: int = 0,
    detail_label: str | None = None,
) -> int:
    t = now()
    with conn() as c:
        cur = c.execute(
            """INSERT INTO jobs (
                   total, concurrency, mode, detail_total, detail_label,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                total,
                concurrency,
                mode or "quick",
                max(0, int(detail_total)),
                str(detail_label)[:64] if detail_label else None,
                t,
                t,
            ),
        )
        return cur.lastrowid


def bump_job(job_id: int, done_delta: int = 1):
    with conn() as c:
        c.execute("UPDATE jobs SET done=done+?, updated_at=? WHERE id=?", (done_delta, now(), job_id))


def bump_job_detail(job_id: int, label: str | None = None, done_delta: int = 1):
    with conn() as c:
        c.execute(
            """UPDATE jobs
                  SET detail_done = CASE
                          WHEN detail_total > 0
                          THEN MIN(detail_total, detail_done + ?)
                          ELSE detail_done + ?
                      END,
                      detail_label = ?,
                      updated_at = ?
                WHERE id = ?""",
            (
                done_delta,
                done_delta,
                str(label)[:64] if label else None,
                now(),
                job_id,
            ),
        )


def set_job_detail_label(job_id: int, label: str | None):
    with conn() as c:
        c.execute(
            "UPDATE jobs SET detail_label=?, updated_at=? WHERE id=?",
            (str(label)[:64] if label else None, now(), job_id),
        )


def cancel_running_jobs(reason: str = "服务重启，任务已中止") -> int:
    """Close jobs whose in-memory asyncio tasks cannot survive a restart."""
    with conn() as c:
        cursor = c.execute(
            """UPDATE jobs
                  SET status='cancelled', detail_label=?, updated_at=?
                WHERE status='running'""",
            (str(reason)[:64], now()),
        )
        return cursor.rowcount


def finish_job(job_id: int):
    with conn() as c:
        c.execute(
            """UPDATE jobs
                  SET status='done',
                      detail_done=CASE
                          WHEN detail_total > 0 THEN detail_total
                          ELSE detail_done
                      END,
                      detail_label=NULL,
                      updated_at=?
                WHERE id=?""",
            (now(), job_id),
        )


def get_job(job_id: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_running_jobs() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM jobs WHERE status='running' ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
