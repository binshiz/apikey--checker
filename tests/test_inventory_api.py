import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import db
from app import ADMIN_KEY, app


class InventoryAPITests(unittest.TestCase):
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

    def create_valid_vault_entry(self):
        api_key = "test-api-key-for-vault-inbound"
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        db.save_result(key_id, {
            "status": "valid",
            "tier": "T2",
            "rpm": 200,
            "tpm": 3000,
            "error": None,
            "extra": {"models_count": 1},
        })
        return db.list_vault()[0]

    def create_inventory_row(self, api_key="test-api-key-for-inventory"):
        key_id = db.upsert_keys([api_key], {api_key: "openai"})[0]
        db.save_result(key_id, {
            "status": "valid",
            "tier": "T2",
            "rpm": 200,
            "tpm": 3000,
            "error": None,
            "extra": {"models_count": 1},
        })
        vault = next(v for v in db.list_vault() if v["api_key"] == api_key)
        db.inbound_from_vault([vault["id"]], "Supplier API", tags="seed")
        return api_key, next(row for row in db.list_inventory() if row["api_key"] == api_key)

    def test_vault_inbound_requires_supplier(self):
        vault = self.create_valid_vault_entry()

        response = self.client.post(
            "/api/vault/inbound",
            headers=self.headers,
            json={"ids": [vault["id"]], "supplier_name": " "},
        )

        self.assertEqual(response.status_code, 400)

    def test_vault_inbound_and_inventory_are_key_safe(self):
        vault = self.create_valid_vault_entry()

        vault_before = self.client.get("/api/vault", headers=self.headers)
        self.assertEqual(vault_before.status_code, 200)
        vault_before_row = vault_before.json()["keys"][0]
        self.assertNotIn("api_key", vault_before_row)
        self.assertEqual(vault_before_row["is_in_inventory"], 0)
        self.assertIsNone(vault_before_row["inventory_id"])

        inbound = self.client.post(
            "/api/vault/inbound",
            headers=self.headers,
            json={
                "ids": [vault["id"]],
                "supplier_name": "Supplier API",
                "total_cost": None,
                "tags": None,
                "note": None,
            },
        )
        self.assertEqual(inbound.status_code, 200)
        inbound_body = inbound.json()
        self.assertEqual(inbound_body["inbounded"], 1)
        self.assertEqual(inbound_body["skipped"], 0)
        self.assertIn("入库批次 ", inbound_body["batch"]["name"])

        vault_after = self.client.get("/api/vault", headers=self.headers).json()["keys"][0]
        self.assertEqual(vault_after["is_in_inventory"], 1)
        self.assertIsNotNone(vault_after["inventory_id"])
        self.assertEqual(vault_after["inventory_status"], "in_stock")

        inventory = self.client.get("/api/inventory", headers=self.headers)
        self.assertEqual(inventory.status_code, 200)
        body = inventory.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["stats"]["total"], 1)
        row = body["keys"][0]
        self.assertNotIn("api_key", row)
        self.assertIn("api_key_short", row)
        self.assertEqual(row["supplier_name"], "Supplier API")
        self.assertEqual(row["stock_status"], "in_stock")

    def test_inventory_detail_and_export_key_safety(self):
        api_key, row = self.create_inventory_row()

        detail = self.client.get(f"/api/inventory/{row['id']}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        self.assertNotIn("api_key", body["item"])
        self.assertIn("api_key_short", body["item"])
        self.assertTrue(body["check_runs"])
        self.assertTrue(body["movements"])
        self.assertNotIn("proxy", body["check_runs"][0])

        export = self.client.post(
            "/api/inventory/export",
            headers=self.headers,
            json={"ids": [row["id"]], "format": "txt"},
        )
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export.text.strip(), api_key)

        json_export = self.client.post(
            "/api/inventory/export",
            headers=self.headers,
            json={"ids": [row["id"]], "format": "json"},
        )
        self.assertEqual(json_export.status_code, 200)
        self.assertEqual(json_export.json()[0]["api_key"], api_key)

    def test_inventory_filters_status_and_meta(self):
        _, row = self.create_inventory_row()

        meta = self.client.post(
            f"/api/inventory/meta/{row['id']}",
            headers=self.headers,
            json={"note": "priority", "tags": "team-a", "risk_flag": "watch"},
        )
        self.assertEqual(meta.status_code, 200)

        status = self.client.post(
            "/api/inventory/status",
            headers=self.headers,
            json={"ids": [row["id"]], "action": "reserve"},
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["updated"], 1)

        filtered = self.client.get(
            "/api/inventory",
            headers=self.headers,
            params={
                "supplier_id": row["supplier_id"],
                "batch_id": row["batch_id"],
                "stock_status": "reserved",
                "risk_flag": "watch",
            },
        )
        self.assertEqual(filtered.status_code, 200)
        body = filtered.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["keys"][0]["note"], "priority")
        self.assertEqual(body["keys"][0]["tags"], "team-a")
        self.assertEqual(body["keys"][0]["risk_flag"], "watch")
        self.assertEqual(body["stats"]["by_risk_flag"]["watch"], 1)

    def test_inventory_recheck_creates_job_and_skips_archived(self):
        _, active = self.create_inventory_row("test-api-key-active-recheck")
        _, archived = self.create_inventory_row("test-api-key-archived-recheck")
        db.update_inventory_status([archived["id"]], "archive")

        with patch("app.run_job", new_callable=AsyncMock):
            response = self.client.post(
                "/api/inventory/recheck",
                headers=self.headers,
                json={"ids": [active["id"], archived["id"]], "concurrency": 2},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNotNone(body["job_id"])
        self.assertEqual(body["queued"], 1)
        self.assertEqual(body["skipped"], 1)

        job = db.get_job(body["job_id"])
        self.assertEqual(job["total"], 1)
        self.assertEqual(job["concurrency"], 2)


if __name__ == "__main__":
    unittest.main()
