import os
import tempfile
import unittest

import db


class VaultLegacySaleCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "keys.db")
        db.init_db()
        self.vault_ids = self._seed_vault()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def _seed_vault(self):
        fixtures = [
            ("fixture-vault-null-note", "openai", "T1", None),
            ("fixture-vault-empty-note", "anthropic", "T1", ""),
            ("fixture-vault-ordinary-note", "openai", "T2", "待交付"),
            ("fixture-vault-sold-note", "openai", "T1", "已售出"),
            ("fixture-vault-customer-sold-note", "anthropic", "T2", "客户A售出"),
            ("fixture-vault-not-sold-note", "openai", "T2", "未售出"),
        ]
        ids = {}
        for api_key, provider, tier, note in fixtures:
            key_id = db.upsert_keys([api_key], {api_key: provider})[0]
            db.save_result(key_id, {
                "status": "valid",
                "tier": tier,
                "rpm": 100,
                "tpm": 1000,
                "error": None,
                "extra": {},
            })
            vault = next(row for row in db.list_vault() if row["api_key"] == api_key)
            if note is not None:
                db.update_vault_note(vault["id"], note)
            ids[api_key] = vault["id"]
        return ids

    @staticmethod
    def _keys(rows):
        return {row["api_key"] for row in rows}

    def test_legacy_candidate_is_review_filter_not_sale_state(self):
        all_rows = db.list_vault()
        candidates = db.list_vault(legacy_sale_candidate=True)

        self.assertEqual(len(all_rows), 6)
        self.assertEqual(self._keys(candidates), {
            "fixture-vault-sold-note",
            "fixture-vault-customer-sold-note",
            "fixture-vault-not-sold-note",
        })
        flags = {row["api_key"]: row["legacy_sale_candidate"] for row in all_rows}
        self.assertEqual(flags["fixture-vault-ordinary-note"], 0)
        self.assertEqual(flags["fixture-vault-sold-note"], 1)
        self.assertEqual(db.vault_stats()["legacy_sale_candidates"], 3)

    def test_legacy_candidate_combines_provider_and_tier_filters(self):
        rows = db.list_vault(
            provider="openai",
            tier="T2",
            legacy_sale_candidate=True,
        )
        self.assertEqual(self._keys(rows), {"fixture-vault-not-sold-note"})

    def test_note_never_changes_formal_inventory_sale_status(self):
        api_key = "fixture-vault-ordinary-note"
        vault_id = self.vault_ids[api_key]
        result = db.inbound_from_vault([vault_id], "Supplier A")
        self.assertEqual(result["inbounded"], 1)
        inventory = db.list_inventory()[0]
        self.assertEqual(inventory["stock_status"], "in_stock")

        db.update_vault_note(vault_id, "客户B已售出")
        inventory = db.list_inventory()[0]
        self.assertEqual(inventory["stock_status"], "in_stock")
        self.assertEqual(db.list_inventory(sale_view="sold"), [])
        self.assertEqual(len(db.list_inventory(sale_view="sellable")), 1)


if __name__ == "__main__":
    unittest.main()
