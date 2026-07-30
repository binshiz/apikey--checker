import unittest

from detector import detect_provider, normalize_key, parse_azure_openai_key, short_key


ENDPOINT = "resource-name.openai.azure.com"
SERVICES_ENDPOINT = "resource-name.services.ai.azure.com"
API_KEY = "0123456789abcdef0123456789abcdef"
CREDENTIAL = f"{ENDPOINT}|{API_KEY}"
LONG_API_KEY = "A" * 84


class AzureOpenAIDetectorTests(unittest.TestCase):
    def test_detects_and_normalizes_endpoint_key_pair(self):
        pasted = f"  HTTPS://RESOURCE-NAME.OPENAI.AZURE.COM ｜ {API_KEY.upper()}  "

        self.assertEqual(normalize_key(pasted), f"{ENDPOINT}|{API_KEY.upper()}")
        self.assertEqual(
            parse_azure_openai_key(pasted),
            (ENDPOINT, API_KEY.upper()),
        )
        self.assertEqual(detect_provider(pasted), "azure_openai")

    def test_accepts_newer_long_opaque_resource_key(self):
        credential = (
            f"https://{SERVICES_ENDPOINT}/openai/v1/chat/completions|{LONG_API_KEY}"
        )

        self.assertEqual(
            parse_azure_openai_key(credential),
            (SERVICES_ENDPOINT, LONG_API_KEY),
        )
        self.assertEqual(detect_provider(credential), "azure_openai")
        self.assertEqual(
            normalize_key(credential),
            f"{SERVICES_ENDPOINT}|{LONG_API_KEY}",
        )

    def test_accepts_v1_base_url_for_both_endpoint_families(self):
        for endpoint in (ENDPOINT, SERVICES_ENDPOINT):
            with self.subTest(endpoint=endpoint):
                credential = f"https://{endpoint}/openai/v1/|{API_KEY}"
                self.assertEqual(
                    parse_azure_openai_key(credential),
                    (endpoint, API_KEY),
                )

    def test_rejects_non_azure_hosts_and_unsafe_endpoint_forms(self):
        for endpoint in (
            "example.com",
            "openai.azure.com.evil.test",
            "http://resource-name.openai.azure.com",
            "resource-name.openai.azure.com/path",
            "https://resource-name.openai.azure.com/path",
            "resource-name.openai.azure.com:443",
            "https://resource-name.services.ai.azure.com/openai/v1/responses",
            "https://resource-name.services.ai.azure.com/openai/v1?x=1",
            "https://resource-name.services.ai.azure.com:443/openai/v1",
            "https://resource-name.services.ai.azure.com.evil.test/openai/v1",
            "-bad.openai.azure.com",
        ):
            with self.subTest(endpoint=endpoint):
                self.assertIsNone(parse_azure_openai_key(f"{endpoint}|{API_KEY}"))
                self.assertIsNone(detect_provider(f"{endpoint}|{API_KEY}"))

    def test_rejects_malformed_api_keys(self):
        for api_key in (
            API_KEY[:-1],
            "A" * 513,
            "contains whitespace " + "A" * 32,
            "x|" + API_KEY,
        ):
            with self.subTest(api_key=api_key):
                self.assertIsNone(parse_azure_openai_key(f"{ENDPOINT}|{api_key}"))
                self.assertIsNone(detect_provider(f"{ENDPOINT}|{api_key}"))

    def test_short_key_masks_the_entire_secret(self):
        display = short_key(
            f"https://{SERVICES_ENDPOINT}/openai/v1/chat/completions|{LONG_API_KEY}"
        )

        self.assertEqual(display, f"{SERVICES_ENDPOINT}|••••••••")
        self.assertNotIn(LONG_API_KEY, display)


if __name__ == "__main__":
    unittest.main()
