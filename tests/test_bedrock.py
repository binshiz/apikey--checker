import asyncio
from decimal import Decimal
import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx

from checkers import bedrock


ACCESS_KEY_ID = "AKIAABCDEFGHIJKLMNOP"
SECRET_ACCESS_KEY = "a/+=" + "b" * 36
KEY = f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}"
BEDROCK_API_KEY = "ABSK" + "QmVkcm9ja0FQSUtleS0" + "A" * 80 + "="


class AwsError(Exception):
    def __init__(self, code, message="not persisted"):
        super().__init__("message intentionally ignored")
        self.response = {"Error": {"Code": code, "Message": message}}


class StsClient:
    def __init__(self, error=None):
        self.error = error

    def get_caller_identity(self):
        if self.error:
            raise self.error
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}


class ProfileClient:
    def __init__(self, pages, foundation_models=None, foundation_error=None):
        self.pages = pages
        self.foundation_models = foundation_models or []
        self.foundation_error = foundation_error
        self.calls = []

    def list_inference_profiles(self, **kwargs):
        self.calls.append(kwargs)
        value = self.pages.get(kwargs.get("nextToken"))
        if isinstance(value, BaseException):
            raise value
        return value or {"inferenceProfileSummaries": []}

    def list_foundation_models(self):
        if self.foundation_error:
            raise self.foundation_error
        return {"modelSummaries": self.foundation_models}


class RuntimeClient:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.get(kwargs["modelId"], self.outcomes.get("*", {}))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class QuotaPaginator:
    def __init__(self, pages=None, error=None):
        self.pages = pages or []
        self.error = error

    def paginate(self, **kwargs):
        if self.error:
            raise self.error
        self.kwargs = kwargs
        return iter(self.pages)


class QuotaClient:
    def __init__(self, pages=None, error=None):
        self.paginator = QuotaPaginator(pages, error)

    def get_paginator(self, name):
        if name != "list_service_quotas":
            raise AssertionError(name)
        return self.paginator


class FakeSession:
    def __init__(self, services):
        self.services = services
        self.requested_services = []

    def client(self, service_name, **kwargs):
        self.requested_services.append(service_name)
        value = self.services[service_name]
        if isinstance(value, BaseException):
            raise value
        return value


def profile_page(*model_ids, next_token=None):
    page = {
        "inferenceProfileSummaries": [
            {"inferenceProfileId": model_id} for model_id in model_ids
        ]
    }
    if next_token:
        page["nextToken"] = next_token
    return page


