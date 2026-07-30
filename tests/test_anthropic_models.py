import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from checkers import anthropic
from checkers.anthropic import build_supported_models


ANTHROPIC_KEY = "sk-ant-api03-" + "ab" * 32


class AnthropicModelSummaryTests(unittest.TestCase):
    def test_rate_limit_headers_include_remaining_capacity_and_reset(self):
        limits = anthropic._extract_rate_limits(httpx.Headers({
            "anthropic-ratelimit-requests-limit": "60",
            "anthropic-ratelimit-requests-remaining": "59",
            "anthropic-ratelimit-requests-reset": "2026-07-27T12:00:00Z",
            "anthropic-ratelimit-input-tokens-limit": "30000",
            "anthropic-ratelimit-input-tokens-remaining": "29000",
            "anthropic-ratelimit-output-tokens-limit": "8000",
            "anthropic-ratelimit-output-tokens-remaining": "7999",
            "anthropic-ratelimit-output-tokens-reset": "2026-07-27T12:00:01Z",
            "anthropic-ratelimit-tokens-remaining": "28000",
        }))

        self.assertEqual(limits["requests"], {
            "limit": 60,
            "remaining": 59,
            "reset": "2026-07-27T12:00:00Z",
        })
        self.assertEqual(limits["input_tokens"], {
            "limit": 30000,
            "remaining": 29000,
        })
        self.assertEqual(limits["output_tokens"], {
            "limit": 8000,
            "remaining": 7999,
            "reset": "2026-07-27T12:00:01Z",
        })
        self.assertEqual(limits["tokens"], {"remaining": 28000})

    def test_display_targets_keep_requested_order(self):
        summary = build_supported_models([
            {"id": "claude-sonnet-4-6"},
            {"id": "claude-opus-4-7"},
            {"id": "claude-fable-5"},
            {"id": "claude-opus-5"},
            {"id": "claude-opus-4-8"},
        ])

        self.assertEqual(
            [(x["label"], x["model"], x["supported"]) for x in summary["display_targets"]],
            [
                ("opus-5", "claude-opus-5", True),
                ("fable-5", "claude-fable-5", True),
                ("opus-4-8", "claude-opus-4-8", True),
                ("opus-4-7", "claude-opus-4-7", True),
                ("sonnet-4-6", "claude-sonnet-4-6", True),
            ],
        )

    def test_claude_prefixed_model_matches_short_target(self):
        summary = build_supported_models([
            {"id": "claude-opus-4-8-20260701", "display_name": "Claude Opus 4.8"},
        ])

        opus = next(
            target
            for target in summary["display_targets"]
            if target["label"] == "opus-4-8"
        )
        self.assertEqual(opus["label"], "opus-4-8")
        self.assertEqual(opus["model"], "claude-opus-4-8-20260701")
        self.assertTrue(opus["supported"])
        self.assertEqual(opus["display_name"], "Claude Opus 4.8")

    def test_missing_targets_are_marked_unsupported(self):
        summary = build_supported_models([{"id": "claude-haiku-4-5"}])

        self.assertEqual(summary["all_count"], 1)
        self.assertEqual(summary["featured"], [])
        self.assertEqual(
            [(x["label"], x["model"], x["supported"]) for x in summary["display_targets"]],
            [
                ("opus-5", None, False),
                ("fable-5", None, False),
                ("opus-4-8", None, False),
                ("opus-4-7", None, False),
                ("sonnet-4-6", None, False),
            ],
        )

    def test_empty_duplicate_and_unknown_models_are_safe(self):
        empty = build_supported_models([])
        self.assertEqual(empty["all_count"], 0)
        self.assertEqual(empty["models_preview"], [])

        deduped = build_supported_models([
            "claude-opus-4-8",
            {"id": "claude-opus-4-8"},
            "",
            {"id": "unknown-model"},
        ])
        self.assertEqual(deduped["all_count"], 2)
        self.assertEqual(deduped["featured"], ["claude-opus-4-8"])
        self.assertEqual(deduped["models_preview"], ["claude-opus-4-8", "unknown-model"])


