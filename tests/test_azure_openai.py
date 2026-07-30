import unittest

import httpx

from checkers.azure_openai import _validate_with_client
from db import _is_callable_valid_check


ENDPOINT = "resource-name.openai.azure.com"
API_KEY = "0123456789abcdef0123456789abcdef"
CREDENTIAL = f"{ENDPOINT}|{API_KEY}"


def deployment_not_found() -> httpx.Response:
    return httpx.Response(404, json={
        "error": {"code": "DeploymentNotFound", "message": "deployment missing"},
    })


class AzureOpenAICheckerTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_ai_full_chat_url_uses_v1_routes(self):
        services_endpoint = "resource-name.services.ai.azure.com"
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "gpt-5.5"}]})
            payload = __import__("json").loads(request.read())
            if payload["model"] == "gpt-5.5":
                return httpx.Response(200, json={"model": "gpt-5.5"})
            return deployment_not_found()

        credential = (
            f"https://{services_endpoint}/openai/v1/chat/completions|{API_KEY}"
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, credential)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            result["extra"]["chat_completions_url"],
            f"https://{services_endpoint}/openai/v1/chat/completions",
        )
        self.assertTrue(all(
            request.url.host == services_endpoint for request in requests
        ))
        self.assertEqual(requests[0].url.path, "/openai/v1/models")
        self.assertTrue(any(
            request.url.path == "/openai/v1/chat/completions" for request in requests
        ))

    async def test_catalog_and_runtime_access_are_recorded_separately(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={
                    "object": "list",
                    "data": [
                        {"id": "gpt-5.5-2026-04-24"},
                        {"id": "gpt-5.6-terra-2026-07-09"},
                        {"id": "text-embedding-3-large"},
                    ],
                })
            model = request.read().decode()
            if '"model":"gpt-5.5"' in model:
                return httpx.Response(200, json={"model": "gpt-5.5-2026-04-24"})
            if '"model":"gpt-5.6-terra"' in model:
                return httpx.Response(200, json={"model": "gpt-5.6-terra-2026-07-09"})
            return deployment_not_found()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, CREDENTIAL)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Unknown")
        self.assertEqual(result["extra"]["models_count"], 3)
        self.assertEqual(result["extra"]["api_version"], "v1")
        self.assertEqual(
            result["extra"]["verified_callable_targets"],
            ["gpt-5.5", "gpt-5.6"],
        )
        probes = result["extra"]["target_model_probes"]
        self.assertEqual(probes["gpt-5.5"]["status"], "callable")
        self.assertEqual(probes["gpt-5.5"]["deployment"], "gpt-5.5")
        self.assertEqual(probes["gpt-5.6"]["status"], "callable")
        self.assertEqual(probes["gpt-5.6"]["deployment"], "gpt-5.6-terra")
        self.assertEqual(requests[0].url.path, "/openai/v1/models")
        self.assertTrue(all(request.headers["api-key"] == API_KEY for request in requests))

    async def test_catalog_visibility_without_deployment_is_not_valid_runtime_access(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/openai/v1/models":
                return httpx.Response(404)
            if request.url.path == "/openai/models":
                return httpx.Response(200, json={
                    "data": [
                        {"id": "gpt-5.5-2026-04-24"},
                        {"id": "gpt-5.6-luna-2026-07-09"},
                    ],
                })
            return deployment_not_found()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, CREDENTIAL)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["extra"]["api_version"], "2024-10-21")
        self.assertEqual(result["extra"]["credential_status"], "valid")
        self.assertEqual(result["extra"]["invocation_verification"], "not_verified")
        self.assertEqual(
            result["extra"]["target_model_probes"]["gpt-5.5"]["status"],
            "deployment_not_found",
        )
        self.assertEqual(requests[1].url.params["api-version"], "2024-10-21")

    async def test_catalog_version_is_tried_as_deployment_fallback(self):
        attempted_models = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={
                    "data": [{"id": "gpt-5.5-2026-04-24"}],
                })
            payload = __import__("json").loads(request.read())
            attempted_models.append(payload["model"])
            if payload["model"] == "gpt-5.5-2026-04-24":
                return httpx.Response(200, json={"model": payload["model"]})
            return deployment_not_found()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, CREDENTIAL)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            result["extra"]["target_model_probes"]["gpt-5.5"]["deployment"],
            "gpt-5.5-2026-04-24",
        )
        self.assertIn("gpt-5.5", attempted_models)
        self.assertIn("gpt-5.5-2026-04-24", attempted_models)

    async def test_unauthorized_key_is_invalid_without_runtime_probe(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(401, json={"error": {"message": "Access denied"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, CREDENTIAL)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["error"], "invalid/revoked")
        self.assertEqual(len(requests), 1)

    async def test_forbidden_network_policy_is_not_marked_invalid(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={
                "error": {
                    "message": (
                        "Access denied due to Virtual Network/Firewall rules for "
                        f"{API_KEY}."
                    ),
                },
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, CREDENTIAL)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["extra"]["http_status"], 403)
        self.assertIn("Firewall", result["error"])
        self.assertNotIn(API_KEY, result["error"])

    async def test_runtime_rate_limit_is_no_quota_not_callable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(429, json={"error": {"message": "rate limited"}})
            payload = __import__("json").loads(request.read())
            if payload["model"] == "gpt-5.5":
                return httpx.Response(429, json={
                    "error": {"code": "RateLimitReached"},
                })
            return deployment_not_found()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _validate_with_client(client, CREDENTIAL)

        self.assertEqual(result["status"], "no_quota")
        self.assertEqual(result["extra"]["invocation_verification"], "rate_limited")
        self.assertEqual(result["extra"]["rate_limited_targets"], ["gpt-5.5"])
        self.assertTrue(result["extra"]["models_list_rate_limited"])


class AzureOpenAIVaultProofTests(unittest.TestCase):
    def test_catalog_only_result_cannot_auto_vault(self):
        self.assertFalse(_is_callable_valid_check(
            "azure_openai",
            "valid",
            {"supported_models": {"all_count": 100}},
        ))

    def test_verified_runtime_result_can_auto_vault(self):
        self.assertTrue(_is_callable_valid_check(
            "azure_openai",
            "valid",
            {
                "invocation_verification": "success",
                "verified_callable_targets": ["gpt-5.5"],
            },
        ))


if __name__ == "__main__":
    unittest.main()
