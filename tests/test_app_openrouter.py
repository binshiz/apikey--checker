import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import db
from app import ADMIN_KEY, CHECKERS, CHECKER_SUPPORTS_PROXY, app
from checkers import openrouter


OPENROUTER_KEY = "sk-or-v1-" + "ef" * 32


class OpenRouterAppTests(unittest.TestCase):
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

    def test_checker_is_registered_with_proxy_support(self):
        self.assertIs(CHECKERS["openrouter"], openrouter.check)
        self.assertTrue(CHECKER_SUPPORTS_PROXY["openrouter"])

    def test_page_exposes_openrouter_import_help_and_all_provider_filters(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("sk-or-v1-...", response.text)
        self.assertIn("openrouter/free", response.text)
        self.assertIn("Claude Opus 5", response.text)
        self.assertIn("Claude Fable 5", response.text)
        self.assertIn("账号总额度", response.text)
        self.assertIn("已使用额度", response.text)
        self.assertIn("剩余额度", response.text)
        self.assertEqual(
            response.text.count('<option value="openrouter">OpenRouter</option>'),
            3,
        )
        self.assertEqual(response.text.count("<option>Paid</option>"), 3)
        self.assertIn("/static/app.js?v=20260805-bedrock-bearer-runtime-v1", response.text)
        self.assertIn("/static/style.css?v=20260725-bedrock-groups", response.text)

    def test_import_detects_masks_and_exports_openrouter_key(self):
        with patch("app.run_job", new_callable=AsyncMock):
            response = self.client.post(
                "/api/keys/import",
                headers=self.headers,
                json={"text": OPENROUTER_KEY},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["breakdown"], {"openrouter": 1})
        listed = self.client.get("/api/keys", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        row = listed.json()["keys"][0]
        self.assertEqual(row["provider"], "openrouter")
        self.assertEqual(row["api_key_short"], f"sk-or-v1-…{OPENROUTER_KEY[-6:]}")
        self.assertNotIn(OPENROUTER_KEY, listed.text)

        key_id = db.list_keys()[0]["id"]
        exported = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "txt"},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.text, OPENROUTER_KEY)


if __name__ == "__main__":
    unittest.main()
