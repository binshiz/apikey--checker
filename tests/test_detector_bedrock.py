import unittest

from detector import detect_provider, normalize_key, parse_bedrock_key, short_key


ACCESS_KEY_ID = "AKIAABCDEFGHIJKLMNOP"
SECRET_ACCESS_KEY = "a/+=" + "b" * 36


class BedrockDetectorTests(unittest.TestCase):
    def test_detects_and_normalizes_long_lived_aws_pair(self):
        pasted = f"  {ACCESS_KEY_ID}   |   {SECRET_ACCESS_KEY}  "

        self.assertEqual(normalize_key(pasted), f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}")
        self.assertEqual(parse_bedrock_key(pasted), (ACCESS_KEY_ID, SECRET_ACCESS_KEY))
        self.assertEqual(detect_provider(pasted), "aws_bedrock")

    def test_aws_secret_charset_supports_slash_plus_and_normalized_equals(self):
        fullwidth_secret = "a/+＝" + "b" * 36

        self.assertEqual(len(fullwidth_secret), 40)
        self.assertEqual(
            normalize_key(f"{ACCESS_KEY_ID}｜{fullwidth_secret}"),
            f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}",
        )
        self.assertEqual(detect_provider(f"{ACCESS_KEY_ID}|{fullwidth_secret}"), "aws_bedrock")

    def test_region_suffix_is_accepted_ignored_and_deduplicated(self):
        region_key = f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}|us-east-2"

        self.assertEqual(normalize_key(region_key), f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}")
        self.assertEqual(parse_bedrock_key(region_key), (ACCESS_KEY_ID, SECRET_ACCESS_KEY))
        self.assertEqual(detect_provider(region_key), "aws_bedrock")

    def test_rejects_temporary_malformed_and_extra_part_credentials(self):
        temporary = "ASIAABCDEFGHIJKLMNOP"

        self.assertIsNone(detect_provider(f"{temporary}|{SECRET_ACCESS_KEY}"))
        self.assertIsNone(detect_provider(f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY[:-1]}"))
        self.assertIsNone(detect_provider(f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}|not-a-region"))
        self.assertIsNone(detect_provider(f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}|us-east-1|extra"))
        self.assertIsNone(detect_provider(f"{ACCESS_KEY_ID.lower()}|{SECRET_ACCESS_KEY}"))

    def test_non_aws_normalization_only_strips_outer_whitespace(self):
        self.assertEqual(normalize_key("  arbitrary | value  "), "arbitrary | value")

    def test_short_key_never_reveals_any_aws_secret_characters(self):
        display = short_key(f" {ACCESS_KEY_ID} | {SECRET_ACCESS_KEY} ")

        self.assertEqual(display, "AKIAABCD…MNOP|••••••••")
        self.assertNotIn(SECRET_ACCESS_KEY, display)
        for marker in ("a", "/", "+", "="):
            self.assertNotIn(marker, display)

        # Unsupported temporary credentials are still masked if displayed.
        self.assertNotIn(SECRET_ACCESS_KEY, short_key(f"ASIAABCDEFGHIJKLMNOP|{SECRET_ACCESS_KEY}"))
        self.assertNotIn(SECRET_ACCESS_KEY, short_key(f"{ACCESS_KEY_ID.lower()}|{SECRET_ACCESS_KEY}"))
        self.assertNotIn(SECRET_ACCESS_KEY, short_key(f"{ACCESS_KEY_ID}|{SECRET_ACCESS_KEY}|us-east-1"))


if __name__ == "__main__":
    unittest.main()