class BedrockCheckerTests(unittest.TestCase):
    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def factory_for(self, sessions, calls):
        def factory(access_key_id, secret_access_key, region):
            self.assertEqual(access_key_id, ACCESS_KEY_ID)
            self.assertEqual(secret_access_key, SECRET_ACCESS_KEY)
            calls.append(region)
            return sessions[region]

        return factory

    def test_native_api_key_deep_check_discovers_invokes_and_builds_mappings(self):
        progress = []
        runtime_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Authorization"], f"Bearer {BEDROCK_API_KEY}")
            host = request.url.host
            is_runtime = host.startswith("bedrock-runtime.")
            region = host.removeprefix(
                "bedrock-runtime." if is_runtime else "bedrock."
            ).removesuffix(".amazonaws.com")
            if region == "ap-south-2":
                return httpx.Response(
                    403,
                    headers={"x-amzn-errortype": "AccessDeniedException"},
                    json={"message": "denied"},
                )
            if is_runtime:
                runtime_requests.append(request)
                self.assertEqual(request.headers["Content-Type"], "application/json")
                self.assertEqual(request.headers["X-Amzn-Bedrock-Accept"], "application/json")
                return httpx.Response(200, json={"content": [{"text": SECRET_ACCESS_KEY}]})
            if request.url.path == "/inference-profiles":
                scope = "us" if region == "us-east-1" else "eu"
                return httpx.Response(200, json={
                    "inferenceProfileSummaries": [
                        {"inferenceProfileId": f"{scope}.anthropic.claude-fable-5"},
                        {"inferenceProfileId": "global.anthropic.claude-opus-4-8-v1:0"},
                        {"inferenceProfileId": f"{scope}.anthropic.claude-sonnet-4-6"},
                    ],
                })
            return httpx.Response(
                200,
                json={
                    "modelSummaries": [
                        {"modelId": "amazon.nova-lite-v1:0"},
                        {
                            "modelId": f"anthropic.claude-opus-4-8-{region}-v1:0",
                            "modelName": "Claude Opus 4.8",
                            "providerName": "Anthropic",
                        },
                    ]
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch.object(
                bedrock,
                "configured_api_key_regions",
                return_value=("us-east-1", "ap-south-2", "eu-west-1"),
            ),
            patch.object(bedrock, "_new_bearer_client", return_value=client) as client_factory,
            patch.object(bedrock, "_new_session", side_effect=AssertionError("SigV4 path used")),
        ):
            result = self.run_async(
                bedrock.deep_check(
                    BEDROCK_API_KEY,
                    proxy="socks5://127.0.0.1:1080",
                    progress_callback=progress.append,
                )
            )

        client_factory.assert_called_once_with("socks5://127.0.0.1:1080")
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["extra"]["credential_type"], "bedrock_api_key")
        self.assertEqual(result["extra"]["credential_status"], "bedrock_verified")
        self.assertEqual(result["extra"]["invocation_verification"], "success")
        self.assertEqual(
            result["extra"]["model_summary"]["authorized_regions"],
            ["us-east-1", "eu-west-1"],
        )
        self.assertEqual(
            result["extra"]["model_summary"]["denied_regions"],
            ["ap-south-2"],
        )
        self.assertEqual(result["extra"]["model_summary"]["catalog_model_count"], 3)
        self.assertEqual(
            result["extra"]["model_summary"]["successful_regions"],
            ["eu-west-1", "us-east-1"],
        )
        self.assertEqual(
            result["extra"]["gateway_mappings_by_region"],
            {
                "eu-west-1": {
                    "claude-fable-5": "eu.anthropic.claude-fable-5",
                    "claude-opus-4-8": "global.anthropic.claude-opus-4-8-v1:0",
                },
                "us-east-1": {
                    "claude-fable-5": "us.anthropic.claude-fable-5",
                    "claude-opus-4-8": "global.anthropic.claude-opus-4-8-v1:0",
                },
            },
        )
        self.assertEqual(len(runtime_requests), 4)
        self.assertCountEqual(progress, ["us-east-1", "ap-south-2", "eu-west-1"])
        serialized = json.dumps(result)
        self.assertNotIn(BEDROCK_API_KEY, serialized)
        self.assertNotIn(SECRET_ACCESS_KEY, serialized)

    def test_native_api_key_all_unauthorized_is_invalid(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                headers={"x-amzn-errortype": "UnrecognizedClientException"},
                json={"message": BEDROCK_API_KEY},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with (
            patch.object(
                bedrock,
                "configured_api_key_regions",
                return_value=("us-east-1", "us-west-2"),
            ),
            patch.object(bedrock, "_new_bearer_client", return_value=client),
        ):
            result = self.run_async(bedrock.deep_check(BEDROCK_API_KEY))

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["extra"]["credential_status"], "invalid")
        self.assertEqual(
            result["extra"]["model_summary"]["invalid_regions"],
            ["us-east-1", "us-west-2"],
        )
        self.assertNotIn(BEDROCK_API_KEY, json.dumps(result))

    def test_quick_check_paginates_selects_latest_and_stops_early(self):
        opus4 = "us.anthropic.claude-opus-4-20250514-v1:0"
        opus48 = "us.anthropic.claude-opus-4-8-20260701-v1:0"
        profiles = ProfileClient({
            None: profile_page(opus4, "us.anthropic.claude-sonnet-4-6", next_token="page-2"),
            "page-2": profile_page(opus48),
        })
        runtime = RuntimeClient({opus48: {"output": {"reply": SECRET_ACCESS_KEY}}})
        east = FakeSession({"sts": StsClient(), "bedrock": profiles, "bedrock-runtime": runtime})
        west = FakeSession({"sts": StsClient()})
        session_calls = []

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1", "us-west-2")),
            patch.object(bedrock, "_new_session", side_effect=self.factory_for({"us-east-1": east, "us-west-2": west}, session_calls)),
            patch.object(bedrock, "_attach_socks_proxy") as attach_proxy,
        ):
            result = self.run_async(bedrock.check(KEY, proxy="socks5://user:password@example"))

        self.assertEqual(result["status"], "valid")
        self.assertIsNone(result["tier"])
        self.assertIsNone(result["rpm"])
        self.assertIsNone(result["tpm"])
        self.assertEqual(result["extra"]["regions_checked"], ["us-east-1"])
        self.assertEqual(result["extra"]["model_summary"]["successful_versions"], ["4.8"])
        self.assertEqual(result["extra"]["model_summary"]["supported_models"], [opus4, opus48])
        self.assertEqual(result["extra"]["model_summary"]["supported_regions"], ["us-east-1"])
        self.assertEqual(
            result["extra"]["model_summary"]["models_by_region"]["us-east-1"],
            [opus4, opus48],
        )
        self.assertTrue(result["extra"]["proxy_used"])
        self.assertFalse(result["extra"]["proxy_ignored"])
        self.assertEqual(attach_proxy.call_count, 3)
        self.assertEqual([call.get("nextToken") for call in profiles.calls], [None, "page-2"])
        self.assertEqual([call["modelId"] for call in runtime.calls], [opus48])
        self.assertEqual(runtime.calls[0]["contentType"], "application/json")
        self.assertEqual(runtime.calls[0]["accept"], "application/json")
        self.assertEqual(json.loads(runtime.calls[0]["body"]), bedrock._INVOKE_BODY)
        self.assertNotIn("service-quotas", east.requested_services)
        self.assertNotIn("us-west-2", session_calls)
        serialized = json.dumps(result)
        self.assertNotIn(SECRET_ACCESS_KEY, serialized)
        self.assertNotIn(ACCESS_KEY_ID, serialized)
        self.assertNotIn("reply", serialized)
        self.assertNotIn("socks5://", serialized)

    def test_invalid_sts_credentials_stop_before_bedrock(self):
        session = FakeSession({"sts": StsClient(AwsError("InvalidClientTokenId"))})
        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["extra"]["credential_status"], "invalid")
        self.assertEqual(result["extra"]["regions_checked"], [])

    def test_quick_check_prioritizes_and_confirms_claude_fable_5(self):
        fable = "global.anthropic.claude-fable-5"
        opus = "us.anthropic.claude-opus-4-8-v1:0"
        runtime = RuntimeClient({fable: {}})
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(opus, fable)}),
            "bedrock-runtime": runtime,
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        fable_summary = result["extra"]["model_summary"]["fable_5"]
        self.assertEqual(result["status"], "valid")
        self.assertEqual([call["modelId"] for call in runtime.calls], [fable])
        self.assertEqual(json.loads(runtime.calls[0]["body"]), bedrock._FABLE_5_INVOKE_BODY)
        self.assertEqual(fable_summary["status"], "supported")
        self.assertIs(fable_summary["supported"], True)
        self.assertEqual(fable_summary["successful_models"], [fable])
        self.assertEqual(fable_summary["successful_regions"], ["us-east-1"])
        self.assertIs(result["extra"]["has_claude_fable_5"], True)
        self.assertEqual(result["extra"]["fable_5_status"], "supported")
        self.assertEqual(result["extra"]["model_summary"]["opus_versions"], ["4.8"])
        self.assertEqual(
            result["extra"]["gateway_mapping"],
            {"claude-fable-5": fable},
        )
        self.assertEqual(
            result["extra"]["gateway_mappings_by_region"],
            {"us-east-1": {"claude-fable-5": fable}},
        )
        self.assertEqual(
            result["extra"]["gateway_primary_model"],
            "claude-fable-5",
        )
        self.assertEqual(
            result["extra"]["gateway_primary_regions"],
            ["us-east-1"],
        )
        self.assertEqual(
            result["extra"]["gateway_region_groups"],
            [{
                "kind": "fable_5",
                "regions": ["us-east-1"],
                "mapping": {"claude-fable-5": fable},
            }],
        )

    def test_gateway_mapping_uses_only_successes_and_prefers_global_routes(self):
        global_opus = "global.anthropic.claude-opus-4-6-v1"
        regional_opus = "us.anthropic.claude-opus-4-6-v1"
        regional_new_opus = "us.anthropic.claude-opus-4-7"
        global_new_opus = "global.anthropic.claude-opus-4-7"
        fable = "us.anthropic.claude-fable-5"
        mapping, by_region = bedrock.build_gateway_mappings({
            "us-east-1": {
                "invocations": [
                    {"model_id": regional_opus, "status": "success"},
                    {"model_id": regional_new_opus, "status": "success"},
                    {"model_id": global_new_opus, "status": "error"},
                    {"model_id": fable, "status": "success"},
                    {
                        "model_id": "global.anthropic.claude-opus-4-8",
                        "status": "throttled",
                    },
                ],
            },
            "eu-west-1": {
                "invocations": [
                    {"model_id": global_opus, "status": "success"},
                ],
            },
        })

        self.assertEqual(mapping, {
            "claude-fable-5": fable,
            "claude-opus-4-6": global_opus,
            "claude-opus-4-7": regional_new_opus,
        })
        self.assertNotIn("claude-opus-4-8", mapping)
        self.assertEqual(by_region, {
            "eu-west-1": {
                "claude-opus-4-6": global_opus,
            },
            "us-east-1": {
                "claude-fable-5": fable,
                "claude-opus-4-6": regional_opus,
                "claude-opus-4-7": regional_new_opus,
            },
        })
        self.assertEqual(
            bedrock.select_gateway_primary_model(by_region),
            ("claude-fable-5", ["us-east-1"]),
        )
        self.assertEqual(
            bedrock.build_gateway_region_groups(by_region),
            [
                {
                    "kind": "fable_5",
                    "regions": ["us-east-1"],
                    "mapping": {
                        "claude-fable-5": fable,
                        "claude-opus-4-6": regional_opus,
                        "claude-opus-4-7": regional_new_opus,
                    },
                },
                {
                    "kind": "other_models",
                    "regions": ["eu-west-1"],
                    "mapping": {
                        "claude-opus-4-6": global_opus,
                    },
                },
            ],
        )

    def test_common_gateway_mapping_requires_same_target_in_every_region(self):
        by_region = {
            "us-east-1": {
                "claude-fable-5": "us.anthropic.claude-fable-5",
                "claude-opus-4-8": "global.anthropic.claude-opus-4-8",
            },
            "us-west-2": {
                "claude-fable-5": "us.anthropic.claude-fable-5",
                "claude-opus-4-8": "us.anthropic.claude-opus-4-8",
            },
        }

        self.assertEqual(
            bedrock.build_common_gateway_mapping(
                by_region,
                ["us-east-1", "us-west-2"],
            ),
            {"claude-fable-5": "us.anthropic.claude-fable-5"},
        )

    def test_incompatible_regions_are_partitioned_into_route_groups(self):
        by_region = {
            "ap-northeast-1": {
                "claude-opus-4-6": "global.anthropic.claude-opus-4-6-v1",
            },
            "ap-southeast-2": {
                "claude-opus-4-5":
                    "global.anthropic.claude-opus-4-5-v1:0",
                "claude-opus-4-6": "au.anthropic.claude-opus-4-6-v1",
            },
            "eu-west-1": {
                "claude-opus-4-6": "eu.anthropic.claude-opus-4-6-v1",
            },
            "us-west-1": {
                "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
            },
        }

        self.assertEqual(
            bedrock.build_gateway_region_groups(by_region),
            [{
                "kind": "other_models",
                "regions": [
                    "ap-northeast-1",
                    "ap-southeast-2",
                    "eu-west-1",
                    "us-west-1",
                ],
                "mapping": {},
                "route_groups": [
                    {
                        "regions": ["ap-northeast-1"],
                        "mapping": {
                            "claude-opus-4-6":
                                "global.anthropic.claude-opus-4-6-v1",
                        },
                    },
                    {
                        "regions": ["eu-west-1"],
                        "mapping": {
                            "claude-opus-4-6":
                                "eu.anthropic.claude-opus-4-6-v1",
                        },
                    },
                    {
                        "regions": ["us-west-1"],
                        "mapping": {
                            "claude-opus-4-6":
                                "us.anthropic.claude-opus-4-6-v1",
                        },
                    },
                    {
                        "regions": ["ap-southeast-2"],
                        "mapping": {
                            "claude-opus-4-5":
                                "global.anthropic.claude-opus-4-5-v1:0",
                            "claude-opus-4-6":
                                "au.anthropic.claude-opus-4-6-v1",
                        },
                    },
                ],
            }],
        )

    def test_gateway_primary_falls_back_to_newest_successful_opus(self):
        by_region = {
            "us-east-1": {
                "claude-opus-4-6": "global.anthropic.claude-opus-4-6-v1",
            },
            "us-east-2": {
                "claude-opus-4-8": "global.anthropic.claude-opus-4-8",
            },
            "us-west-2": {
                "claude-opus-4-8": "us.anthropic.claude-opus-4-8",
            },
        }

        self.assertEqual(
            bedrock.select_gateway_primary_model(by_region),
            ("claude-opus-4-8", ["us-east-2", "us-west-2"]),
        )

    def test_quick_check_falls_back_between_fable_5_routes(self):
        regional_fable = "us.anthropic.claude-fable-5"
        global_fable = "global.anthropic.claude-fable-5"
        runtime = RuntimeClient({
            regional_fable: AwsError("AccessDeniedException"),
            global_fable: {},
        })
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(global_fable, regional_fable)}),
            "bedrock-runtime": runtime,
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "valid")
        self.assertEqual(
            [call["modelId"] for call in runtime.calls],
            [regional_fable, global_fable],
        )
        self.assertEqual(result["extra"]["fable_5_status"], "supported")

    def test_invoke_only_iam_can_confirm_fable_without_list_permissions(self):
        fable = "anthropic.claude-fable-5"
        access_denied = AwsError("AccessDeniedException")
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient(
                {None: access_denied},
                foundation_error=access_denied,
            ),
            "bedrock-runtime": RuntimeClient({fable: {}}),
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        summary = result["extra"]["model_summary"]
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["extra"]["fable_5_status"], "supported")
        self.assertEqual(summary["fable_5"]["discovered_models"], [])
        self.assertEqual(summary["fable_5"]["successful_models"], [fable])

    def test_fable_data_retention_requirement_is_distinct_from_opus_access(self):
        fable = "us.anthropic.claude-fable-5"
        opus = "us.anthropic.claude-opus-4-8-v1:0"
        runtime = RuntimeClient({
            fable: AwsError(
                "ValidationException",
                "Set data retention mode to provider_data_share before invoking this model",
            ),
            opus: {},
        })
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(opus, fable)}),
            "bedrock-runtime": runtime,
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        fable_summary = result["extra"]["model_summary"]["fable_5"]
        self.assertEqual(result["status"], "valid")
        self.assertEqual([call["modelId"] for call in runtime.calls], [fable, opus])
        self.assertEqual(fable_summary["status"], "data_retention_required")
        self.assertIs(fable_summary["supported"], False)
        self.assertTrue(fable_summary["data_retention_required"])
        self.assertNotIn("Set data retention", json.dumps(result))

    def test_fable_throttle_confirms_model_support(self):
        fable = "global.anthropic.claude-fable-5"
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(fable)}),
            "bedrock-runtime": RuntimeClient({fable: AwsError("ThrottlingException")}),
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        fable_summary = result["extra"]["model_summary"]["fable_5"]
        self.assertEqual(result["status"], "no_quota")
        self.assertEqual(fable_summary["status"], "throttled")
        self.assertIs(fable_summary["supported"], True)
        self.assertEqual(fable_summary["throttled_regions"], ["us-east-1"])

    def test_quick_check_falls_back_to_an_older_opus_version(self):
        opus48 = "us.anthropic.claude-opus-4-8-v1:0"
        opus47 = "us.anthropic.claude-opus-4-7-v1:0"
        runtime = RuntimeClient({
            opus48: AwsError("ServiceUnavailableException"),
            opus47: {},
        })
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(opus47, opus48)}),
            "bedrock-runtime": runtime,
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "valid")
        self.assertEqual([call["modelId"] for call in runtime.calls], [opus48, opus47])
        self.assertEqual(result["extra"]["availability_basis"], "invoke_model")
        self.assertEqual(result["extra"]["invocation_verification"], "success")
        self.assertEqual(result["extra"]["model_summary"]["successful_versions"], ["4.7"])

    def test_discovered_opus_is_not_valid_without_runtime_success(self):
        model_id = "us.anthropic.claude-opus-4-8-v1:0"
        for code in ("ServiceUnavailableException", "ValidationException"):
            with self.subTest(code=code):
                session = FakeSession({
                    "sts": StsClient(),
                    "bedrock": ProfileClient({None: profile_page(model_id)}),
                    "bedrock-runtime": RuntimeClient({"*": AwsError(code)}),
                })
                with (
                    patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
                    patch.object(bedrock, "_new_session", return_value=session),
                ):
                    result = self.run_async(bedrock.check(KEY))

                self.assertEqual(result["status"], "error")
                self.assertIsNotNone(result["error"])
                self.assertEqual(result["extra"]["availability_basis"], "runtime_failed")
                self.assertEqual(result["extra"]["invocation_verification"], "failed")
                self.assertEqual(result["extra"]["model_summary"]["supported_regions"], ["us-east-1"])
                self.assertEqual(result["extra"]["model_summary"]["successful_regions"], [])

    def test_operation_not_allowed_is_an_actionable_runtime_error(self):
        model_id = "us.anthropic.claude-opus-4-8-v1:0"
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(model_id)}),
            "bedrock-runtime": RuntimeClient({
                model_id: AwsError("ValidationException", "Operation not allowed"),
            }),
        })
        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            "Bedrock InvokeModel blocked (Operation not allowed)",
        )
        self.assertEqual(result["extra"]["runtime_restriction"], "operation_not_allowed")
        self.assertEqual(result["extra"]["model_summary"]["successful_regions"], [])

    def test_partial_region_discovery_without_runtime_success_is_error(self):
        east_model = "us.anthropic.claude-opus-4-8-v1:0"
        japan_model = "jp.anthropic.claude-opus-4-8-v1:0"
        sessions = {
            "us-east-1": FakeSession({
                "sts": StsClient(),
                "bedrock": ProfileClient({None: profile_page(east_model)}),
                "bedrock-runtime": RuntimeClient({"*": AwsError("ServiceUnavailableException")}),
            }),
            "ap-northeast-1": FakeSession({
                "bedrock": ProfileClient({None: profile_page(japan_model)}),
                "bedrock-runtime": RuntimeClient({"*": AwsError("ValidationException")}),
            }),
            "ap-east-1": FakeSession({
                "bedrock": ProfileClient(
                    {None: AwsError("UnrecognizedClientException")},
                    foundation_error=AwsError("UnrecognizedClientException"),
                ),
            }),
        }

        with (
            patch.object(bedrock, "configured_regions", return_value=tuple(sessions)),
            patch.object(bedrock, "_new_session", side_effect=self.factory_for(sessions, [])),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "error")
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["extra"]["availability_basis"], "runtime_failed")
        self.assertEqual(
            result["extra"]["model_summary"]["supported_regions"],
            ["us-east-1", "ap-northeast-1"],
        )
        self.assertTrue(any(
            failure["code"] == "UnrecognizedClientException"
            for failure in result["extra"]["partial_failures"]
        ))

    def test_region_profile_is_preferred_over_global_profile_for_same_version(self):
        global_model = "global.anthropic.claude-opus-4-8-v1:0"
        regional_model = "eu.anthropic.claude-opus-4-8-v1:0"
        runtime = RuntimeClient({
            global_model: AwsError("AccessDeniedException"),
            regional_model: {},
        })
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(global_model, regional_model)}),
            "bedrock-runtime": runtime,
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("eu-west-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "valid")
        self.assertEqual([call["modelId"] for call in runtime.calls], [regional_model])

    def test_non_credential_sts_errors_fall_through_to_bedrock(self):
        model_id = "us.anthropic.claude-opus-4-8-v1:0"
        for code in ("AccessDeniedException", "RegionDisabledException", "EndpointConnectionError"):
            with self.subTest(code=code):
                session = FakeSession({
                    "sts": StsClient(AwsError(code)),
                    "bedrock": ProfileClient({None: profile_page(model_id)}),
                    "bedrock-runtime": RuntimeClient({model_id: {}}),
                })
                with (
                    patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
                    patch.object(bedrock, "_new_session", return_value=session),
                ):
                    result = self.run_async(bedrock.check(KEY))

                self.assertEqual(result["status"], "valid")
                self.assertEqual(result["extra"]["credential_status"], "bedrock_verified")
                self.assertEqual(result["extra"]["regions_checked"], ["us-east-1"])
                self.assertTrue(any(
                    failure["stage"] == "sts" and failure["code"] == code
                    for failure in result["extra"]["partial_failures"]
                ))

    def test_bedrock_can_reject_credentials_when_sts_is_unavailable(self):
        session = FakeSession({
            "sts": StsClient(AwsError("RegionDisabledException")),
            "bedrock": ProfileClient({
                None: AwsError("InvalidSignatureException"),
            }, foundation_error=AwsError("InvalidSignatureException")),
        })
        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["extra"]["credential_status"], "invalid")

    def test_all_invoke_model_throttles_map_to_no_quota(self):
        sessions = {}
        for region in ("us-east-1", "us-west-2"):
            model_id = f"{region}.anthropic.claude-opus-4-8-v1:0"
            sessions[region] = FakeSession({
                "sts": StsClient(),
                "bedrock": ProfileClient({None: profile_page(model_id)}),
                "bedrock-runtime": RuntimeClient({"*": AwsError("ThrottlingException")}),
            })
        with (
            patch.object(bedrock, "configured_regions", return_value=tuple(sessions)),
            patch.object(bedrock, "_new_session", side_effect=self.factory_for(sessions, [])),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "no_quota")
        self.assertTrue(all(
            item["status"] == "throttled"
            for region in result["extra"]["region_results"].values()
            for item in region["invocations"]
        ))

    def test_all_profile_discovery_throttles_map_to_no_quota(self):
        east = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: AwsError("ThrottlingException")}),
        })
        west = FakeSession({
            "bedrock": ProfileClient({None: AwsError("TooManyRequestsException")}),
        })
        sessions = {"us-east-1": east, "us-west-2": west}
        with (
            patch.object(bedrock, "configured_regions", return_value=tuple(sessions)),
            patch.object(bedrock, "_new_session", side_effect=self.factory_for(sessions, [])),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "no_quota")

    def test_authorized_throttle_overrides_other_region_failures(self):
        throttled_model = "us.anthropic.claude-opus-4-8-v1:0"
        for non_throttle_failure in (
            AwsError("AccessDeniedException"),
            AwsError("InternalServerException"),
            TimeoutError("network timeout"),
        ):
            with self.subTest(failure=type(non_throttle_failure).__name__):
                east = FakeSession({
                    "sts": StsClient(),
                    "bedrock": ProfileClient({None: profile_page(throttled_model)}),
                    "bedrock-runtime": RuntimeClient({"*": AwsError("ThrottlingException")}),
                })
                west = FakeSession({
                    "bedrock": ProfileClient({None: non_throttle_failure}),
                })
                sessions = {"us-east-1": east, "us-west-2": west}
                with (
                    patch.object(bedrock, "configured_regions", return_value=tuple(sessions)),
                    patch.object(bedrock, "_new_session", side_effect=self.factory_for(sessions, [])),
                ):
                    result = self.run_async(bedrock.check(KEY))

                self.assertEqual(result["status"], "no_quota")
                self.assertEqual(result["extra"]["availability_basis"], "invoke_model_throttled")
                self.assertEqual(result["extra"]["invocation_verification"], "throttled")

    def test_deep_mixed_model_results_keep_proven_throttled_opus_access(self):
        denied = "us.anthropic.claude-opus-4-8-v1:0"
        throttled = "us.anthropic.claude-opus-4-5-v1:0"
        missing = "us.anthropic.claude-opus-4-1-v1:0"
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(denied, throttled, missing)}),
            "bedrock-runtime": RuntimeClient({
                denied: AwsError("AccessDeniedException"),
                throttled: AwsError("ThrottlingException"),
                missing: AwsError("ResourceNotFoundException"),
            }),
            "service-quotas": QuotaClient(error=AwsError("AccessDeniedException")),
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.deep_check(KEY))

        summary = result["extra"]["model_summary"]
        self.assertEqual(result["status"], "no_quota")
        self.assertEqual(result["extra"]["availability_basis"], "invoke_model_throttled")
        self.assertEqual(summary["throttled_versions"], ["4.5"])
        self.assertEqual(summary["throttled_regions"], ["us-east-1"])
        self.assertEqual(summary["throttled_models"], [throttled])
        self.assertEqual(summary["throttled_attempts"], 1)

    def test_permission_and_timeout_without_success_map_to_error(self):
        for failure in (AwsError("AccessDeniedException"), TimeoutError(SECRET_ACCESS_KEY)):
            with self.subTest(failure=type(failure).__name__):
                model_id = "us.anthropic.claude-opus-4-8-v1:0"
                session = FakeSession({
                    "sts": StsClient(),
                    "bedrock": ProfileClient({None: profile_page(model_id)}),
                    "bedrock-runtime": RuntimeClient({"*": failure}),
                })
                with (
                    patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
                    patch.object(bedrock, "_new_session", return_value=session),
                ):
                    result = self.run_async(bedrock.check(KEY))
                self.assertEqual(result["status"], "error")
                self.assertNotIn(SECRET_ACCESS_KEY, json.dumps(result))

    def test_deep_check_invokes_one_profile_per_version_and_keeps_quota_failures_partial(self):
        old45 = "us.anthropic.claude-opus-4-5-20250101-v1:0"
        new45 = "us.anthropic.claude-opus-4-5-20251212-v1:0"
        opus48 = "us.anthropic.claude-opus-4-8-20260701-v1:0"
        west48 = "eu.anthropic.claude-opus-4-8-20260701-v1:0"
        east_runtime = RuntimeClient({
            opus48: {"output": {"reply": SECRET_ACCESS_KEY}},
            new45: AwsError("AccessDeniedException"),
        })
        west_runtime = RuntimeClient({west48: TimeoutError(SECRET_ACCESS_KEY)})
        east = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(old45, new45, opus48)}),
            "bedrock-runtime": east_runtime,
            "service-quotas": QuotaClient(pages=[{"Quotas": [
                {"QuotaName": "Claude Opus 4.8 global requests per minute", "Value": Decimal("125")},
                {"QuotaName": "Claude Opus 4.8 cross-region input tokens per minute", "Value": 500000},
            ]}]),
        })
        west = FakeSession({
            "sts": StsClient(),
            "bedrock": ProfileClient({None: profile_page(west48)}),
            "bedrock-runtime": west_runtime,
            "service-quotas": QuotaClient(error=AwsError("AccessDeniedException")),
        })
        sessions = {"us-east-1": east, "eu-west-1": west}

        with (
            patch.object(bedrock, "configured_regions", return_value=tuple(sessions)),
            patch.object(bedrock, "_new_session", side_effect=self.factory_for(sessions, [])),
        ):
            result = self.run_async(bedrock.deep_check(KEY))

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["extra"]["check_mode"], "bedrock_deep")
        self.assertEqual(result["extra"]["regions_checked"], ["us-east-1", "eu-west-1"])
        self.assertEqual(
            result["extra"]["model_summary"]["supported_regions"],
            ["us-east-1", "eu-west-1"],
        )
        self.assertEqual(
            result["extra"]["model_summary"]["successful_models"],
            [opus48],
        )
        self.assertEqual(result["extra"]["model_summary"]["supported_model_count"], 4)
        self.assertEqual({call["modelId"] for call in east_runtime.calls}, {new45, opus48})
        self.assertNotIn(old45, {call["modelId"] for call in east_runtime.calls})
        self.assertEqual(result["extra"]["quotas"]["us-east-1"]["4.8"]["global"]["rpm"], 125)
        self.assertEqual(result["extra"]["quotas"]["us-east-1"]["4.8"]["xregion"]["tpm_in"], 500000)
        self.assertEqual(result["extra"]["region_results"]["eu-west-1"]["quota_status"], "error")
        self.assertTrue(any(item["stage"] == "service_quotas" for item in result["extra"]["partial_failures"]))
        serialized = json.dumps(result)
        self.assertNotIn(SECRET_ACCESS_KEY, serialized)
        self.assertNotIn(ACCESS_KEY_ID, serialized)
        self.assertNotIn("reply", serialized)

    def test_deep_check_discovers_only_opus_foundation_models(self):
        profile_id = "global.anthropic.claude-opus-4-8-v1:0"
        base_model_id = "anthropic.claude-3-opus-20240229-v1:0"
        bedrock_client = ProfileClient(
            {None: profile_page(profile_id)},
            foundation_models=[
                {
                    "modelId": base_model_id,
                    "modelName": "Claude 3 Opus",
                    "providerName": "Anthropic",
                },
                {
                    "modelId": "anthropic.claude-sonnet-4-20250514-v1:0",
                    "modelName": "Claude Sonnet 4",
                    "providerName": "Anthropic",
                },
            ],
        )
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": bedrock_client,
            "bedrock-runtime": RuntimeClient({profile_id: {}}),
            "service-quotas": QuotaClient(),
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.deep_check(KEY))

        summary = result["extra"]["model_summary"]
        self.assertEqual(result["status"], "valid")
        self.assertEqual(summary["foundation_models_found"], 1)
        self.assertEqual(summary["supported_models"], [base_model_id, profile_id])
        self.assertEqual(
            {call["modelId"] for call in session.services["bedrock-runtime"].calls},
            {base_model_id, profile_id},
        )
        self.assertNotIn("sonnet", json.dumps(result).lower())

    def test_deep_check_discovers_fable_5_as_a_foundation_model(self):
        fable = "anthropic.claude-fable-5"
        bedrock_client = ProfileClient(
            {None: profile_page()},
            foundation_models=[
                {
                    "modelId": fable,
                    "modelName": "Claude Fable 5",
                    "providerName": "Anthropic",
                },
                {
                    "modelId": "anthropic.claude-sonnet-5",
                    "modelName": "Claude Sonnet 5",
                    "providerName": "Anthropic",
                },
            ],
        )
        runtime = RuntimeClient({fable: {}})
        session = FakeSession({
            "sts": StsClient(),
            "bedrock": bedrock_client,
            "bedrock-runtime": runtime,
            "service-quotas": QuotaClient(pages=[{"Quotas": [{
                "QuotaName": "Claude Fable 5 global requests per minute",
                "Value": 20,
            }]}]),
        })

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
            patch.object(bedrock, "_new_session", return_value=session),
        ):
            result = self.run_async(bedrock.deep_check(KEY))

        summary = result["extra"]["model_summary"]
        self.assertEqual(result["status"], "valid")
        self.assertEqual(summary["supported_models"], [fable])
        self.assertEqual(summary["fable_5"]["status"], "supported")
        self.assertEqual([call["modelId"] for call in runtime.calls], [fable])
        self.assertEqual(result["extra"]["quotas"]["us-east-1"]["fable-5"]["global"]["rpm"], 20)
        self.assertNotIn("sonnet", json.dumps(result).lower())

    def test_deep_region_concurrency_never_exceeds_three(self):
        regions = (
            "us-east-1", "us-west-2", "eu-west-1",
            "eu-west-2", "ap-south-1", "ca-central-1",
        )
        active = 0
        peak = 0
        lock = threading.Lock()
        progress_events = []

        proxy = "socks5h://user:password@127.0.0.1:1080"

        def scan(access_key_id, secret_access_key, region, selected_proxy):
            nonlocal active, peak
            self.assertEqual(selected_proxy, proxy)
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return bedrock._empty_region_result(region), {}, []

        async def progress(label):
            progress_events.append(label)

        with (
            patch.object(bedrock, "configured_regions", return_value=regions),
            patch.object(bedrock, "_validate_identity_sync", return_value={"ok": True, "account": "1", "principal": "arn:test"}),
            patch.object(bedrock, "_scan_region_deep_sync", side_effect=scan),
        ):
            self.run_async(
                bedrock.deep_check(
                    KEY,
                    proxy=proxy,
                    progress_callback=progress,
                )
            )

        self.assertEqual(peak, 3)
        self.assertEqual(progress_events.count("STS 验证"), 1)
        self.assertEqual(set(progress_events) - {"STS 验证"}, set(regions))
        self.assertEqual(len(progress_events), len(regions) + 1)

    def test_quick_check_fans_out_with_bounded_region_concurrency(self):
        regions = tuple(f"test-region-{index}" for index in range(7))
        active = 0
        peak = 0
        lock = threading.Lock()
        progress_events = []

        def scan(access_key_id, secret_access_key, region, proxy):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return bedrock._empty_region_result(region), []

        async def progress(label):
            progress_events.append(label)

        with (
            patch.object(bedrock, "configured_regions", return_value=regions),
            patch.object(bedrock, "_validate_identity_sync", return_value={"ok": True, "account": "1", "principal": "arn:test"}),
            patch.object(bedrock, "_scan_region_quick_sync", side_effect=scan),
        ):
            result = self.run_async(
                bedrock.check(KEY, progress_callback=progress)
            )

        self.assertEqual(result["status"], "error")
        self.assertGreaterEqual(peak, 2)
        self.assertLessEqual(peak, bedrock.MAX_QUICK_REGION_CONCURRENCY)
        self.assertEqual(progress_events.count("STS 验证"), 1)
        self.assertEqual(set(progress_events) - {"STS 验证"}, set(regions))

    def test_quick_check_has_a_hard_global_deadline(self):
        def scan(access_key_id, secret_access_key, region, proxy):
            time.sleep(0.05)
            return bedrock._empty_region_result(region), []

        with (
            patch.object(bedrock, "configured_regions", return_value=("us-east-1", "us-west-2")),
            patch.object(bedrock, "QUICK_GLOBAL_TIMEOUT_SECONDS", 0.01),
            patch.object(bedrock, "_validate_identity_sync", return_value={"ok": True, "account": "1", "principal": "arn:test"}),
            patch.object(bedrock, "_scan_region_quick_sync", side_effect=scan),
        ):
            result = self.run_async(bedrock.check(KEY))

        self.assertEqual(result["status"], "error")
        self.assertTrue(any(
            failure["code"] == "QuickCheckTimeout"
            for failure in result["extra"]["partial_failures"]
        ))

    def test_client_attaches_socks_transport_without_exposing_proxy(self):
        proxy = "socks5h://user:password@127.0.0.1:1080"
        session = MagicMock()
        client = object()
        session.client.return_value = client

        with patch.object(bedrock, "_attach_socks_proxy") as attach_proxy:
            result = bedrock._client(session, "bedrock-runtime", proxy)

        self.assertIs(result, client)
        config = session.client.call_args.kwargs["config"]
        self.assertEqual(config.connect_timeout, 10)
        self.assertEqual(config.read_timeout, 30)
        attach_proxy.assert_called_once_with(client, proxy, config)

    def test_socks_transport_uses_remote_dns_proxy_manager(self):
        from urllib3.contrib.socks import SOCKSProxyManager

        proxy = "socks5h://127.0.0.1:1080"
        transport = bedrock._SocksURLLib3Session(
            proxies={"http": proxy, "https": proxy},
            timeout=(1, 2),
        )
        try:
            self.assertEqual(
                transport._proxy_config.proxy_url_for("https://sts.us-east-1.amazonaws.com"),
                proxy,
            )
            manager = transport._get_proxy_manager(proxy)
            self.assertIsInstance(manager, SOCKSProxyManager)
            self.assertTrue(manager.connection_pool_kw["_socks_options"]["rdns"])
        finally:
            transport.close()

    def test_environment_region_override_is_validated_and_deduplicated(self):
        with patch.dict(os.environ, {"BEDROCK_REGIONS": "US-EAST-1, eu-west-1;us-east-1 invalid"}):
            self.assertEqual(bedrock.configured_regions(), ("us-east-1", "eu-west-1"))

    def test_default_regions_cover_current_and_legacy_bedrock_endpoints(self):
        expected = {
            "us-east-1", "us-east-2", "us-west-1", "us-west-2",
            "ca-central-1", "ca-west-1", "sa-east-1", "mx-central-1",
            "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1",
            "eu-central-2", "eu-north-1", "eu-south-1", "eu-south-2",
            "me-south-1", "me-central-1", "af-south-1", "il-central-1",
            "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
            "ap-southeast-1", "ap-southeast-2", "ap-southeast-3",
            "ap-southeast-4", "ap-southeast-5", "ap-southeast-6",
            "ap-southeast-7", "ap-south-1", "ap-south-2", "ap-east-1",
            "ap-east-2",
        }

        self.assertEqual(set(bedrock.DEFAULT_REGIONS), expected)


if __name__ == "__main__":
    unittest.main()