class AnthropicOpus5ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def probe_with_handler(self, handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await anthropic._probe_opus5(client, ANTHROPIC_KEY)

    async def test_success_records_only_model_and_token_usage(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["x-api-key"], ANTHROPIC_KEY)
            payload = request.read().decode()
            self.assertIn('"model":"claude-opus-5"', payload)
            self.assertIn('"max_tokens":1', payload)
            self.assertIn('"content":"."', payload)
            return httpx.Response(
                200,
                headers={
                    "anthropic-ratelimit-requests-limit": "60",
                    "anthropic-ratelimit-requests-remaining": "59",
                    "anthropic-ratelimit-requests-reset": "2026-07-27T12:00:00Z",
                    "anthropic-ratelimit-input-tokens-limit": "30000",
                    "anthropic-ratelimit-input-tokens-remaining": "29992",
                },
                json={
                    "model": "claude-opus-5-20260724",
                    "content": [{"type": "text", "text": "never persist this"}],
                    "usage": {"input_tokens": 8, "output_tokens": 1},
                },
            )

        probe = await self.probe_with_handler(handler)

        self.assertEqual(probe["status"], "callable")
        self.assertEqual(probe["model"], anthropic.OPUS5_MODEL)
        self.assertEqual(probe["resolved_model"], "claude-opus-5-20260724")
        self.assertEqual(probe["rpm"], 60)
        self.assertEqual(probe["input_tpm"], 30000)
        self.assertEqual(probe["rate_limits"]["requests"], {
            "limit": 60,
            "remaining": 59,
            "reset": "2026-07-27T12:00:00Z",
        })
        self.assertEqual(probe["rate_limits"]["input_tokens"], {
            "limit": 30000,
            "remaining": 29992,
        })
        self.assertEqual(
            probe["token_usage"],
            {"input_tokens": 8, "output_tokens": 1, "total_tokens": 9},
        )
        self.assertNotIn("never persist this", str(probe))

    async def test_http_statuses_are_kept_distinct(self):
        cases = [
            (400, "Your credit balance is too low", "no_quota"),
            (401, "authentication failed", "authentication_failed"),
            (402, "payment required", "no_quota"),
            (403, "permission denied", "access_denied"),
            (404, "model not found", "model_unavailable"),
            (429, "rate limited", "rate_limited"),
            (500, "upstream failed", "upstream_error"),
            (422, "bad request", "request_rejected"),
        ]
        for status_code, message, expected in cases:
            with self.subTest(status_code=status_code):
                probe = await self.probe_with_handler(
                    lambda _request, code=status_code, text=message: httpx.Response(
                        code,
                        json={"error": {"type": "fixture_error", "message": text}},
                    )
                )
                self.assertEqual(probe["status"], expected)
                self.assertEqual(probe["http_status"], status_code)

    async def test_timeout_is_not_reported_as_model_denial(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        probe = await self.probe_with_handler(handler)

        self.assertEqual(probe["status"], "timeout")
        self.assertEqual(probe["error"], "timeout")

    async def test_error_body_cannot_leak_full_key(self):
        probe = await self.probe_with_handler(
            lambda _request: httpx.Response(
                403,
                json={"error": {
                    "type": ANTHROPIC_KEY,
                    "message": f"credential {ANTHROPIC_KEY} has no access",
                }},
            )
        )

        self.assertEqual(probe["status"], "access_denied")
        self.assertNotIn(ANTHROPIC_KEY, str(probe))
        self.assertIn("[redacted]", probe["error"])
        self.assertEqual(probe["error_code"], "[redacted]")

    async def test_full_check_uses_opus_runtime_proof_without_burst(self):
        client = AsyncMock()
        client.post.side_effect = [
            httpx.Response(
                200,
                headers={
                    "anthropic-ratelimit-requests-limit": "60",
                    "anthropic-ratelimit-requests-remaining": "59",
                    "anthropic-ratelimit-requests-reset": "2026-07-27T12:00:00Z",
                    "anthropic-ratelimit-input-tokens-limit": "30000",
                    "anthropic-ratelimit-input-tokens-remaining": "29992",
                },
                json={"model": anthropic.PROBE_MODEL},
            ),
            httpx.Response(
                200,
                json={
                    "model": anthropic.OPUS5_MODEL,
                    "usage": {"input_tokens": 2, "output_tokens": 1},
                },
            ),
        ]
        client.get.return_value = httpx.Response(
            200,
            json={"data": [{"id": anthropic.OPUS5_MODEL}]},
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)

        with patch("checkers.anthropic.httpx.AsyncClient", return_value=context):
            result = await anthropic.check(ANTHROPIC_KEY)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Tier 1")
        self.assertTrue(result["extra"]["has_opus_5"])
        self.assertEqual(result["extra"]["opus5_probe"]["status"], "callable")
        self.assertEqual(result["extra"]["source"], "header")
        self.assertEqual(result["extra"]["credit_status"], "available")
        self.assertEqual(
            result["extra"]["rate_limit_window"],
            {
                "model": anthropic.PROBE_MODEL,
                "limits": {
                    "requests": {
                        "limit": 60,
                        "remaining": 59,
                        "reset": "2026-07-27T12:00:00Z",
                    },
                    "input_tokens": {
                        "limit": 30000,
                        "remaining": 29992,
                    },
                },
            },
        )
        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(
            client.post.await_args_list[1].kwargs["json"]["model"],
            anthropic.OPUS5_MODEL,
        )

    async def test_full_check_marks_credit_balance_error_as_depleted(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            400,
            json={"error": {"message": "Your credit balance is too low"}},
        )
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)

        with patch("checkers.anthropic.httpx.AsyncClient", return_value=context):
            result = await anthropic.check(ANTHROPIC_KEY)

        self.assertEqual(result["status"], "no_quota")
        self.assertEqual(result["extra"]["credit_status"], "depleted")
        self.assertEqual(result["extra"]["opus5_probe"]["status"], "no_quota")
        client.get.assert_not_awaited()
        self.assertEqual(client.post.await_count, 1)


if __name__ == "__main__":
    unittest.main()
