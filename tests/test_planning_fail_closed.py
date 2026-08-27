from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yt_auto.pipeline import ShortsFactory
from yt_auto.research import SourceSafeResearchExhaustedError


class PlanningFailClosedTests(unittest.TestCase):
    @staticmethod
    def _factory(plan_side_effect: Exception) -> ShortsFactory:
        factory = ShortsFactory.__new__(ShortsFactory)
        factory.config = SimpleNamespace(
            app=SimpleNamespace(
                feedback_enabled=False,
                candidate_pool_size=4,
                state_dir=Path("state"),
            )
        )
        factory.logger = Mock()
        factory.feedback = Mock()
        factory.script_writer = Mock()
        factory.topic_planner = SimpleNamespace(
            plan=Mock(side_effect=plan_side_effect),
        )
        factory._channel = Mock(
            return_value=SimpleNamespace(
                id="ancient_history",
                styles=["documentary"],
            )
        )
        factory._content_profile = Mock(return_value=SimpleNamespace())
        factory._recent_titles = Mock(return_value=set())
        return factory

    @patch("yt_auto.channel_analyzer.ChannelAnalyzer.load", return_value=None)
    def test_source_safe_research_exhaustion_retries_then_uses_normal_failure(
        self,
        _load_dna: Mock,
    ) -> None:
        factory = self._factory(
            SourceSafeResearchExhaustedError(
                "No source-safe Ancient History Short has enough concrete facts"
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Failed to generate a high-quality topic after multiple attempts",
        ):
            factory.build_one("ancient_history", content_kind="short")

        self.assertEqual(factory.topic_planner.plan.call_count, 12)
        self.assertEqual(factory.logger.warning.call_count, 12)
        self.assertIn(
            "Research exhausted for planning candidate 12",
            factory.logger.warning.call_args.args[1],
        )

    @patch("yt_auto.channel_analyzer.ChannelAnalyzer.load", return_value=None)
    def test_unrelated_runtime_error_is_not_swallowed(self, _load_dna: Mock) -> None:
        factory = self._factory(RuntimeError("unexpected planner bug"))

        with self.assertRaisesRegex(RuntimeError, "unexpected planner bug"):
            factory.build_one("ancient_history", content_kind="short")

        factory.topic_planner.plan.assert_called_once()
        factory.logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
