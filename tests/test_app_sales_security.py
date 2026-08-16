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

    def test_bedrock_txt_exports_expand_only_callable_regions_across_views(self):
        api_key = "AKIA0000000000000000|" + "A" * 40
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        db.save_result(
            key_id,
            {
                "status": "valid",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": None,
                "extra": {
                    "invocation_verification": "success",
                    "region_results": {
                        "us-east-1": {
                            "invocations": [
                                {
                                    "model_id": "global.anthropic.claude-opus-4-8",
                                    "family": "opus",
                                    "version": "4.8",
                                    "status": "success",
                                },
                                {
                                    "model_id": "us.anthropic.claude-fable-5",
                                    "family": "fable",
                                    "version": "5",
                                    "status": "success",
                                },
                            ],
                        },
                        "us-east-2": {
                            "invocations": [
                                {
                                    "model_id": "global.anthropic.claude-opus-4-8",
                                    "family": "opus",
                                    "version": "4.8",
                                    "status": "success",
                                },
                            ],
                        },
                        "us-west-2": {
                            "invocations": [
                                {
                                    "model_id": "us.anthropic.claude-fable-5",
                                    "family": "fable",
                                    "version": "5",
                                    "status": "success",
                                },
                                {
                                    "model_id": "us.anthropic.claude-opus-4-7",
                                    "family": "opus",
                                    "version": "4.7",
                                    "status": "success",
                                },
                            ],
                        },
                    },
                    "model_summary": {
                        "supported_regions": [
                            "us-east-1",
                            "US-EAST-2",
                            "eu-west-1",
                        ],
                        "successful_regions": [
                            "us-east-1",
                            "US-EAST-2",
                            "us-west-2",
                            "us-east-1",
                            "invalid region\nunsafe",
                        ],
                        "supported_models": [
                            "global.anthropic.claude-opus-4-8-v1:0",
                        ],
                    },
                },
            },
        )
        vault = next(row for row in db.list_vault() if row["api_key"] == api_key)
        db.inbound_from_vault([vault["id"]], "AWS Supplier")
        inventory = next(row for row in db.list_inventory() if row["api_key"] == api_key)
        expected = "\n".join([
            f"{api_key}|us-east-1",
            f"{api_key}|us-west-2",
            f"{api_key}|us-east-2",
        ])

        for endpoint, row_id in (
            ("/api/keys/export", key_id),
            ("/api/vault/export", vault["id"]),
            ("/api/inventory/export", inventory["id"]),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(
                    endpoint,
                    headers=self.headers,
                    json={"ids": [row_id], "format": "txt"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.text, expected)

        fable_group_mapping = {
            "claude-fable-5": "us.anthropic.claude-fable-5",
        }
        other_group_mapping = {
            "claude-opus-4-8": "global.anthropic.claude-opus-4-8",
        }
        bundle = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "bundle"},
        )
        self.assertEqual(bundle.status_code, 200, bundle.text)
        self.assertEqual(
            bundle.text,
            "\n".join([
                "\n".join([
                    f"{api_key}|us-east-1",
                    f"{api_key}|us-west-2",
                ]),
                json.dumps(fable_group_mapping, indent=2),
                f"{api_key}|us-east-2",
                json.dumps(other_group_mapping, indent=2),
            ]),
        )

        json_export = self.client.post(
            "/api/inventory/export",
            headers=self.headers,
            json={"ids": [inventory["id"]], "format": "json"},
        )
        self.assertEqual(json_export.status_code, 200, json_export.text)
        self.assertEqual(json_export.json()[0]["api_key"], api_key)
        self.assertEqual(
            json_export.json()[0]["extra"]["gateway_primary_model"],
            "claude-fable-5",
        )
        self.assertEqual(
            json_export.json()[0]["extra"]["gateway_primary_regions"],
            ["us-east-1", "us-west-2"],
        )
        self.assertEqual(
            json_export.json()[0]["extra"]["gateway_region_groups"],
            [
                {
                    "kind": "fable_5",
                    "regions": ["us-east-1", "us-west-2"],
                    "mapping": fable_group_mapping,
                },
                {
                    "kind": "other_models",
                    "regions": ["us-east-2"],
                    "mapping": other_group_mapping,
                },
            ],
        )

    def test_bedrock_bundle_places_flat_json_after_each_compatible_route_group(self):
        api_key = "AKIA0000000000000000|" + "A" * 40
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        mappings_by_region = {
            "af-south-1": {
                "claude-opus-4-6": "global.anthropic.claude-opus-4-6-v1",
            },
            "ap-northeast-1": {
                "claude-opus-4-6": "global.anthropic.claude-opus-4-6-v1",
            },
            "ca-west-1": {
                "claude-opus-4-6": "eu.anthropic.claude-opus-4-6-v1",
            },
            "eu-west-1": {
                "claude-opus-4-6": "eu.anthropic.claude-opus-4-6-v1",
            },
            "ca-central-1": {
                "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
            },
            "us-west-1": {
                "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
            },
            "ap-southeast-2": {
                "claude-opus-4-5-20251101":
                    "global.anthropic.claude-opus-4-5-20251101-v1:0",
                "claude-opus-4-6": "au.anthropic.claude-opus-4-6-v1",
            },
            "ap-southeast-4": {
                "claude-opus-4-5-20251101":
                    "global.anthropic.claude-opus-4-5-20251101-v1:0",
                "claude-opus-4-6": "au.anthropic.claude-opus-4-6-v1",
            },
        }
        db.save_result(
            key_id,
            {
                "status": "valid",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": None,
                "extra": {
                    "invocation_verification": "success",
                    "region_results": {
                        region: {
                            "invocations": [
                                {
                                    "model_id": target,
                                    "status": "success",
                                }
                                for target in mapping.values()
                            ],
                        }
                        for region, mapping in mappings_by_region.items()
                    },
                },
            },
        )

        response = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "bundle"},
        )

        global_mapping = {
            "claude-opus-4-6": "global.anthropic.claude-opus-4-6-v1",
        }
        eu_mapping = {
            "claude-opus-4-6": "eu.anthropic.claude-opus-4-6-v1",
        }
        us_mapping = {
            "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
        }
        au_mapping = {
            "claude-opus-4-5-20251101":
                "global.anthropic.claude-opus-4-5-20251101-v1:0",
            "claude-opus-4-6": "au.anthropic.claude-opus-4-6-v1",
        }
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.text,
            "\n".join([
                f"{api_key}|af-south-1",
                f"{api_key}|ap-northeast-1",
                json.dumps(global_mapping, indent=2),
                f"{api_key}|ca-west-1",
                f"{api_key}|eu-west-1",
                json.dumps(eu_mapping, indent=2),
                f"{api_key}|ca-central-1",
                f"{api_key}|us-west-1",
                json.dumps(us_mapping, indent=2),
                f"{api_key}|ap-southeast-2",
                f"{api_key}|ap-southeast-4",
                json.dumps(au_mapping, indent=2),
            ]),
        )
        self.assertNotIn('"route_groups"', response.text)

    def test_native_bedrock_api_key_export_expands_authorized_regions(self):
        api_key = "ABSK" + "QmVkcm9ja0FQSUtleS0" + "A" * 80 + "="
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        db.save_result(
            key_id,
            {
                "status": "valid",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": None,
                "extra": {
                    "credential_type": "bedrock_api_key",
                    "invocation_verification": "not_attempted",
                    "model_summary": {
                        "authorized_regions": [
                            "us-east-1",
                            "EU-WEST-1",
                            "us-east-1",
                            "invalid region\nunsafe",
                        ],
                        "successful_regions": [],
                    },
                },
            },
        )

        response = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "txt"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.text,
            f"{api_key}|us-east-1\n{api_key}|eu-west-1",
        )
        self.assertEqual(db.list_vault(), [])

    def test_native_bedrock_api_key_exports_runtime_regions_and_grouped_mappings(self):
        api_key = "ABSK" + "QmVkcm9ja0FQSUtleS0" + "C" * 80 + "="
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        db.save_result(
            key_id,
            {
                "status": "valid",
                "tier": None,
                "rpm": None,
                "tpm": None,
                "error": None,
                "extra": {
                    "credential_type": "bedrock_api_key",
                    "invocation_verification": "success",
                    "model_summary": {
                        "authorized_regions": [
                            "us-east-1",
                            "eu-west-1",
                            "ap-south-2",
                        ],
                        "successful_regions": ["us-east-1", "eu-west-1"],
                    },
                    "region_results": {
                        "us-east-1": {
                            "invocations": [{
                                "model_id": "us.anthropic.claude-fable-5",
                                "status": "success",
                            }],
                        },
                        "eu-west-1": {
                            "invocations": [{
                                "model_id": "global.anthropic.claude-opus-4-8-v1:0",
                                "status": "success",
                            }],
                        },
                        "ap-south-2": {"invocations": []},
                    },
                },
            },
        )

        txt_response = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "txt"},
        )
        bundle_response = self.client.post(
            "/api/keys/export",
            headers=self.headers,
            json={"ids": [key_id], "format": "bundle"},
        )

        self.assertEqual(txt_response.status_code, 200, txt_response.text)
        self.assertEqual(
            txt_response.text,
            f"{api_key}|us-east-1\n{api_key}|eu-west-1",
        )
        self.assertEqual(bundle_response.status_code, 200, bundle_response.text)
        self.assertEqual(
            bundle_response.text,
            "\n".join([
                f"{api_key}|us-east-1",
                json.dumps({
                    "claude-fable-5": "us.anthropic.claude-fable-5",
                }, indent=2),
                f"{api_key}|eu-west-1",
                json.dumps({
                    "claude-opus-4-8":
                        "global.anthropic.claude-opus-4-8-v1:0",
                }, indent=2),
            ]),
        )
        self.assertNotIn("ap-south-2", txt_response.text)
        self.assertNotIn("ap-south-2", bundle_response.text)

    def test_native_bedrock_api_key_job_uses_bearer_region_count(self):
        api_key = "ABSK" + "QmVkcm9ja0FQSUtleS0" + "B" * 80 + "="
        with (
            patch("app.run_job", new_callable=AsyncMock),
            patch(
                "app.bedrock_checker.configured_api_key_regions",
                return_value=("us-east-1", "us-west-2", "eu-west-1"),
            ),
        ):
            response = self.client.post(
                "/api/keys/import",
                headers=self.headers,
                json={"text": api_key, "concurrency": 2},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["breakdown"], {"aws_bedrock": 1})
        job = db.get_job(response.json()["job_id"])
        self.assertEqual(job["detail_total"], 3)

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
        self.assertGreater(job["detail_total"], 1)
        self.assertEqual(job["detail_done"], 0)
        self.assertEqual(job["detail_label"], "准备中")

    def test_running_jobs_route_is_not_shadowed_by_job_id_route(self):
        job_id = db.create_job(1, 1, mode="quick")

        response = self.client.get("/api/jobs/running", headers=self.headers)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(job_id, [job["id"] for job in response.json()["jobs"]])

    def test_interrupted_jobs_are_cancelled_instead_of_staying_running(self):
        job_id = db.create_job(2, 2, mode="quick", detail_total=10)

        cancelled = db.cancel_running_jobs()

        self.assertEqual(cancelled, 1)
        job = db.get_job(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertIn("中止", job["detail_label"])
        self.assertEqual(db.get_running_jobs(), [])

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

    def test_bedrock_deep_check_receives_selected_socks_proxy(self):
        api_key = "AKIA0000000000000000|" + "A" * 40
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        proxy = "socks5h://proxy-user:proxy-password@127.0.0.1:1080"
        checker = AsyncMock(return_value={
            "status": "valid",
            "tier": None,
            "rpm": None,
            "tpm": None,
            "error": None,
            "extra": {"proxy_used": True},
        })
        pool = MagicMock()
        pool.get_round_robin = AsyncMock(return_value=proxy)
        pool.mark_dead = AsyncMock()

        with patch("app.get_pool", return_value=pool), patch(
            "app.bedrock_checker.deep_check", checker
        ):
            asyncio.run(
                check_one_key(
                    key_id,
                    None,
                    asyncio.Semaphore(1),
                    use_proxy=True,
                    mode="bedrock_deep",
                )
            )

        checker.assert_awaited_once_with(api_key, proxy=proxy)
        pool.get_round_robin.assert_awaited_once()
        pool.mark_dead.assert_not_awaited()

    def test_bedrock_deep_check_updates_region_progress(self):
        api_key = "AKIA0000000000000000|" + "A" * 40
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        proxy = "socks5h://127.0.0.1:1080"

        async def run_checker(key, *, proxy, progress_callback):
            self.assertEqual(key, api_key)
            await progress_callback("STS 验证")
            await progress_callback("us-east-1")
            return {
                "status": "valid",
                "error": None,
                "extra": {"proxy_used": True},
            }

        checker = AsyncMock(side_effect=run_checker)
        pool = MagicMock()
        pool.get_round_robin = AsyncMock(return_value=proxy)
        pool.mark_dead = AsyncMock()
        job_id = db.create_job(
            1,
            1,
            mode="bedrock_deep",
            detail_total=2,
            detail_label="STS 验证",
        )

        with patch("app.get_pool", return_value=pool), patch(
            "app.bedrock_checker.deep_check", checker
        ):
            asyncio.run(
                check_one_key(
                    key_id,
                    job_id,
                    asyncio.Semaphore(1),
                    use_proxy=True,
                    mode="bedrock_deep",
                )
            )

        job = db.get_job(job_id)
        self.assertEqual(job["done"], 1)
        self.assertEqual(job["detail_done"], 2)
        self.assertEqual(job["detail_label"], "us-east-1")
        db.finish_job(job_id)
        finished = db.get_job(job_id)
        self.assertEqual(finished["status"], "done")
        self.assertEqual(finished["detail_done"], finished["detail_total"])
        self.assertIsNone(finished["detail_label"])

    def test_proxy_request_never_falls_back_to_direct_connection(self):
        api_key = "AKIA0000000000000000|" + "A" * 40
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        checker = AsyncMock()
        pool = MagicMock()
        pool.get_round_robin = AsyncMock(return_value=None)

        with patch("app.get_pool", return_value=pool), patch(
            "app.bedrock_checker.deep_check", checker
        ):
            asyncio.run(
                check_one_key(
                    key_id,
                    None,
                    asyncio.Semaphore(1),
                    use_proxy=True,
                    mode="bedrock_deep",
                )
            )

        checker.assert_not_awaited()
        result = db.get_key(key_id)
        self.assertEqual(result["status"], "error")
        self.assertTrue(json.loads(result["extra"])["proxy_unavailable"])

    def test_bedrock_application_error_does_not_remove_working_proxy(self):
        api_key = "AKIA0000000000000000|" + "A" * 40
        key_id = db.upsert_keys([api_key], {api_key: "aws_bedrock"})[0]
        proxy = "socks5h://127.0.0.1:1080"
        checker = AsyncMock(return_value={
            "status": "error",
            "error": "Bedrock check failed (AccessDeniedException)",
            "extra": {"proxy_used": True},
        })
        pool = MagicMock()
        pool.get_round_robin = AsyncMock(return_value=proxy)
        pool.mark_dead = AsyncMock()

        with patch("app.get_pool", return_value=pool), patch(
            "app.bedrock_checker.deep_check", checker
        ):
            asyncio.run(
                check_one_key(
                    key_id,
                    None,
                    asyncio.Semaphore(1),
                    use_proxy=True,
                    mode="bedrock_deep",
                )
            )

        pool.mark_dead.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
