import unittest

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


if __name__ == "__main__":
    unittest.main()
