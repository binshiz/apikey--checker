import unittest

from detector import detect_provider, parse_openrouter_key, short_key


OPENROUTER_KEY = "sk-or-v1-" + "a1" * 32


class OpenRouterDetectorTests(unittest.TestCase):
    def test_documented_key_format_is_detected(self):
        self.assertEqual(parse_openrouter_key(OPENROUTER_KEY), OPENROUTER_KEY)
        self.assertEqual(detect_provider(OPENROUTER_KEY), "openrouter")
        self.assertEqual(
            detect_provider(f"  {OPENROUTER_KEY}\n"),
            "openrouter",
        )

    def test_truncated_extended_and_non_hex_keys_are_rejected(self):
        for candidate in (
            OPENROUTER_KEY[:-1],
            OPENROUTER_KEY + "0",
            OPENROUTER_KEY[:-1] + "g",
            OPENROUTER_KEY.upper(),
            "sk-or-v2-" + OPENROUTER_KEY.removeprefix("sk-or-v1-"),
        ):
            with self.subTest(candidate=candidate[:20]):
                self.assertIsNone(parse_openrouter_key(candidate))
                self.assertIsNone(detect_provider(candidate))

    def test_nearby_openai_key_is_not_misclassified(self):
        openai_key = "sk-" + "A" * 40
        self.assertEqual(detect_provider(openai_key), "openai")
        self.assertNotEqual(detect_provider(openai_key), "openrouter")

    def test_short_key_preserves_provider_and_masks_secret(self):
        displayed = short_key(OPENROUTER_KEY)
        self.assertEqual(displayed, f"sk-or-v1-…{OPENROUTER_KEY[-6:]}")
        self.assertNotIn(OPENROUTER_KEY, displayed)


if __name__ == "__main__":
    unittest.main()
