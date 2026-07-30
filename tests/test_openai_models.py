import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from checkers import openai
from checkers.openai import build_supported_models


class OpenAIModelSummaryTests(unittest.TestCase):
    def test_newer_gpt_version_is_featured_first(self):
        summary = build_supported_models(["gpt-4o", "gpt-5.5", "gpt-5.6", "gpt-5.6-mini"])

        text_models = summary["groups"]["text"]
        self.assertLess(text_models.index("gpt-5.6"), text_models.index("gpt-5.5"))
        self.assertLess(text_models.index("gpt-5.6"), text_models.index("gpt-5.6-mini"))
        self.assertEqual(summary["latest_by_family"]["gpt"], "gpt-5.6")
        self.assertEqual(summary["featured"][0], "gpt-5.6")
        self.assertEqual(
            [(x["label"], x["model"], x["supported"]) for x in summary["display_targets"]],
            [
                ("gpt-5.6", "gpt-5.6", True),
                ("gpt-5.5", "gpt-5.5", True),
                ("gpt-image-2", None, False),
                ("sora-2", None, False),
            ],
        )

    def test_models_are_grouped_by_capability(self):
        summary = build_supported_models([
            "gpt-image-2",
            "sora-2",
            "text-embedding-3-large",
            "omni-moderation-latest",
            "gpt-4o-audio-preview",
            "unfamiliar-model",
        ])

        self.assertIn("gpt-image-2", summary["groups"]["image"])
        self.assertIn("sora-2", summary["groups"]["video"])
        self.assertIn("text-embedding-3-large", summary["groups"]["embedding"])
        self.assertIn("omni-moderation-latest", summary["groups"]["moderation"])
        self.assertIn("gpt-4o-audio-preview", summary["groups"]["audio"])
        self.assertIn("unfamiliar-model", summary["groups"]["other"])
        self.assertEqual(summary["featured"], ["gpt-image-2", "sora-2"])

    def test_only_display_targets_are_featured(self):
        summary = build_supported_models([
            "gpt-5-pro",
            "o4-mini",
            "gpt-image-2",
            "sora-2-pro",
            "gpt-audio-mini-2025-12-15",
            "text-embedding-3-large",
            "omni-moderation-2024-09-26",
        ])

        self.assertEqual(summary["featured"], ["gpt-image-2", "sora-2-pro"])
        self.assertEqual(
            [(x["label"], x["model"], x["supported"]) for x in summary["display_targets"]],
            [
                ("gpt-5.6", None, False),
                ("gpt-5.5", None, False),
                ("gpt-image-2", "gpt-image-2", True),
                ("sora-2", "sora-2-pro", True),
            ],
        )

    def test_empty_and_duplicate_models_are_safe(self):
        empty = build_supported_models([])
        self.assertEqual(empty["all_count"], 0)
        self.assertEqual(empty["featured"], [])
        self.assertEqual(empty["groups"]["text"], [])

        deduped = build_supported_models(["gpt-5.6", "gpt-5.6", "", "  "])
        self.assertEqual(deduped["all_count"], 1)
        self.assertEqual(deduped["featured"], ["gpt-5.6"])


class OpenAIRateLimitHeaderTests(unittest.TestCase):
    def test_extracts_limit_remaining_reset_and_project_window(self):
        limits = openai._extract_rate_limits(httpx.Headers({
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "487",
            "x-ratelimit-reset-requests": "1s",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "198400",
            "x-ratelimit-reset-tokens": "6m0s",
            "x-ratelimit-limit-project-tokens": "60000",
            "x-ratelimit-remaining-project-tokens": "57000",
            "x-ratelimit-reset-project-tokens": "3s",
        }))

        self.assertEqual(limits["requests"], {
            "limit": 500,
            "remaining": 487,
            "reset": "1s",
        })
        self.assertEqual(limits["tokens"], {
            "limit": 200000,
            "remaining": 198400,
            "reset": "6m0s",
        })
        self.assertEqual(limits["project_tokens"], {
            "limit": 60000,
            "remaining": 57000,
            "reset": "3s",
        })

    def test_legacy_per_minute_headers_remain_compatible(self):
        rpm, tpm = openai._extract_rl(httpx.Headers({
            "x-ratelimit-limit-requests-per-minute": "60",
            "x-ratelimit-limit-tokens-per-minute": "150000",
        }))

        self.assertEqual((rpm, tpm), (60, 150000))

    def test_reasoning_probe_uses_responses_api(self):
        candidates = openai._probe_candidates(["o1-mini"])

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["model"], "o1-mini")
        self.assertEqual(candidates[0]["endpoint"], "responses")

    def test_extracts_numeric_tpm_observation_without_retaining_message(self):
        observation = openai._parse_error_limit_observation(
            "Rate limit reached on tokens per min (TPM): "
            "Limit 30,000, Used 29,500, Requested 1,000. "
            "Please try again in 2.5s."
        )

        self.assertEqual(observation, {
            "limit": 30000,
            "used": 29500,
            "requested": 1000,
            "dimension": "tokens",
            "retry_after": "2.5s",
            "source": "error_message",
        })

    def test_error_observation_requires_a_numeric_limit(self):
        self.assertEqual(
            openai._parse_error_limit_observation(
                "Rate limited. Please try again later."
            ),
            {},
        )


class OpenAICheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, models, post_responses, *, catalog_headers=None):
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200,
            headers=catalog_headers,
            json={"data": [{"id": model} for model in models]},
        )
        client.post.side_effect = post_responses
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=None)

        with patch("checkers.openai.httpx.AsyncClient", return_value=context):
            result = await openai.check("sk-proj-test-key")
        return result, client

    async def test_success_keeps_exact_window_and_marks_tier_as_estimate(self):
        result, client = await self.run_check(
            ["gpt-4o-mini"],
            [
                httpx.Response(
                    200,
                    headers={
                        "x-ratelimit-limit-requests": "500",
                        "x-ratelimit-remaining-requests": "487",
                        "x-ratelimit-reset-requests": "1s",
                        "x-ratelimit-limit-tokens": "200000",
                        "x-ratelimit-remaining-tokens": "198400",
                        "x-ratelimit-reset-tokens": "6m0s",
                        "x-ratelimit-limit-project-tokens": "60000",
                        "x-ratelimit-remaining-project-tokens": "57000",
                    },
                    json={"id": "chatcmpl-test"},
                ),
            ],
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Tier 1")
        self.assertEqual(result["rpm"], 500)
        self.assertEqual(result["tpm"], 200000)
        self.assertEqual(result["extra"]["tier_confidence"], "low")
        self.assertEqual(
            result["extra"]["tier_source"],
            "model_tpm_header_estimate",
        )
        self.assertEqual(
            result["extra"]["rate_limit_window"]["limits"]["requests"],
            {"limit": 500, "remaining": 487, "reset": "1s"},
        )
        self.assertEqual(
            result["extra"]["rate_limit_window"]["limits"]["project_tokens"],
            {"limit": 60000, "remaining": 57000},
        )
        self.assertEqual(result["extra"]["invocation_verification"], "success")
        self.assertNotIn("burst_probe", result["extra"])
        self.assertEqual(client.post.await_count, 1)

    async def test_valid_key_without_headers_gets_clear_reason_without_burst(self):
        result, client = await self.run_check(
            ["gpt-4o-mini"],
            [httpx.Response(200, json={"id": "chatcmpl-test"})],
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Unknown")
        self.assertEqual(
            result["extra"]["tier_reason_code"],
            "rate_limit_window_unavailable",
        )
        self.assertEqual(
            result["extra"]["tier_reason"],
            "Key 有效，但 OpenAI 未返回限流窗口",
        )
        self.assertEqual(result["extra"]["invocation_verification"], "success")
        self.assertNotIn("burst_probe", result["extra"])
        self.assertEqual(client.post.await_count, 1)

    async def test_immediate_429_is_reported_as_rate_limited_not_no_signal(self):
        result, _client = await self.run_check(
            ["gpt-4o-mini"],
            [
                httpx.Response(
                    429,
                    json={
                        "error": {
                            "code": "rate_limit_exceeded",
                            "type": "insufficient_quota",
                        },
                    },
                ),
            ],
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Unknown")
        self.assertEqual(
            result["extra"]["tier_reason_code"],
            "rate_limited_without_window",
        )
        self.assertEqual(
            result["extra"]["invocation_verification"],
            "rate_limited",
        )
        self.assertIn("探测返回 HTTP 429", result["extra"]["tier_reason"])
        self.assertIn("不代表整条 Key 当前全面限流", result["extra"]["tier_reason"])
        self.assertNotIn("探测时已限流", result["extra"]["tier_reason"])

    async def test_mixed_success_and_429_is_reported_as_partial_probe_result(self):
        result, client = await self.run_check(
            ["gpt-4o-mini", "gpt-4o"],
            [
                httpx.Response(
                    429,
                    json={"error": {"code": "rate_limit_exceeded"}},
                ),
                httpx.Response(200, json={"id": "chatcmpl-test"}),
            ],
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Unknown")
        self.assertEqual(
            result["extra"]["tier_reason_code"],
            "partial_probe_rate_limited",
        )
        self.assertEqual(
            result["extra"]["invocation_verification"],
            "success",
        )
        self.assertIn("至少一次最小模型调用成功", result["extra"]["tier_reason"])
        self.assertIn("另有探测请求返回 HTTP 429", result["extra"]["tier_reason"])
        self.assertNotIn("当前限流", result["extra"]["tier_reason"])
        self.assertEqual(client.post.await_count, 2)

    async def test_429_error_message_supplies_low_confidence_tpm_observation(self):
        result, _client = await self.run_check(
            ["gpt-4o-mini"],
            [
                httpx.Response(
                    429,
                    headers={"x-request-id": "req_safe_123"},
                    json={
                        "error": {
                            "code": "rate_limit_exceeded",
                            "message": (
                                "Rate limit reached on tokens per min (TPM): "
                                "Limit 30,000, Used 29,500, Requested 1,000. "
                                "Please try again in 2s."
                            ),
                        },
                    },
                ),
            ],
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Unknown")
        self.assertEqual(result["tpm"], 30000)
        self.assertEqual(
            result["extra"]["rate_limit_source"],
            "error_message_observation",
        )
        self.assertEqual(
            result["extra"]["tier_reason_code"],
            "rate_limit_observed_from_error",
        )
        self.assertEqual(result["extra"]["retry_after"], "2s")
        self.assertEqual(result["extra"]["request_id"], "req_safe_123")
        self.assertEqual(
            result["extra"]["rate_limit_observation"],
            {
                "model": "gpt-4o-mini",
                "endpoint": "chat_completions",
                "limit": 30000,
                "used": 29500,
                "requested": 1000,
                "dimension": "tokens",
                "retry_after": "2s",
                "source": "error_message",
            },
        )
        self.assertNotIn("Rate limit reached", str(result))

    async def test_retry_after_is_kept_without_inventing_tpm(self):
        result, _client = await self.run_check(
            ["gpt-4o-mini"],
            [
                httpx.Response(
                    429,
                    headers={
                        "retry-after": "4",
                        "x-request-id": "req_retry_123",
                    },
                    json={"error": {"code": "rate_limit_exceeded"}},
                ),
            ],
        )

        self.assertEqual(result["status"], "valid")
        self.assertIsNone(result["tpm"])
        self.assertEqual(result["extra"]["retry_after"], "4")
        self.assertEqual(
            result["extra"]["tier_reason_code"],
            "rate_limited_retry_after",
        )
        self.assertIn("探测返回 HTTP 429", result["extra"]["tier_reason"])
        self.assertIn("不代表整条 Key 当前全面限流", result["extra"]["tier_reason"])

    async def test_insufficient_quota_stays_distinct(self):
        result, _client = await self.run_check(
            ["gpt-4o-mini"],
            [
                httpx.Response(
                    429,
                    json={"error": {"code": "insufficient_quota"}},
                ),
            ],
        )

        self.assertEqual(result["status"], "no_quota")
        self.assertIsNone(result["tier"])
        self.assertEqual(result["extra"]["invocation_verification"], "no_quota")
        self.assertEqual(result["extra"]["quota_reason"], "insufficient_quota")

    async def test_official_billing_429_codes_are_not_called_temporary_limits(self):
        cases = (
            "credit_balance_exhausted",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
        )
        for error_code in cases:
            with self.subTest(error_code=error_code):
                result, _client = await self.run_check(
                    ["gpt-4o-mini"],
                    [
                        httpx.Response(
                            429,
                            json={
                                "error": {
                                    "code": error_code,
                                    "type": "insufficient_quota",
                                },
                            },
                        ),
                    ],
                )

                self.assertEqual(result["status"], "no_quota")
                self.assertEqual(
                    result["extra"]["invocation_verification"],
                    "no_quota",
                )
                self.assertEqual(result["extra"]["quota_reason"], error_code)

    async def test_newly_discovered_text_model_uses_responses_api(self):
        result, client = await self.run_check(
            ["gpt-5.6-mini"],
            [
                httpx.Response(
                    200,
                    headers={
                        "x-ratelimit-limit-requests": "1000",
                        "x-ratelimit-remaining-requests": "999",
                    },
                    json={"id": "resp-test"},
                ),
            ],
        )

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["tier"], "Unknown")
        self.assertEqual(
            result["extra"]["tier_reason_code"],
            "official_tier_unavailable",
        )
        request = client.post.await_args
        self.assertEqual(request.args[0], f"{openai.BASE}/v1/responses")
        self.assertEqual(request.kwargs["json"], {
            "model": "gpt-5.6-mini",
            "input": ".",
            "max_output_tokens": 16,
            "store": False,
        })
        self.assertEqual(
            result["extra"]["rate_limit_window"]["endpoint"],
            "responses",
        )


if __name__ == "__main__":
    unittest.main()
