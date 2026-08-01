import unittest

from app import _parse_keys_text, _secret_export_lines


ENDPOINT = "resource-name.openai.azure.com"
API_KEY = "0123456789abcdef0123456789abcdef"
DEPLOYMENT_URL = (
    "https://resource-name.cognitiveservices.azure.com/openai/deployments/"
    "gpt-5.5/chat/completions?api-version=2025-04-01-preview"
)


class AzureOpenAIExportTests(unittest.TestCase):
    def test_text_copy_prefixes_endpoint_with_https(self):
        lines = _secret_export_lines({
            "provider": "azure_openai",
            "api_key": f"{ENDPOINT}|{API_KEY}",
        })

        self.assertEqual(
            lines,
            [f"https://{ENDPOINT}/openai/v1/chat/completions|{API_KEY}"],
        )

    def test_already_prefixed_credential_is_not_double_prefixed(self):
        lines = _secret_export_lines({
            "provider": "azure_openai",
            "api_key": f"https://{ENDPOINT}|{API_KEY}",
        })

        self.assertEqual(
            lines,
            [f"https://{ENDPOINT}/openai/v1/chat/completions|{API_KEY}"],
        )

    def test_services_ai_full_url_exports_canonically(self):
        endpoint = "resource-name.services.ai.azure.com"
        lines = _secret_export_lines({
            "provider": "azure_openai",
            "api_key": f"{endpoint}|{API_KEY}",
        })

        self.assertEqual(
            lines,
            [f"https://{endpoint}/openai/v1/chat/completions|{API_KEY}"],
        )

    def test_services_full_url_exports_canonically(self):
        endpoint = "resource-name.services.azure.com"
        lines = _secret_export_lines({
            "provider": "azure_openai",
            "api_key": f"{endpoint}|{API_KEY}",
        })

        self.assertEqual(
            lines,
            [f"https://{endpoint}/openai/v1/chat/completions|{API_KEY}"],
        )

    def test_cognitive_services_deployment_url_exports_without_rewriting(self):
        lines = _secret_export_lines({
            "provider": "azure_openai",
            "api_key": f"{DEPLOYMENT_URL}|{API_KEY}",
        })

        self.assertEqual(lines, [f"{DEPLOYMENT_URL}|{API_KEY}"])

    def test_cognitive_services_environment_variables_import_as_one_credential(self):
        long_key = "C" * 84
        keys, providers = _parse_keys_text(
            f"GPT_5_5_KEY={long_key}\n"
            f"GPT_5_5_ENDPOINT={DEPLOYMENT_URL}\n"
        )

        expected = f"{DEPLOYMENT_URL}|{long_key}"
        self.assertEqual(keys, [expected])
        self.assertEqual(providers, {expected: "azure_openai"})

    def test_paired_environment_variables_import_as_one_credential(self):
        endpoint = "resource-name.services.azure.com"
        long_key = "A" * 84
        keys, providers = _parse_keys_text(
            "GPT_5_6_SOL_KEY=" + long_key + "\n"
            "GPT_5_6_SOL_ENDPOINT=https://" + endpoint
            + "/openai/v1/chat/completions\n"
        )

        expected = f"{endpoint}|{long_key}"
        self.assertEqual(keys, [expected])
        self.assertEqual(providers, {expected: "azure_openai"})

    def test_quoted_environment_values_and_order_are_supported(self):
        endpoint = "resource-name.services.ai.azure.com"
        long_key = "B" * 84
        keys, providers = _parse_keys_text(
            f'MY_ENDPOINT="https://{endpoint}/openai/v1"\n'
            f"MY_KEY='{long_key}'\n"
        )

        expected = f"{endpoint}|{long_key}"
        self.assertEqual(keys, [expected])
        self.assertEqual(providers[expected], "azure_openai")


if __name__ == "__main__":
    unittest.main()
