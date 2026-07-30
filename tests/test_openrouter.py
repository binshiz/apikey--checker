import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import db
from checkers import openrouter


OPENROUTER_KEY = "sk-or-v1-" + "ab" * 32


def key_info(**overrides):
    data = {
        "is_free_tier": False,
        "is_management_key": False,
        "is_provisioning_key": False,
        "include_byok_in_limit": False,
        "limit": 100.0,
        "limit_remaining": 74.5,
        "limit_reset": "monthly",
        "usage": 25.5,
        "usage_daily": 1.5,
        "usage_weekly": 5.5,
        "usage_monthly": 25.5,
        "byok_usage": 2.5,
        "byok_usage_daily": 0.5,
        "byok_usage_weekly": 1.5,
        "byok_usage_monthly": 2.5,
        "expires_at": "2027-12-31T23:59:59Z",
    }
    data.update(overrides)
    return {"data": data}


def credits_info(total_credits=100.5, total_usage=25.75):
    return {"data": {
        "total_credits": total_credits,
        "total_usage": total_usage,
    }}


def error_response(status, message="request failed", code=None):
    error = {"message": message}
    if code:
        error["code"] = code
    return httpx.Response(status, json={"error": error})


class OpenRouterCheckerTests(unittest.IsolatedAsyncioTestCase):
    async def check_with_handler(self, handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await openrouter._validate_with_client(client, OPENROUTER_KEY)

    async def test_paid_key_metadata_and_fable5_runtime_probe_succeed(self):
        requests = []
        requested_models = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            self.assertEqual(
                request.headers["authorization"],
                f"Bearer {OPENROUTER_KEY}",
            )
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info())
            if request.url.path == openrouter.CREDITS_PATH:
                return httpx.Response(200, json=credits_info())
            payload = json.loads(request.read())
            requested_models.append(payload["model"])
            self.assertEqual(payload["messages"], [{"role": "user", "content": "."}])
            self.assertEqual(payload["max_tokens"], 1)
            self.assertIn(
                payload["model"],
                (openrouter.OPUS5_MODEL, openrouter.FABLE5_MODEL),
            )
            return httpx.Response(200, json={
                "model": payload["model"],
                "choices": [{"message": {"content": "never persist this"}}],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            })

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Paid")
        self.assertEqual(result["extra"]["credential_status"], "valid")
        self.assertEqual(result["extra"]["invocation_verification"], "success")
        self.assertTrue(result["extra"]["key_limit_configured"])
        self.assertEqual(result["extra"]["limit_remaining"], 74.5)
        self.assertEqual(result["extra"]["usage_monthly"], 25.5)
        self.assertEqual(result["extra"]["credits_status"], "success")
        self.assertEqual(result["extra"]["account_total_credits"], 100.5)
        self.assertEqual(result["extra"]["account_total_usage"], 25.75)
        self.assertEqual(result["extra"]["account_balance"], 74.75)
        self.assertEqual(result["extra"]["requested_model"], openrouter.OPUS5_MODEL)
        self.assertEqual(result["extra"]["resolved_model"], openrouter.OPUS5_MODEL)
        self.assertTrue(result["extra"]["has_opus_5"])
        self.assertTrue(result["extra"]["has_fable_5"])
        self.assertEqual(result["extra"]["opus5_probe"]["status"], "callable")
        self.assertEqual(result["extra"]["fable5_probe"]["status"], "callable")
        self.assertEqual(result["extra"]["token_usage"]["total_tokens"], 3)
        self.assertNotIn("never persist this", str(result))
        self.assertEqual(
            [request.url.path for request in requests],
            [
                openrouter.KEY_INFO_PATH,
                openrouter.CREDITS_PATH,
                openrouter.CHAT_COMPLETIONS_PATH,
                openrouter.CHAT_COMPLETIONS_PATH,
            ],
        )
        self.assertEqual(
            requested_models,
            [openrouter.OPUS5_MODEL, openrouter.FABLE5_MODEL],
        )

    async def test_free_tier_maps_to_free(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info(
                    is_free_tier=True,
                    limit=None,
                    limit_remaining=None,
                ))
            if request.url.path == openrouter.CREDITS_PATH:
                return httpx.Response(200, json=credits_info(20, 3.5))
            payload = json.loads(request.read())
            if payload["model"] in (openrouter.OPUS5_MODEL, openrouter.FABLE5_MODEL):
                return error_response(403, "paid model unavailable")
            return httpx.Response(200, json={"model": "openrouter/free"})

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Free")
        self.assertEqual(result["extra"]["account_type"], "free")
        self.assertFalse(result["extra"]["key_limit_configured"])
        self.assertNotIn("limit", result["extra"])
        self.assertNotIn("limit_remaining", result["extra"])
        self.assertEqual(result["extra"]["account_total_credits"], 20)
        self.assertEqual(result["extra"]["account_total_usage"], 3.5)
        self.assertEqual(result["extra"]["account_balance"], 16.5)
        self.assertFalse(result["extra"]["has_opus_5"])
        self.assertFalse(result["extra"]["has_fable_5"])
        self.assertEqual(result["extra"]["opus5_probe"]["status"], "access_denied")
        self.assertEqual(result["extra"]["fable5_probe"]["status"], "access_denied")
        self.assertEqual(result["extra"]["requested_model"], openrouter.PROBE_MODEL)

    async def test_key_info_http_status_mapping(self):
        cases = {
            401: "invalid",
            402: "no_quota",
            429: "no_quota",
            403: "error",
            500: "error",
        }
        for http_status, expected in cases.items():
            with self.subTest(http_status=http_status):
                result = await self.check_with_handler(
                    lambda _request, status=http_status: error_response(
                        status,
                        code="fixture_error",
                    )
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["extra"]["failure_stage"], "key_info")
                self.assertEqual(result["extra"]["http_status"], http_status)

    async def test_runtime_http_status_mapping(self):
        cases = {
            401: ("invalid", "authentication_failed", "authentication_failed"),
            402: ("no_quota", "quota_limited", "no_quota"),
            429: ("no_quota", "quota_limited", "rate_limited"),
            403: ("error", "access_denied", "access_denied"),
            500: ("error", "failed", "upstream_error"),
        }
        for http_status, expected in cases.items():
            with self.subTest(http_status=http_status):
                def handler(request: httpx.Request, status=http_status) -> httpx.Response:
                    if request.url.path == openrouter.KEY_INFO_PATH:
                        return httpx.Response(200, json=key_info())
                    if request.url.path == openrouter.CREDITS_PATH:
                        return httpx.Response(200, json=credits_info())
                    return error_response(status, code="fixture_error")

                result = await self.check_with_handler(handler)
                self.assertEqual(result["status"], expected[0])
                self.assertEqual(result["extra"]["invocation_verification"], expected[1])
                self.assertEqual(result["extra"]["opus5_probe"]["status"], expected[2])
                if http_status == 401:
                    self.assertNotIn("fable5_probe", result["extra"])
                else:
                    self.assertEqual(
                        result["extra"]["fable5_probe"]["status"],
                        expected[2],
                    )
                self.assertEqual(
                    result["extra"]["failure_stage"],
                    "opus5_probe" if http_status == 401 else "runtime_probe",
                )

    async def test_unavailable_fable5_falls_back_to_free_runtime_probe(self):
        requested_models = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info())
            if request.url.path == openrouter.CREDITS_PATH:
                return httpx.Response(200, json=credits_info())
            payload = json.loads(request.read())
            requested_models.append(payload["model"])
            if payload["model"] in (openrouter.OPUS5_MODEL, openrouter.FABLE5_MODEL):
                return error_response(404, "model unavailable")
            return httpx.Response(200, json={
                "model": "google/gemma-free",
                "usage": {"total_tokens": 2},
            })

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            requested_models,
            [
                openrouter.OPUS5_MODEL,
                openrouter.FABLE5_MODEL,
                openrouter.PROBE_MODEL,
            ],
        )
        self.assertFalse(result["extra"]["has_opus_5"])
        self.assertFalse(result["extra"]["has_fable_5"])
        self.assertEqual(
            result["extra"]["opus5_probe"]["status"],
            "model_unavailable",
        )
        self.assertEqual(
            result["extra"]["fable5_probe"]["status"],
            "model_unavailable",
        )
        self.assertEqual(result["extra"]["resolved_model"], "google/gemma-free")

    async def test_inference_key_remains_valid_when_account_credits_are_forbidden(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info())
            if request.url.path == openrouter.CREDITS_PATH:
                return error_response(403, "credits unavailable", code="forbidden")
            return httpx.Response(200, json={
                "model": openrouter.OPUS5_MODEL,
                "usage": {"total_tokens": 2},
            })

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["extra"]["credits_status"], "forbidden")
        self.assertEqual(result["extra"]["credits_http_status"], 403)
        self.assertNotIn("account_total_credits", result["extra"])
        self.assertNotIn("account_total_usage", result["extra"])
        self.assertNotIn("account_balance", result["extra"])
        self.assertEqual(result["extra"]["invocation_verification"], "success")

    async def test_management_key_reads_account_balance_without_runtime_probe(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info(is_management_key=True))
            if request.url.path == openrouter.CREDITS_PATH:
                return httpx.Response(200, json=credits_info())
            self.fail(f"unexpected path: {request.url.path}")

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "management key is not an inference API key")
        self.assertEqual(result["extra"]["credential_status"], "valid")
        self.assertEqual(result["extra"]["invocation_verification"], "not_applicable")
        self.assertEqual(result["extra"]["validation_method"], "current_key+account_credits")
        self.assertEqual(result["extra"]["credits_status"], "success")
        self.assertEqual(result["extra"]["account_total_credits"], 100.5)
        self.assertEqual(result["extra"]["account_total_usage"], 25.75)
        self.assertEqual(result["extra"]["account_balance"], 74.75)
        self.assertEqual(
            [request.url.path for request in requests],
            [openrouter.KEY_INFO_PATH, openrouter.CREDITS_PATH],
        )
        self.assertTrue(all(request.method == "GET" for request in requests))

    async def test_management_key_records_credits_failure_without_leaking_key(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info(is_management_key=True))
            return httpx.Response(403, json={"error": {
                "code": "forbidden",
                "message": f"cannot read credits for {OPENROUTER_KEY}",
            }})

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["extra"]["credits_status"], "forbidden")
        self.assertEqual(result["extra"]["credits_http_status"], 403)
        self.assertIn("[redacted]", result["extra"]["credits_error"])
        self.assertNotIn(OPENROUTER_KEY, str(result))
        self.assertNotIn("account_balance", result["extra"])

    async def test_management_key_rejects_malformed_credits_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == openrouter.KEY_INFO_PATH:
                return httpx.Response(200, json=key_info(is_management_key=True))
            return httpx.Response(200, json={"data": {"total_credits": 100}})

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["extra"]["credits_status"], "invalid_response")
        self.assertEqual(
            result["extra"]["credits_error"],
            "credits response is missing totals",
        )

    async def test_provisioning_key_skips_credits_and_runtime_probe(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json=key_info(is_provisioning_key=True))

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "provisioning key is not an inference API key")
        self.assertEqual(result["extra"]["credential_status"], "valid")
        self.assertEqual(result["extra"]["invocation_verification"], "not_applicable")
        self.assertEqual(result["extra"]["validation_method"], "current_key_only")
        self.assertEqual(len(requests), 1)

    async def test_malformed_key_info_response_is_error(self):
        for response in (
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, content=b"not-json"),
        ):
            with self.subTest(content=response.content):
                result = await self.check_with_handler(lambda _request, r=response: r)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"], "invalid key-info response")

    async def test_timeout_does_not_mark_key_invalid(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        result = await self.check_with_handler(handler)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "timeout")
        self.assertEqual(result["extra"]["failure_stage"], "key_info")

    async def test_error_body_cannot_leak_full_key(self):
        result = await self.check_with_handler(
            lambda _request: httpx.Response(403, json={"error": {
                "code": OPENROUTER_KEY,
                "message": f"credential {OPENROUTER_KEY} cannot be used",
            }})
        )

        self.assertEqual(result["status"], "error")
        self.assertNotIn(OPENROUTER_KEY, str(result))
        self.assertIn("[redacted]", result["error"])
        self.assertEqual(result["extra"]["error_code"], "[redacted]")

    async def test_check_passes_proxy_to_httpx_without_persisting_it(self):
        client = AsyncMock()
        client.get.side_effect = [
            httpx.Response(200, json=key_info()),
            httpx.Response(200, json=credits_info()),
        ]
        client.post.return_value = httpx.Response(
            200,
            json={"model": "openrouter/free"},
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)
        proxy = "socks5://user:password@127.0.0.1:1080"

        with patch("checkers.openrouter.httpx.AsyncClient", return_value=context) as cls:
            result = await openrouter.check(OPENROUTER_KEY, proxy=proxy)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(cls.call_args.kwargs["proxy"], proxy)
        self.assertNotIn(proxy, str(result))


class OpenRouterVaultProofTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "keys.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def test_runtime_proof_is_required_for_vault(self):
        without_proof = OPENROUTER_KEY
        with_proof = "sk-or-v1-" + "cd" * 32
        first_id, second_id = db.upsert_keys(
            [without_proof, with_proof],
            {
                without_proof: "openrouter",
                with_proof: "openrouter",
            },
        )

        db.save_result(first_id, {
            "status": "valid",
            "tier": "Paid",
            "extra": {
                "credential_status": "valid",
                "invocation_verification": "failed",
            },
        })
        db.save_result(second_id, {
            "status": "valid",
            "tier": "Paid",
            "extra": {
                "credential_status": "valid",
                "invocation_verification": "success",
            },
        })

        vault = db.list_vault()
        self.assertEqual(len(vault), 1)
        self.assertEqual(vault[0]["provider"], "openrouter")
        self.assertEqual(vault[0]["api_key"], with_proof)

    def test_audit_metadata_redacts_openrouter_key(self):
        sanitized = db._sanitize_metadata({
            "message": f"failed to use {OPENROUTER_KEY}",
        })
        self.assertNotIn(OPENROUTER_KEY, sanitized["message"])
        self.assertIn("[REDACTED_API_KEY]", sanitized["message"])


if __name__ == "__main__":
    unittest.main()
