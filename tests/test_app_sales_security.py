import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

import db
from app import ADMIN_KEY, CHECKERS, app, check_one_key


class AppSalesSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "keys.db")
        db.init_db()
        self.client = TestClient(app)
        self.headers = {"X-Admin-Key": ADMIN_KEY}

    def tearDown(self):
        self.client.close()
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def create_inventory(self, suffix: str):
        api_key = f"test-openai-sale-{suffix}"
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        db.save_result(
            key_id,
            {
                "status": "valid",
                "tier": "T2",
                "rpm": 200,
                "tpm": 3_000,
                "error": None,
                "extra": {},
            },
        )
        vault = next(row for row in db.list_vault() if row["api_key"] == api_key)
        db.inbound_from_vault([vault["id"]], "Supplier Sale")
        inventory = next(row for row in db.list_inventory() if row["api_key"] == api_key)
        return api_key, key_id, inventory

    def test_sell_export_return_and_restore_flow(self):
        api_key, key_id, item = self.create_inventory("flow")

        sold = self.client.post(
            "/api/inventory/sell",
            headers=self.headers,
            json={
                "ids": [item["id"]],
                "buyer": "Buyer A",
                "unit_price": "12.34",
                "currency": "CNY",
                "external_ref": "REF-1",
                "note": "delivery",
            },
        )
        self.assertEqual(sold.status_code, 200, sold.text)
        self.assertEqual(sold.json()["sold"], 1)

        sold_list = self.client.get(
            "/api/inventory",
            headers=self.headers,
            params={"sale_view": "sold"},
        ).json()
        self.assertEqual(sold_list["count"], 1)
        self.assertEqual(sold_list["keys"][0]["buyer"], "Buyer A")
        self.assertEqual(sold_list["keys"][0]["unit_price_minor"], 1234)
        self.assertNotIn("api_key", sold_list["keys"][0])

        sellable = self.client.get(
            "/api/inventory",
            headers=self.headers,
            params={"sale_view": "sellable"},
        ).json()
        self.assertEqual(sellable["count"], 0)

        export = self.client.post(
            "/api/sales/export",
            headers=self.headers,
            json={"ids": [item["id"]], "format": "txt"},
        )
        self.assertEqual(export.status_code, 200, export.text)
        self.assertEqual(export.text.strip(), api_key)

        returned = self.client.post(
            "/api/inventory/return",
            headers=self.headers,
            json={"ids": [item["id"]], "reason": "customer return"},
        )
        self.assertEqual(returned.status_code, 200, returned.text)
        self.assertEqual(returned.json()["returned"], 1)

        db.save_result(
            key_id,
            {
                "status": "valid",
                "tier": "T2",
                "rpm": 200,
                "tpm": 3_000,
                "error": None,
                "extra": {},
            },
        )
        after_check = next(row for row in db.list_inventory() if row["id"] == item["id"])
        self.assertEqual(after_check["stock_status"], "returned")
        self.assertEqual(after_check["current_check_status"], "valid")

        restored = self.client.post(
            "/api/inventory/status",
            headers=self.headers,
            json={"ids": [item["id"]], "action": "restore_to_stock"},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["updated"], 1)

    def test_sale_conflict_rolls_back_every_item(self):
        _, _, first = self.create_inventory("atomic-a")
        _, second_key_id, second = self.create_inventory("atomic-b")
        db.save_result(
            second_key_id,
            {
                "status": "invalid",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": "revoked",
                "extra": {},
            },
        )

        response = self.client.post(
            "/api/inventory/sell",
            headers=self.headers,
            json={"ids": [first["id"], second["id"]], "buyer": "Buyer B"},
        )
        self.assertEqual(response.status_code, 409, response.text)

        rows = {row["id"]: row for row in db.list_inventory()}
        self.assertEqual(rows[first["id"]]["stock_status"], "in_stock")
        self.assertEqual(rows[second["id"]]["stock_status"], "invalid")
        with db.conn() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 0)

    def test_list_endpoints_hide_keys_and_exports_are_audited(self):
        api_key, key_id, item = self.create_inventory("safe")
        vault = next(row for row in db.list_vault() if row["api_key"] == api_key)

        keys_response = self.client.get("/api/keys", headers=self.headers).json()["keys"][0]
        vault_response = self.client.get("/api/vault", headers=self.headers).json()["keys"][0]
        self.assertNotIn("api_key", keys_response)
        self.assertNotIn("api_key", vault_response)

        key_export = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "txt"},
        )
        vault_export = self.client.post(
            "/api/vault/export",
            headers=self.headers,
            json={"ids": [vault["id"]], "format": "txt"},
        )
        inventory_export = self.client.post(
            "/api/inventory/export",
            headers=self.headers,
            json={"ids": [item["id"]], "format": "txt"},
        )
        self.assertEqual(key_export.text.strip(), api_key)
        self.assertEqual(vault_export.text.strip(), api_key)
        self.assertEqual(inventory_export.text.strip(), api_key)

        with db.conn() as connection:
            audits = connection.execute(
                "SELECT action, metadata FROM audit_logs WHERE action='full_key_export'"
            ).fetchall()
        self.assertEqual(len(audits), 3)
        self.assertTrue(all(api_key not in (row["metadata"] or "") for row in audits))
        self.assertTrue(all(json.loads(row["metadata"])["count"] == 1 for row in audits))

    def test_deep_recheck_only_queues_bedrock(self):
        aws_key = "AKIA0000000000000000|" + "A" * 40
        openai_key = "test-openai-deep-skip"
        aws_id, openai_id = db.upsert_keys(
            [aws_key, openai_key],
            {aws_key: "aws_bedrock", openai_key: "openai"},
        )

        with patch("app.run_job", new_callable=AsyncMock):
            response = self.client.post(
                "/api/keys/recheck",
                headers=self.headers,
                json={
                    "ids": [aws_id, openai_id],
                    "concurrency": 2,
                    "mode": "bedrock_deep",
                    "use_proxy": True,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["queued"], 1)
        self.assertEqual(response.json()["skipped"], 1)
        job = db.get_job(response.json()["job_id"])
        self.assertEqual(job["mode"], "bedrock_deep")

    def test_proxy_credentials_are_not_persisted_or_returned(self):
        api_key = "test-openai-proxy-redaction"
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        proxy = "socks5://proxy-user:proxy-password@127.0.0.1:1080"
        checker = AsyncMock(
            return_value={
                "status": "error",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": f"ProxyError while connecting through {proxy}",
                "extra": {
                    "proxy": proxy,
                    "proxy_dead": proxy,
                    "diagnostic": f"failed via {proxy}",
                },
            }
        )
        pool = MagicMock()
        pool.get_round_robin = AsyncMock(return_value=proxy)
        pool.mark_dead = AsyncMock()

        with patch.dict(CHECKERS, {"openai": checker}), patch(
            "app.get_pool", return_value=pool
        ):
            asyncio.run(
                check_one_key(
                    key_id,
                    None,
                    asyncio.Semaphore(1),
                    use_proxy=True,
                )
            )

        with db.conn() as connection:
            key_row = connection.execute(
                "SELECT extra, error FROM keys WHERE id=?", (key_id,)
            ).fetchone()
            run_row = connection.execute(
                "SELECT extra, error, proxy FROM check_runs WHERE key_id=? ORDER BY id DESC",
                (key_id,),
            ).fetchone()
        persisted = json.dumps([dict(key_row), dict(run_row)], ensure_ascii=False)
        self.assertNotIn("proxy-password", persisted)
        self.assertNotIn(proxy, persisted)
        self.assertIsNone(run_row["proxy"])

        # Protect clients from historical rows written before the redaction fix.
        with db.conn() as connection:
            connection.execute(
                "UPDATE keys SET extra=? WHERE id=?",
                (json.dumps({"proxy": proxy, "proxy_dead": proxy}), key_id),
            )
        response_text = self.client.get(
            "/api/keys", headers=self.headers
        ).text
        self.assertNotIn("proxy-password", response_text)
        self.assertNotIn(proxy, response_text)


if __name__ == "__main__":
    unittest.main()
