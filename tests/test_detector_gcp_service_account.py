import json
import unittest

from detector import (
    canonicalize_gcp_service_account,
    detect_provider,
    normalize_key,
    parse_gcp_service_account,
    short_key,
)


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
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://attacker.invalid/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/checker",
    }
    info.update(overrides)
    return info


class GCPServiceAccountDetectorTests(unittest.TestCase):
    def test_pretty_json_is_detected_and_canonicalized(self):
        info = service_account_info()
        pretty = json.dumps(info, indent=2)
        canonical = canonicalize_gcp_service_account(info)

        self.assertEqual(normalize_key(pretty), canonical)
        self.assertEqual(detect_provider(pretty), "gcp_service_account")
        self.assertEqual(parse_gcp_service_account(canonical), info)
        self.assertNotIn("\n", canonical.replace("\\n", ""))

    def test_field_order_and_whitespace_deduplicate(self):
        info = service_account_info()
        reversed_info = dict(reversed(list(info.items())))

        self.assertEqual(
            canonicalize_gcp_service_account(info),
            canonicalize_gcp_service_account(json.dumps(reversed_info, indent=4)),
        )

    def test_rejects_wrong_type_missing_fields_and_bad_pem(self):
        invalid = (
            service_account_info(type="authorized_user"),
            service_account_info(client_email="not-a-service-account@example.com"),
            service_account_info(private_key="not a PEM key"),
            {k: v for k, v in service_account_info().items() if k != "private_key_id"},
        )
        for value in invalid:
            with self.subTest(value=value.get("type")):
                raw = json.dumps(value)
                self.assertIsNone(parse_gcp_service_account(raw))
                self.assertIsNone(detect_provider(raw))
                with self.assertRaises(ValueError):
                    canonicalize_gcp_service_account(value)

    def test_short_key_contains_metadata_but_never_private_key(self):
        canonical = canonicalize_gcp_service_account(service_account_info())
        display = short_key(canonical)

        self.assertEqual(
            display,
            "fixture-project|checker@fixture-project.iam.gserviceaccount.com|…deadbeef",
        )
        self.assertNotIn(PRIVATE_KEY, display)
        self.assertNotIn("ZmFrZS", display)

    def test_accepts_google_managed_service_account_email_domains(self):
        for email in (
            "123456789-compute@developer.gserviceaccount.com",
            "fixture-project@appspot.gserviceaccount.com",
        ):
            with self.subTest(email=email):
                info = service_account_info(client_email=email)
                self.assertEqual(
                    detect_provider(canonicalize_gcp_service_account(info)),
                    "gcp_service_account",
                )


if __name__ == "__main__":
    unittest.main()
