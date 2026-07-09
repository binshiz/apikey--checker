import unittest

from checkers.anthropic import build_supported_models


class AnthropicModelSummaryTests(unittest.TestCase):
    def test_display_targets_keep_requested_order(self):
        summary = build_supported_models([
            {"id": "claude-sonnet-4-6"},
            {"id": "claude-opus-4-7"},
            {"id": "claude-fable-5"},
            {"id": "claude-opus-4-8"},
        ])

        self.assertEqual(
            [(x["label"], x["model"], x["supported"]) for x in summary["display_targets"]],
            [
                ("fable-5", "claude-fable-5", True),
                ("opus-4-8", "claude-opus-4-8", True),
                ("opus-4-7", "claude-opus-4-7", True),
                ("sonnet-4-6", "claude-sonnet-4-6", True),
            ],
        )

    def test_claude_prefixed_model_matches_short_target(self):
        summary = build_supported_models([
            {"id": "claude-opus-4-8-20260701", "display_name": "Claude Opus 4.8"},
        ])

        opus = summary["display_targets"][1]
        self.assertEqual(opus["label"], "opus-4-8")
        self.assertEqual(opus["model"], "claude-opus-4-8-20260701")
        self.assertTrue(opus["supported"])
        self.assertEqual(opus["display_name"], "Claude Opus 4.8")

    def test_missing_targets_are_marked_unsupported(self):
        summary = build_supported_models([{"id": "claude-haiku-4-5"}])

        self.assertEqual(summary["all_count"], 1)
        self.assertEqual(summary["featured"], [])
        self.assertEqual(
            [(x["label"], x["model"], x["supported"]) for x in summary["display_targets"]],
            [
                ("fable-5", None, False),
                ("opus-4-8", None, False),
                ("opus-4-7", None, False),
                ("sonnet-4-6", None, False),
            ],
        )

    def test_empty_duplicate_and_unknown_models_are_safe(self):
        empty = build_supported_models([])
        self.assertEqual(empty["all_count"], 0)
        self.assertEqual(empty["models_preview"], [])

        deduped = build_supported_models([
            "claude-opus-4-8",
            {"id": "claude-opus-4-8"},
            "",
            {"id": "unknown-model"},
        ])
        self.assertEqual(deduped["all_count"], 2)
        self.assertEqual(deduped["featured"], ["claude-opus-4-8"])
        self.assertEqual(deduped["models_preview"], ["claude-opus-4-8", "unknown-model"])


if __name__ == "__main__":
    unittest.main()
