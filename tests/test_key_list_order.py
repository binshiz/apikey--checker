import os
import tempfile
import unittest
from unittest.mock import patch

import db


class KeyListOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "keys.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def test_reimported_existing_key_returns_to_top_as_pending(self):
        old_key = "test-openai-old-key"
        new_key = "test-openai-new-key"

        with patch("db.now", return_value=1000):
            old_id = db.upsert_keys([old_key], {old_key: "openai"})[0]
            db.save_result(old_id, {
                "status": "valid",
                "tier": "T2",
                "rpm": 200,
                "tpm": 3000,
                "error": None,
                "extra": {},
            })

        with patch("db.now", return_value=1001):
            db.upsert_keys([new_key], {new_key: "openai"})

        with patch("db.now", return_value=1002):
            reimported = db.upsert_keys([old_key], {old_key: "openai"})

        rows = db.list_keys()

        self.assertEqual(reimported, [old_id])
        self.assertEqual(rows[0]["api_key"], old_key)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["updated_at"], 1002)


if __name__ == "__main__":
    unittest.main()
