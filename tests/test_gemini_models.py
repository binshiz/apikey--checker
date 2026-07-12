import unittest

import httpx

from checkers.gemini import _burst_test, _validate_key, build_supported_models


class GeminiModelSummaryTests(unittest.TestCase):
    def test_targets_require_generate_content_support(self):
        summary = build_supported_models([
            {
                "name": "models/gemini-3.5-flash",
                "displayName": "Gemini 3.5 Flash",
                "supportedGenerationMethods": ["generateContent", "countTokens"],
            },
            {
                "name": "models/gemini-3.1-pro-preview",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-3-flash-preview",
                "supportedGenerationMethods": [],
            },
            {
                "name": "models/gemini-2.5-pro",
                "supportedGenerationMethods": ["embedContent"],
            },
            {
                "name": "models/gemini-embedding-2",
                "supportedGenerationMethods": ["embedContent"],
            },
            {
                "name": "models/imagen-4.0-generate-001",
                "supportedGenerationMethods": ["predict"],
            },
            {
                "name": "models/gemini-2.5-flash-image",
                "supportedGenerationMethods": ["generateContent"],
            },
            # Duplicate IDs are compared without the resource-name prefix.
            {"name": "gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
        ])

        self.assertEqual(summary["all_count"], 7)
        self.assertEqual(summary["callable_count"], 3)
        self.assertIn("gemini-embedding-2", summary["groups"]["embedding"])
        self.assertIn("imagen-4.0-generate-001", summary["groups"]["image"])
        self.assertIn("gemini-2.5-flash-image", summary["groups"]["image"])
        self.assertEqual(summary["method_counts"]["generateContent"], 3)
        self.assertEqual(
            [(target["label"], target["model"], target["supported"]) for target in summary["display_targets"]],
            [
                ("gemini-3.5-flash", "gemini-3.5-flash", True),
                ("gemini-3.1-pro", "gemini-3.1-pro-preview", True),
                ("gemini-3-flash", None, False),
                ("gemini-2.5-pro", None, False),
            ],
        )

    def test_legacy_string_ids_remain_supported_and_are_sorted(self):
        summary = build_supported_models([
            "models/gemini-2.5-pro",
            "gemini-3-flash",
            "gemini-3.5-flash",
            "gemini-3.5-flash",
            "",
        ])

        self.assertEqual(summary["all_count"], 3)
        self.assertEqual(summary["callable_count"], 3)
        self.assertEqual(summary["models_preview"][0], "gemini-3.5-flash")


class GeminiModelListTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_models_follows_page_tokens(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.params.get("pageToken") == "page-2":
                return httpx.Response(200, json={
                    "models": [{
                        "name": "models/gemini-2.5-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    }],
                })
            return httpx.Response(200, json={
                "models": [{
                    "name": "models/gemini-3.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                }],
                "nextPageToken": "page-2",
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            valid, error, models = await _validate_key(client, "test-key")

        self.assertTrue(valid)
        self.assertIsNone(error)
        self.assertEqual(len(models), 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.params.get("pageSize"), "1000")
        self.assertEqual(requests[1].url.params.get("pageToken"), "page-2")

    async def test_rate_limited_model_list_is_valid_but_incomplete(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            valid, error, models = await _validate_key(client, "test-key")

        self.assertTrue(valid)
        self.assertEqual(error, "models list rate limited")
        self.assertEqual(models, [])

    async def test_unavailable_probe_stops_after_single_preflight(self):
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await _burst_test(client, "test-key", "missing-model", cap=120)

        self.assertTrue(result["model_unavailable"])
        self.assertEqual(result["total_sent"], 1)
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
