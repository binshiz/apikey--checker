import os
import sqlite3
import tempfile
import unittest

import db


class DBInventoryFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "keys.db")

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def table_names(self):
        with db.conn() as c:
            rows = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {row["name"] for row in rows}

    def counts(self, *tables):
        with db.conn() as c:
            return {
                table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

    def create_formal_inventory(self, api_key="test-openai-inventory", supplier="Supplier A"):
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        db.save_result(key_id, {
            "status": "valid",
            "tier": "T2",
            "rpm": 500,
            "tpm": 100000,
            "error": None,
            "extra": {"supported_models": {"all_count": 1}},
        })
        vault = next(v for v in db.list_vault() if v["api_key"] == api_key)
        db.inbound_from_vault([vault["id"]], supplier)
        return next(row for row in db.list_inventory() if row["api_key"] == api_key)

    def seed_legacy_database(self):
        os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
        c = sqlite3.connect(db.DB_PATH)
        c.row_factory = sqlite3.Row
        db._create_legacy_schema(c)
        c.execute(
            """INSERT INTO keys (
                id, api_key, provider, status, tier, rpm, tpm, extra, error,
                checked_at, created_at, updated_at
            ) VALUES (1, ?, 'openai', 'invalid', 'LegacyTier', 10, 100,
                '{"legacy": true}', 'revoked', 1100, 1000, 1200)""",
            ("test-openai-key",),
        )
        c.execute(
            """INSERT INTO vault (
                id, api_key, provider, tier, rpm, tpm, extra, note,
                first_verified_at, last_verified_at, check_count
            ) VALUES (1, ?, 'openai', 'VaultTier', 20, 200, '{}',
                'keep note', 900, 1300, 2)""",
            ("test-openai-key",),
        )
        c.execute(
            """INSERT INTO vault (
                id, api_key, provider, tier, rpm, tpm, extra, note,
                first_verified_at, last_verified_at, check_count
            ) VALUES (2, ?, 'anthropic', 'T1', 30, 300, '{"models": 1}',
                'vault only', 1400, 1500, 1)""",
            ("test-anthropic-key",),
        )
        c.execute(
            """INSERT INTO jobs (
                id, status, total, done, concurrency, created_at, updated_at
            ) VALUES (1, 'done', 1, 1, 4, 1000, 1200)"""
        )
        c.commit()
        c.close()

    def test_empty_database_initialization_is_idempotent(self):
        db.init_db()
        db.init_db()

        self.assertTrue({
            "keys",
            "vault",
            "jobs",
            "schema_migrations",
            "suppliers",
            "purchase_batches",
            "api_key_inventory",
            "stock_movements",
            "check_runs",
        }.issubset(self.table_names()))
        self.assertEqual(self.counts("schema_migrations")["schema_migrations"], 1)
        self.assertEqual(
            self.counts("api_key_inventory", "stock_movements", "check_runs"),
            {"api_key_inventory": 0, "stock_movements": 0, "check_runs": 0},
        )

    def test_legacy_database_is_backfilled_once(self):
        self.seed_legacy_database()

        db.init_db()
        first_counts = self.counts(
            "keys",
            "vault",
            "jobs",
            "api_key_inventory",
            "stock_movements",
            "check_runs",
        )
        db.init_db()
        second_counts = self.counts(
            "keys",
            "vault",
            "jobs",
            "api_key_inventory",
            "stock_movements",
            "check_runs",
        )

        self.assertEqual(first_counts, second_counts)
        self.assertEqual(first_counts["keys"], 1)
        self.assertEqual(first_counts["vault"], 2)
        self.assertEqual(first_counts["jobs"], 1)
        self.assertEqual(first_counts["api_key_inventory"], 2)
        self.assertEqual(first_counts["stock_movements"], 2)
        self.assertEqual(first_counts["check_runs"], 2)

        with db.conn() as c:
            duplicate = c.execute(
                "SELECT * FROM api_key_inventory WHERE api_key='test-openai-key'"
            ).fetchone()
            vault_only = c.execute(
                "SELECT * FROM api_key_inventory WHERE api_key='test-anthropic-key'"
            ).fetchone()
            run_sources = [
                row["source"]
                for row in c.execute("SELECT source FROM check_runs ORDER BY id").fetchall()
            ]

        self.assertEqual(duplicate["stock_status"], "invalid")
        self.assertEqual(duplicate["tier"], "LegacyTier")
        self.assertEqual(duplicate["key_id"], 1)
        self.assertEqual(duplicate["vault_id"], 1)
        self.assertEqual(duplicate["note"], "keep note")
        self.assertEqual(vault_only["stock_status"], "pending_check")
        self.assertIsNone(vault_only["key_id"])
        self.assertEqual(vault_only["vault_id"], 2)
        self.assertEqual(run_sources, ["legacy_keys", "legacy_vault"])

    def test_import_and_save_result_sync_inventory_tables(self):
        db.init_db()
        api_key = "test-openai-import"

        ids = db.upsert_keys([api_key], {api_key: "openai"})
        self.assertEqual(len(ids), 1)
        key_id = ids[0]

        with db.conn() as c:
            inventory = c.execute("SELECT * FROM api_key_inventory").fetchone()
            movement = c.execute("SELECT * FROM stock_movements").fetchone()

        self.assertEqual(inventory["api_key"], api_key)
        self.assertEqual(inventory["key_id"], key_id)
        self.assertEqual(inventory["provider"], "openai")
        self.assertEqual(inventory["stock_status"], "pending_check")
        self.assertEqual(movement["movement_type"], "inbound")
        self.assertEqual(movement["to_status"], "pending_check")

        db.save_result(key_id, {
            "status": "valid",
            "tier": "T2",
            "rpm": 500,
            "tpm": 100000,
            "error": None,
            "extra": {"supported_models": {"all_count": 1}},
        })

        with db.conn() as c:
            inventory = c.execute("SELECT * FROM api_key_inventory").fetchone()
            check_run = c.execute("SELECT * FROM check_runs").fetchone()
            vault = c.execute("SELECT * FROM vault").fetchone()
            movements = c.execute(
                "SELECT movement_type, from_status, to_status FROM stock_movements ORDER BY id"
            ).fetchall()

        self.assertEqual(inventory["stock_status"], "pending_check")
        self.assertEqual(inventory["tier"], "T2")
        self.assertEqual(inventory["rpm"], 500)
        self.assertEqual(inventory["latest_check_run_id"], check_run["id"])
        self.assertEqual(inventory["vault_id"], vault["id"])
        self.assertIsNone(inventory["supplier_id"])
        self.assertIsNone(inventory["batch_id"])
        self.assertEqual(check_run["source"], "checker")
        self.assertEqual(check_run["status"], "valid")
        self.assertEqual(vault["api_key"], api_key)
        self.assertEqual(
            [(row["movement_type"], row["from_status"], row["to_status"]) for row in movements],
            [("inbound", None, "pending_check")],
        )

        inbound = db.inbound_from_vault([vault["id"]], "Supplier A")
        self.assertEqual(inbound["inbounded"], 1)
        self.assertEqual(inbound["skipped"], 0)
        self.assertIn("入库批次 ", inbound["batch"]["name"])

        with db.conn() as c:
            inventory = c.execute("SELECT * FROM api_key_inventory").fetchone()
            supplier = c.execute("SELECT * FROM suppliers").fetchone()
            batch = c.execute("SELECT * FROM purchase_batches").fetchone()
            movements = c.execute(
                "SELECT movement_type, from_status, to_status FROM stock_movements ORDER BY id"
            ).fetchall()

        self.assertEqual(inventory["stock_status"], "in_stock")
        self.assertEqual(inventory["tier"], "T2")
        self.assertEqual(inventory["supplier_id"], supplier["id"])
        self.assertEqual(inventory["batch_id"], batch["id"])
        self.assertEqual(supplier["name"], "Supplier A")
        self.assertEqual(batch["quantity"], 1)
        self.assertIsNotNone(batch["purchased_at"])
        self.assertEqual(
            [(row["movement_type"], row["from_status"], row["to_status"]) for row in movements],
            [("inbound", None, "pending_check"), ("inbound_from_vault", "pending_check", "in_stock")],
        )

        again = db.inbound_from_vault([vault["id"]], "Supplier A")
        self.assertEqual(again["inbounded"], 0)
        self.assertEqual(again["skipped"], 1)
        self.assertEqual(self.counts("purchase_batches")["purchase_batches"], 1)

        inventory_rows = db.list_inventory()
        stats = db.inventory_stats()
        self.assertEqual(len(inventory_rows), 1)
        self.assertEqual(inventory_rows[0]["supplier_name"], "Supplier A")
        self.assertEqual(inventory_rows[0]["stock_status"], "in_stock")
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["by_status"]["in_stock"], 1)

        self.assertEqual(db.delete_keys([key_id]), 1)
        with db.conn() as c:
            inventory = c.execute("SELECT * FROM api_key_inventory").fetchone()
            check_run = c.execute("SELECT * FROM check_runs").fetchone()

        self.assertIsNone(inventory["key_id"])
        self.assertIsNone(check_run["key_id"])
        self.assertEqual(inventory["api_key"], api_key)

    def test_inventory_status_actions_write_movements(self):
        db.init_db()
        row = self.create_formal_inventory()

        result = db.update_inventory_status([row["id"]], "reserve")
        self.assertEqual(result, {"updated": 1, "skipped": 0})
        result = db.update_inventory_status([row["id"]], "reserve")
        self.assertEqual(result, {"updated": 0, "skipped": 1})
        result = db.update_inventory_status([row["id"]], "quarantine")
        self.assertEqual(result, {"updated": 1, "skipped": 0})
        result = db.update_inventory_status([row["id"]], "archive")
        self.assertEqual(result, {"updated": 1, "skipped": 0})

        with db.conn() as c:
            inventory = c.execute("SELECT stock_status FROM api_key_inventory WHERE id=?", (row["id"],)).fetchone()
            movements = c.execute(
                """SELECT movement_type, from_status, to_status
                   FROM stock_movements
                   WHERE inventory_id=?
                   ORDER BY id""",
                (row["id"],),
            ).fetchall()

        self.assertEqual(inventory["stock_status"], "archived")
        self.assertEqual(
            [(m["movement_type"], m["from_status"], m["to_status"]) for m in movements[-3:]],
            [
                ("reserve", "in_stock", "reserved"),
                ("quarantine", "reserved", "quarantined"),
                ("archive", "quarantined", "archived"),
            ],
        )

    def test_restore_to_stock_requires_latest_valid_check(self):
        db.init_db()
        valid_row = self.create_formal_inventory("test-openai-valid-restore")
        no_quota_row = self.create_formal_inventory("test-openai-noquota-restore")

        db.update_inventory_status([valid_row["id"], no_quota_row["id"]], "reserve")
        db.save_result(no_quota_row["key_id"], {
            "status": "no_quota",
            "tier": "T1",
            "rpm": 0,
            "tpm": 0,
            "error": "empty balance",
            "extra": {},
        })

        result = db.update_inventory_status(
            [valid_row["id"], no_quota_row["id"]],
            "restore_to_stock",
        )
        rows = {row["id"]: row for row in db.list_inventory()}

        self.assertEqual(result, {"updated": 1, "skipped": 1})
        self.assertEqual(rows[valid_row["id"]]["stock_status"], "in_stock")
        self.assertEqual(rows[no_quota_row["id"]]["stock_status"], "no_quota")

    def test_inventory_meta_and_filters(self):
        db.init_db()
        row = self.create_formal_inventory()

        ok = db.update_inventory_meta(
            row["id"],
            note="slow seller",
            tags="gpt,wholesale",
            risk_flag="watch",
        )
        self.assertTrue(ok)

        rows = db.list_inventory(
            supplier_id=row["supplier_id"],
            batch_id=row["batch_id"],
            risk_flag="watch",
        )
        stats = db.inventory_stats()
        detail = db.get_inventory_detail(row["id"])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "slow seller")
        self.assertEqual(rows[0]["tags"], "gpt,wholesale")
        self.assertEqual(rows[0]["risk_flag"], "watch")
        self.assertEqual(stats["by_risk_flag"]["watch"], 1)
        self.assertEqual(stats["risk_flags"][0]["risk_flag"], "watch")
        self.assertIsNotNone(detail)
        self.assertIn("check_runs", detail)
        self.assertIn("movements", detail)

    def test_prepare_inventory_recheck_skips_archived(self):
        db.init_db()
        active = self.create_formal_inventory("test-openai-active-recheck")
        archived = self.create_formal_inventory("test-openai-archived-recheck")
        db.update_inventory_status([archived["id"]], "archive")

        prepared = db.prepare_inventory_recheck([active["id"], archived["id"], 9999])
        rows = {row["id"]: row for row in db.list_inventory()}

        self.assertEqual(prepared["queued"], 1)
        self.assertEqual(prepared["skipped"], 2)
        self.assertEqual(len(prepared["key_ids"]), 1)
        self.assertEqual(rows[active["id"]]["stock_status"], "pending_check")
        self.assertEqual(rows[archived["id"]]["stock_status"], "archived")


if __name__ == "__main__":
    unittest.main()
