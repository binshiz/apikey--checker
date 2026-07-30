import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests
from google.auth.exceptions import RefreshError, TransportError

from checkers import gcp_service_account
from detector import canonicalize_gcp_service_account


PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "ZmFrZS10ZXN0LXByaXZhdGUta2V5\n"
    "-----END PRIVATE KEY-----\n"
)


def service_account_info(**overrides):
    info = {
        "type": "service_account",
        "project_id": "fixture-project",
        "private_key_id": "0123456789abcdef0123456789abcdefdeadbeef",
        "private_key": PRIVATE_KEY,
        "client_email": "checker@fixture-project.iam.gserviceaccount.com",
        "client_id": "123456789012345678901",
        "token_uri": "https://attacker.invalid/token",
    }
    info.update(overrides)
    return info


def successful_online_result(*models):
    supported = list(models or ("gemini-3.6-flash",))
    return (
        "2026-07-24T12:00:00Z",
        {
            "vertex_location": "global",
            "vertex_probe_method": "generateContent",
            "model_probe_results": [
                {"model": model, "status": "callable", "http_status": 200}
                for model in supported
            ],
            "models_checked": len(supported),
            "supported_models": supported,
            "supported_model_count": len(supported),
            "model_invocation_verification": "success",
        },
    )


class GCPServiceAccountCheckerTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_only_non_secret_metadata(self):
        key = canonicalize_gcp_service_account(service_account_info())
        with patch(
            "checkers.gcp_service_account._refresh_credentials",
            return_value=successful_online_result(
                "gemini-3.6-flash",
                "gemini-3.5-flash-lite",
            ),
        ) as refresh:
            result = await gcp_service_account.check(
                key,
                proxy="socks5://proxy-user:proxy-password@example.test:1080",
            )

        self.assertEqual(result["status"], "valid")
        self.assertIsNone(result["tier"])
        self.assertEqual(result["extra"]["token_exchange"], "success")
        self.assertEqual(result["extra"]["private_key_id_suffix"], "deadbeef")
        self.assertTrue(result["extra"]["proxy_used"])
        self.assertEqual(
            result["extra"]["supported_models"],
            ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
        )
        self.assertEqual(
            result["extra"]["model_invocation_verification"],
            "success",
        )
        refresh.assert_called_once()
        self.assertEqual(
            refresh.call_args.args[1],
            "socks5://proxy-user:proxy-password@example.test:1080",
        )
        persisted = repr(result)
        self.assertNotIn(PRIVATE_KEY, persisted)
        self.assertNotIn("proxy-password", persisted)

    async def test_oauth_success_without_callable_model_stays_valid_but_unproven(self):
        key = canonicalize_gcp_service_account(service_account_info())
        with patch(
            "checkers.gcp_service_account._refresh_credentials",
            return_value=(
                "2026-07-24T12:00:00Z",
                {
                    "vertex_location": "global",
                    "vertex_probe_method": "generateContent",
                    "model_probe_results": [{
                        "model": "gemini-3.6-flash",
                        "status": "permission_denied",
                        "http_status": 403,
                    }],
                    "models_checked": 1,
                    "supported_models": [],
                    "supported_model_count": 0,
                    "model_invocation_verification": "none",
                },
            ),
        ):
            result = await gcp_service_account.check(key)

        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["extra"]["token_exchange"], "success")
        self.assertEqual(result["extra"]["supported_models"], [])
        self.assertEqual(
            result["extra"]["model_invocation_verification"],
            "none",
        )

    async def test_google_rejection_is_invalid_without_raw_error(self):
        key = canonicalize_gcp_service_account(service_account_info())
        with patch(
            "checkers.gcp_service_account._refresh_credentials",
            side_effect=RefreshError(f"invalid_grant {PRIVATE_KEY}"),
        ):
            result = await gcp_service_account.check(key)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["error"], "Google rejected the service account credential")
        self.assertNotIn(PRIVATE_KEY, repr(result))

    async def test_transport_failure_is_retryable_error(self):
        key = canonicalize_gcp_service_account(service_account_info())
        with patch(
            "checkers.gcp_service_account._refresh_credentials",
            side_effect=TransportError("upstream unavailable"),
        ):
            result = await gcp_service_account.check(key)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "Google OAuth token request failed")

    async def test_retryable_google_refresh_failure_is_error(self):
        key = canonicalize_gcp_service_account(service_account_info())
        with patch(
            "checkers.gcp_service_account._refresh_credentials",
            side_effect=RefreshError("server_error", retryable=True),
        ):
            result = await gcp_service_account.check(key)

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            "Google OAuth token service is temporarily unavailable",
        )

    async def test_invalid_document_never_reaches_google_auth(self):
        with patch("checkers.gcp_service_account._refresh_credentials") as refresh:
            result = await gcp_service_account.check('{"type":"authorized_user"}')

        self.assertEqual(result["status"], "invalid")
        refresh.assert_not_called()

    async def test_well_formed_but_non_rsa_private_key_is_invalid(self):
        key = canonicalize_gcp_service_account(service_account_info())

        result = await gcp_service_account.check(key)

        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["error"], "Invalid GCP service account private key")
        self.assertNotIn(PRIVATE_KEY, repr(result))

    def test_refresh_overrides_untrusted_token_uri_and_configures_proxy(self):
        info = service_account_info()
        credentials = MagicMock()
        credentials.token = "access-token-must-not-be-returned"
        credentials.expiry = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        session = MagicMock()
        responses = []
        for index, _model_id in enumerate(gcp_service_account.VERTEX_MODEL_IDS):
            response = MagicMock()
            response.status_code = 200 if index == 0 else 403
            response.text = f"response must not persist {PRIVATE_KEY}"
            responses.append(response)
        session.post.side_effect = responses
        with (
            patch(
                "checkers.gcp_service_account.service_account.Credentials.from_service_account_info",
                return_value=credentials,
            ) as from_info,
            patch(
                "checkers.gcp_service_account.requests.Session",
                return_value=session,
            ),
        ):
            expiry, model_probe = gcp_service_account._refresh_credentials(
                info,
                "socks5://127.0.0.1:1080",
            )

        passed_info = from_info.call_args.args[0]
        self.assertEqual(
            passed_info["token_uri"],
            gcp_service_account.GOOGLE_OAUTH_TOKEN_URI,
        )
        self.assertEqual(
            from_info.call_args.kwargs["scopes"],
            (gcp_service_account.GOOGLE_CLOUD_SCOPE,),
        )
        self.assertEqual(
            session.proxies.update.call_args.args[0],
            {
                "http": "socks5://127.0.0.1:1080",
                "https": "socks5://127.0.0.1:1080",
            },
        )
        self.assertEqual(expiry, "2026-07-24T12:00:00Z")
        self.assertEqual(model_probe["supported_models"], ["gemini-3.6-flash"])
        self.assertEqual(model_probe["model_invocation_verification"], "success")
        self.assertEqual(
            len(model_probe["model_probe_results"]),
            len(gcp_service_account.VERTEX_MODEL_IDS),
        )
        self.assertNotIn(credentials.token, repr(model_probe))
        self.assertNotIn(PRIVATE_KEY, repr(model_probe))
        for model_id, call in zip(
            gcp_service_account.VERTEX_MODEL_IDS,
            session.post.call_args_list,
        ):
            self.assertIn(
                f"/locations/global/publishers/google/models/{model_id}:generateContent",
                call.args[0],
            )
            self.assertEqual(
                call.kwargs["json"],
                gcp_service_account.VERTEX_PROBE_BODY,
            )
            self.assertEqual(
                call.kwargs["timeout"],
                gcp_service_account.VERTEX_REQUEST_TIMEOUT_SECONDS,
            )
            self.assertEqual(
                call.kwargs["headers"]["Authorization"],
                f"Bearer {credentials.token}",
            )

    def test_vertex_probe_classifies_http_outcomes_without_response_body(self):
        session = MagicMock()
        statuses = [200, 429, 403, 404, 400, 503]
        responses = []
        for http_status in statuses:
            response = MagicMock()
            response.status_code = http_status
            response.text = f"secret response {PRIVATE_KEY}"
            responses.append(response)
        session.post.side_effect = responses

        result = gcp_service_account._probe_vertex_models(
            session,
            "secret-access-token",
            "fixture-project",
        )

        self.assertEqual(
            [probe["status"] for probe in result["model_probe_results"]],
            [
                "callable",
                "rate_limited",
                "permission_denied",
                "not_found",
                "request_rejected",
                "upstream_error",
            ],
        )
        self.assertEqual(
            result["supported_models"],
            [gcp_service_account.VERTEX_MODEL_IDS[0]],
        )
        self.assertNotIn("secret-access-token", repr(result))
        self.assertNotIn(PRIVATE_KEY, repr(result))

    def test_vertex_probe_stops_after_transport_failure(self):
        session = MagicMock()
        session.post.side_effect = requests.Timeout("contains secret-access-token")

        result = gcp_service_account._probe_vertex_models(
            session,
            "secret-access-token",
            "fixture-project",
        )

        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(
            result["model_probe_results"],
            [{
                "model": gcp_service_account.VERTEX_MODEL_IDS[0],
                "status": "network_error",
            }],
        )
        self.assertEqual(result["model_invocation_verification"], "none")
        self.assertNotIn("secret-access-token", repr(result))


if __name__ == "__main__":
    unittest.main()
