import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import db
from app import ADMIN_KEY, app
from detector import canonicalize_gcp_service_account


PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "ZmFrZS10ZXN0LXByaXZhdGUta2V5\n"
    "-----END PRIVATE KEY-----\n"
)


def service_account_info(project_id="fixture-project", **overrides):
    info = {
        "type": "service_account",
        "project_id": project_id,
        "private_key_id": "0123456789abcdef0123456789abcdefdeadbeef",
        "private_key": PRIVATE_KEY,
        "client_email": f"checker@{project_id}.iam.gserviceaccount.com",
        "client_id": "123456789012345678901",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    info.update(overrides)
    return info


class GCPServiceAccountAppTests(unittest.TestCase):
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

    def test_file_objects_mix_with_text_and_deduplicate(self):
        info = service_account_info()
        openai_key = "sk-" + "A" * 40
        with patch("app.run_job", new_callable=AsyncMock):
            response = self.client.post(
                "/api/keys/import",
                headers=self.headers,
                json={
                    "text": openai_key,
                    "service_accounts": [info, dict(reversed(list(info.items())))],
                    "concurrency": 2,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["imported"], 2)
        self.assertEqual(
            response.json()["breakdown"],
            {"openai": 1, "gcp_service_account": 1},
        )
        rows = db.list_keys()
        self.assertEqual(len(rows), 2)
        gcp_row = next(row for row in rows if row["provider"] == "gcp_service_account")
        self.assertEqual(
            gcp_row["api_key"],
            canonicalize_gcp_service_account(info),
        )

    def test_pretty_pasted_json_imports_and_list_is_masked(self):
        info = service_account_info()
        with patch("app.run_job", new_callable=AsyncMock):
            response = self.client.post(
                "/api/keys/import",
                headers=self.headers,
                json={"text": json.dumps(info, indent=2)},
            )

        self.assertEqual(response.status_code, 200, response.text)
        listed = self.client.get("/api/keys", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        body = listed.json()["keys"][0]
        self.assertNotIn("api_key", body)
        self.assertIn("fixture-project", body["api_key_short"])
        self.assertNotIn(PRIVATE_KEY, listed.text)
        self.assertNotIn("ZmFrZS", listed.text)

    def test_invalid_batch_is_rejected_before_any_write(self):
        invalid = service_account_info(type="authorized_user")
        response = self.client.post(
            "/api/keys/import",
            headers=self.headers,
            json={
                "service_accounts": [service_account_info(), invalid],
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(db.list_keys(), [])

    def test_more_than_one_hundred_file_objects_is_rejected(self):
        response = self.client.post(
            "/api/keys/import",
            headers=self.headers,
            json={"service_accounts": [service_account_info()] * 101},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(db.list_keys(), [])

    def test_valid_proof_auto_vaults_and_txt_export_is_jsonl(self):
        info = service_account_info()
        credential = canonicalize_gcp_service_account(info)
        key_id = db.upsert_keys(
            [credential],
            {credential: "gcp_service_account"},
        )[0]
        db.save_result(key_id, {
            "status": "valid",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": None,
            "extra": {
                "credential_type": "gcp_service_account",
                "project_id": info["project_id"],
                "client_email": info["client_email"],
                "private_key_id_suffix": "deadbeef",
                "token_exchange": "success",
                "model_invocation_verification": "success",
                "supported_models": ["gemini-3.6-flash"],
            },
        })

        vault = db.list_vault()
        self.assertEqual(len(vault), 1)
        export = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "txt"},
        )
        self.assertEqual(export.status_code, 200, export.text)
        self.assertEqual(export.text, credential)
        self.assertEqual(json.loads(export.text), info)
        with db.conn() as connection:
            audit = connection.execute(
                "SELECT action FROM audit_logs WHERE action='full_key_export'"
            ).fetchone()
        self.assertIsNotNone(audit)

    def test_oauth_only_without_model_invocation_proof_does_not_vault(self):
        credential = canonicalize_gcp_service_account(service_account_info())
        key_id = db.upsert_keys(
            [credential],
            {credential: "gcp_service_account"},
        )[0]
        db.save_result(key_id, {
            "status": "valid",
            "error": None,
            "extra": {
                "credential_type": "gcp_service_account",
                "token_exchange": "success",
                "model_invocation_verification": "none",
                "supported_models": [],
            },
        })

        self.assertEqual(db.list_vault(), [])


if __name__ == "__main__":
    unittest.main()
