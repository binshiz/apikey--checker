import asyncio
from decimal import Decimal
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from checkers import bedrock


ACCESS_KEY_ID = "AKIAABCDEFGHIJKLMNOP"
SECRET_ACCESS_KEY = "a/+=" + "b" * 36
KEY = f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}"


class AwsError(Exception):
    def __init__(self, code):
        super().__init__("message intentionally ignored")
        self.response = {"Error": {"Code": code, "Message": "not persisted"}}


class StsClient:
    def __init__(self, error=None):
        self.error = error

    def get_caller_identity(self):
        if self.error:
            raise self.error
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/test"}


class ProfileClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_inference_profiles(self, **kwargs):
        self.calls.append(kwargs)
        value = self.pages.get(kwargs.get("nextToken"))
        if isinstance(value, BaseException):
            raise value
        return value or {"inferenceProfileSummaries": []}


class RuntimeClient:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def converse(self, **kwargs):
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
        ):
            result = self.run_async(bedrock.check(KEY, proxy="socks5://user:password@example"))

        self.assertEqual(result["status"], "valid")
        self.assertIsNone(result["tier"])
        self.assertIsNone(result["rpm"])
        self.assertIsNone(result["tpm"])
        self.assertEqual(result["extra"]["regions_checked"], ["us-east-1"])
        self.assertEqual(result["extra"]["model_summary"]["successful_versions"], ["4.8"])
        self.assertTrue(result["extra"]["proxy_ignored"])
        self.assertEqual([call.get("nextToken") for call in profiles.calls], [None, "page-2"])
        self.assertEqual([call["modelId"] for call in runtime.calls], [opus48])
        self.assertNotIn("service-quotas", east.requested_services)
        self.assertNotIn("us-west-2", session_calls)
        serialized = json.dumps(result)
        self.assertNotIn(SECRET_ACCESS_KEY, serialized)
        self.assertNotIn(ACCESS_KEY_ID, serialized)
        self.assertNotIn("reply", serialized)
        self.assertNotIn("socks5://", serialized)

    def test_sts_credential_error_is_invalid_but_permission_error_is_not(self):
        for code, expected_status in (
            ("InvalidClientTokenId", "invalid"),
            ("AccessDeniedException", "error"),
            ("RequestExpired", "error"),
        ):
            with self.subTest(code=code):
                session = FakeSession({"sts": StsClient(AwsError(code))})
                with (
                    patch.object(bedrock, "configured_regions", return_value=("us-east-1",)),
                    patch.object(bedrock, "_new_session", return_value=session),
                ):
                    result = self.run_async(bedrock.check(KEY))
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["extra"]["regions_checked"], [])

    def test_all_converse_throttles_map_to_no_quota(self):
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

    def test_throttle_mixed_with_non_throttle_failure_maps_to_error(self):
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

                self.assertEqual(result["status"], "error")
                self.assertNotEqual(result["error"], "all Bedrock checks were throttled")

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

    def test_deep_region_concurrency_never_exceeds_three(self):
        regions = (
            "us-east-1", "us-west-2", "eu-west-1",
            "eu-west-2", "ap-south-1", "ca-central-1",
        )
        active = 0
        peak = 0
        lock = threading.Lock()

        def scan(access_key_id, secret_access_key, region):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return bedrock._empty_region_result(region), {}, []

        with (
            patch.object(bedrock, "configured_regions", return_value=regions),
            patch.object(bedrock, "_validate_identity_sync", return_value={"ok": True, "account": "1", "principal": "arn:test"}),
            patch.object(bedrock, "_scan_region_deep_sync", side_effect=scan),
        ):
            self.run_async(bedrock.deep_check(KEY))

        self.assertEqual(peak, 3)

    def test_environment_region_override_is_validated_and_deduplicated(self):
        with patch.dict(os.environ, {"BEDROCK_REGIONS": "US-EAST-1, eu-west-1;us-east-1 invalid"}):
            self.assertEqual(bedrock.configured_regions(), ("us-east-1", "eu-west-1"))


if __name__ == "__main__":
    unittest.main()
