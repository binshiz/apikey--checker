import os
import json
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
            "sales",
            "audit_logs",
        }.issubset(self.table_names()))
        self.assertEqual(self.counts("schema_migrations")["schema_migrations"], 2)
        self.assertEqual(
            self.counts("api_key_inventory", "stock_movements", "check_runs"),
            {"api_key_inventory": 0, "stock_movements": 0, "check_runs": 0},
        )
        with db.conn() as c:
            movement_foreign_keys = c.execute(
                "PRAGMA foreign_key_list(stock_movements)"
            ).fetchall()
        self.assertTrue(any(
            row["from"] == "sale_id"
            and row["table"] == "sales"
            and row["to"] == "id"
            and row["on_delete"] == "SET NULL"
            for row in movement_foreign_keys
        ))

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
        self.assertEqual(duplicate["current_check_status"], "invalid")
        self.assertEqual(duplicate["tier"], "LegacyTier")
        self.assertEqual(duplicate["key_id"], 1)
        self.assertEqual(duplicate["vault_id"], 1)
        self.assertEqual(duplicate["note"], "keep note")
        self.assertEqual(vault_only["stock_status"], "pending_check")
        self.assertEqual(vault_only["current_check_status"], "valid")
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
        self.assertEqual(inventory["current_check_status"], "pending")
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
        self.assertEqual(inventory["current_check_status"], "valid")
        self.assertEqual(inventory["tier"], "T2")
        self.assertEqual(inventory["rpm"], 500)
        self.assertEqual(inventory["latest_check_run_id"], check_run["id"])
        self.assertEqual(inventory["vault_id"], vault["id"])
        self.assertIsNone(inventory["supplier_id"])
        self.assertIsNone(inventory["batch_id"])
        self.assertEqual(check_run["source"], "checker")
        self.assertEqual(check_run["status"], "valid")
        self.assertEqual(vault["api_key"], api_key)
        self.assertEqual(db.list_vault()[0]["is_in_inventory"], 0)
        self.assertIsNone(db.list_vault()[0]["inventory_id"])
        self.assertIsNone(db.list_vault()[0]["inventory_status"])
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
        vault_after_inbound = db.list_vault()[0]
        self.assertEqual(vault_after_inbound["is_in_inventory"], 1)
        self.assertEqual(vault_after_inbound["inventory_id"], inventory["id"])
        self.assertEqual(vault_after_inbound["inventory_status"], "in_stock")
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

        result = db.update_inventory_status([row["id"], row["id"]], "reserve")
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
        self.assertEqual(rows[no_quota_row["id"]]["stock_status"], "reserved")
        self.assertEqual(rows[no_quota_row["id"]]["current_check_status"], "no_quota")

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
        db.init_db()
        rows = {row["id"]: row for row in db.list_inventory()}

        self.assertEqual(prepared["queued"], 1)
        self.assertEqual(prepared["skipped"], 2)
        self.assertEqual(len(prepared["key_ids"]), 1)
        self.assertEqual(rows[active["id"]]["stock_status"], "in_stock")
        self.assertEqual(rows[active["id"]]["current_check_status"], "pending")
        self.assertEqual(rows[archived["id"]]["stock_status"], "archived")

    def test_v2_reconciles_database_with_premature_migration_marker(self):
        self.seed_legacy_database()
        with db.conn() as c:
            db._create_migration_table(c)
            db._migration_inventory_foundation(c)
            # Simulate the early v2 fresh-schema bug: sale_id exists, but it
            # was created without a foreign key to sales.
            c.execute("ALTER TABLE stock_movements ADD COLUMN sale_id INTEGER")
            c.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(1, 'v1', 1)"
            )
            # Simulate a process that recorded v2 before applying its schema.
            c.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(2, 'v2', 1)"
            )

        db.init_db()
        with db.conn() as c:
            inventory_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(api_key_inventory)")
            }
            movement_columns = {
                row["name"] for row in c.execute("PRAGMA table_info(stock_movements)")
            }
            job_columns = {row["name"] for row in c.execute("PRAGMA table_info(jobs)")}
            active_index = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_sales_one_active_sold'"
            ).fetchone()
            movement_foreign_keys = c.execute(
                "PRAGMA foreign_key_list(stock_movements)"
            ).fetchall()
            movement_count = c.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]

        self.assertIn("current_check_status", inventory_columns)
        self.assertIn("sale_id", movement_columns)
        self.assertIn("mode", job_columns)
        self.assertTrue({"sales", "audit_logs"}.issubset(self.table_names()))
        self.assertIn("WHERE status='sold'", active_index["sql"])
        self.assertEqual(movement_count, 2)
        self.assertTrue(any(
            row["from"] == "sale_id"
            and row["table"] == "sales"
            and row["to"] == "id"
            and row["on_delete"] == "SET NULL"
            for row in movement_foreign_keys
        ))

    def test_save_result_and_v2_migration_do_not_persist_proxy_credentials(self):
        db.init_db()
        api_key = "test-proxy-persistence"
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        proxy_url = "socks5://proxy-user:proxy-pass@127.0.0.1:1080"
        db.save_result(key_id, {
            "status": "valid", "tier": "T1", "rpm": 1, "tpm": 1,
            "error": f"connection failed via {proxy_url}",
            "extra": {
                "proxy": proxy_url,
                "proxy_dead": proxy_url,
                "nested": {"detail": f"failed via {proxy_url}"},
                "safe": "keep-me",
            },
        })

        with db.conn() as c:
            key_row = c.execute(
                "SELECT extra, error FROM keys WHERE id=?", (key_id,)
            ).fetchone()
            inventory_row = c.execute(
                "SELECT extra, error FROM api_key_inventory WHERE key_id=?", (key_id,)
            ).fetchone()
            check_row = c.execute(
                "SELECT id, extra, error, proxy FROM check_runs WHERE key_id=?", (key_id,)
            ).fetchone()
            vault_row = c.execute(
                "SELECT id, extra FROM vault WHERE api_key=?", (api_key,)
            ).fetchone()

        for row in (key_row, inventory_row, check_row, vault_row):
            self.assertNotIn(proxy_url, " ".join(
                str(value or "") for value in dict(row).values()
            ))
        self.assertIsNone(check_row["proxy"])
        clean_extra = json.loads(key_row["extra"])
        self.assertNotIn("proxy", clean_extra)
        self.assertIs(clean_extra["proxy_dead"], True)
        self.assertEqual(clean_extra["safe"], "keep-me")

        # Seed the exact legacy persistence shape, then ensure the always-run
        # v2 reconciliation cleans every copy without inspecting/printing it.
        legacy_extra = json.dumps({
            "proxy": proxy_url,
            "proxy_dead": proxy_url,
            "safe": "keep-me",
        })
        with db.conn() as c:
            c.execute(
                "UPDATE keys SET extra=?, error=? WHERE id=?",
                (legacy_extra, proxy_url, key_id),
            )
            c.execute(
                "UPDATE api_key_inventory SET extra=?, error=? WHERE key_id=?",
                (legacy_extra, proxy_url, key_id),
            )
            c.execute(
                "UPDATE check_runs SET extra=?, error=?, proxy=? WHERE id=?",
                (legacy_extra, proxy_url, proxy_url, check_row["id"]),
            )
            c.execute(
                "UPDATE vault SET extra=? WHERE id=?",
                (legacy_extra, vault_row["id"]),
            )

        db.init_db()
        with db.conn() as c:
            cleaned_rows = [
                c.execute("SELECT extra, error FROM keys WHERE id=?", (key_id,)).fetchone(),
                c.execute(
                    "SELECT extra, error FROM api_key_inventory WHERE key_id=?", (key_id,)
                ).fetchone(),
                c.execute(
                    "SELECT extra, error, proxy FROM check_runs WHERE id=?",
                    (check_row["id"],),
                ).fetchone(),
                c.execute("SELECT extra FROM vault WHERE id=?", (vault_row["id"],)).fetchone(),
            ]
        for row in cleaned_rows:
            serialized = " ".join(str(value or "") for value in dict(row).values())
            self.assertNotIn(proxy_url, serialized)
            extra = json.loads(row["extra"])
            self.assertNotIn("proxy", extra)
            self.assertIs(extra["proxy_dead"], True)
            self.assertEqual(extra["safe"], "keep-me")
        self.assertIsNone(cleaned_rows[2]["proxy"])

    def test_mixed_inbound_batch_uses_successful_quantity_for_unit_cost(self):
        db.init_db()
        first = self.create_formal_inventory("test-inbound-existing")
        second_key = "test-inbound-new"
        second_id = db.upsert_keys([second_key], {second_key: "openai"})[0]
        db.save_result(second_id, {
            "status": "valid", "tier": "T1", "rpm": 1, "tpm": 1,
            "error": None, "extra": {},
        })
        second_vault = next(v for v in db.list_vault() if v["api_key"] == second_key)

        result = db.inbound_from_vault(
            [first["vault_id"], second_vault["id"]],
            "Supplier B",
            total_cost=12.5,
        )
        second = next(row for row in db.list_inventory() if row["api_key"] == second_key)

        self.assertEqual(result["inbounded"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["batch"]["quantity"], 1)
        self.assertEqual(second["unit_cost"], 12.5)

    def test_inbound_uses_latest_key_check_status_instead_of_stale_vault(self):
        db.init_db()
        api_key = "test-inbound-stale-vault"
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        db.save_result(key_id, {
            "status": "valid", "tier": "T2", "rpm": 10, "tpm": 100,
            "error": None, "extra": {"snapshot": "valid"},
        })
        vault = next(row for row in db.list_vault() if row["api_key"] == api_key)

        db.save_result(key_id, {
            "status": "invalid", "tier": None, "rpm": None, "tpm": None,
            "error": "revoked", "extra": {"snapshot": "invalid"},
        })
        result = db.inbound_from_vault([vault["id"]], "Supplier Stale")
        inventory = next(row for row in db.list_inventory() if row["api_key"] == api_key)

        self.assertEqual(result["inbounded"], 1)
        self.assertEqual(inventory["stock_status"], "invalid")
        self.assertEqual(inventory["current_check_status"], "invalid")
        self.assertEqual(inventory["latest_check_status"], "invalid")
        self.assertEqual(db.list_inventory(sale_view="sellable"), [])

        with db.conn() as c:
            movement = c.execute(
                """SELECT from_status, to_status
                   FROM stock_movements
                   WHERE inventory_id=? AND movement_type='inbound_from_vault'""",
                (inventory["id"],),
            ).fetchone()
        self.assertEqual((movement["from_status"], movement["to_status"]), (
            "invalid", "invalid",
        ))

    def test_sale_return_restore_history_and_atomic_conflicts(self):
        db.init_db()
        sellable = self.create_formal_inventory("test-sale-valid")
        conflict = self.create_formal_inventory("test-sale-conflict")
        db.save_result(conflict["key_id"], {
            "status": "no_quota", "tier": None, "rpm": None, "tpm": None,
            "error": "no quota", "extra": {},
        })

        with self.assertRaises(db.InventoryConflictError) as raised:
            db.sell_inventory([sellable["id"], conflict["id"]], "Buyer A", 990)
        self.assertNotIn("test-sale", str(raised.exception))
        self.assertEqual(self.counts("sales")["sales"], 0)
        self.assertEqual(db.list_inventory(sale_view="sold"), [])

        db.update_inventory_status([sellable["id"]], "reserve")
        sold = db.sell_inventory(
            [sellable["id"]], "Buyer A", 990, external_ref="ORDER-1"
        )
        sold_row = db.list_inventory(sale_view="sold")[0]
        self.assertEqual(sold, {"sold": 1, "sale_ids": [sold_row["sale_id"]]})
        self.assertEqual(sold_row["buyer"], "Buyer A")
        self.assertEqual(sold_row["unit_price_minor"], 990)

        with self.assertRaises(db.InventoryConflictError):
            db.sell_inventory([sellable["id"]], "Buyer B")
        returned = db.return_inventory([sellable["id"]], "customer return")
        self.assertEqual(returned["sale_ids"], sold["sale_ids"])
        db.init_db()
        row = next(r for r in db.list_inventory() if r["id"] == sellable["id"])
        self.assertEqual(row["stock_status"], "returned")
        self.assertEqual(row["current_check_status"], "pending")
        self.assertEqual(db.update_inventory_status([sellable["id"]], "restore_to_stock"), {
            "updated": 0, "skipped": 1,
        })
        with self.assertRaises(db.InventoryConflictError):
            db.sell_inventory([sellable["id"]], "Buyer B")

        db.save_result(sellable["key_id"], {
            "status": "valid", "tier": "T2", "rpm": 500, "tpm": 100000,
            "error": None, "extra": {},
        })
        checked = next(r for r in db.list_inventory() if r["id"] == sellable["id"])
        self.assertEqual(checked["stock_status"], "returned")
        self.assertEqual(checked["current_check_status"], "valid")
        self.assertEqual(db.update_inventory_status([sellable["id"]], "restore_to_stock"), {
            "updated": 1, "skipped": 0,
        })
        second_sale = db.sell_inventory([sellable["id"]], "Buyer B")
        self.assertNotEqual(second_sale["sale_ids"], sold["sale_ids"])
        detail = db.get_inventory_detail(sellable["id"])
        self.assertEqual([sale["status"] for sale in detail["sales"]], ["sold", "returned"])
        self.assertTrue(all("api_key" not in sale for sale in detail["sales"]))
        self.assertEqual(len(db.export_sold_entries([sellable["id"]])), 1)

    def test_audit_metadata_redacts_credentials(self):
        db.init_db()
        credential = "AKIAABCDEFGHIJKLMNOP|" + ("a" * 40)
        audit_id = db.record_audit(
            "test_action",
            "inventory",
            metadata={"api_key": credential, "message": credential},
        )
        with db.conn() as c:
            metadata = c.execute(
                "SELECT metadata FROM audit_logs WHERE id=?", (audit_id,)
            ).fetchone()["metadata"]
        decoded = json.loads(metadata)
        self.assertNotIn(credential, metadata)
        self.assertEqual(decoded["api_key"], "[REDACTED]")
        self.assertIn("REDACTED", decoded["message"])


if __name__ == "__main__":
    unittest.main()
