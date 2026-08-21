# -*- coding: utf-8 -*-
"""Tests for symmetric scoring metrics, Brier score, and reliability table.

覆盖：
- BacktestEngine.compute_calibration 的手算期望（Brier + 可靠性分桶）；
- compute_summary 新增的 strict_accuracy_pct / direction_breakdown / calibration；
- BacktestService 的概率提取（p_up 优先、confidence_level 回退、空仓立场取反）；
- DecisionSignalOutcomeService 的信号置信度校准；
- 引擎内置置信度映射与 decision_signal_extractor 保持同步。
"""

import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

from src.core.backtest_engine import (
    CONFIDENCE_LEVEL_PROBABILITY,
    SCORING_SCHEME,
    BacktestEngine,
    confidence_level_to_probability,
)
from src.services.backtest_service import BacktestService
from src.services.decision_signal_outcome_service import DecisionSignalOutcomeService


@dataclass
class FakeRow:
    analysis_history_id: Optional[int] = None
    eval_status: str = "completed"
    position_recommendation: str = "long"
    outcome: Optional[str] = "win"
    direction_correct: Optional[bool] = True
    direction_expected: Optional[str] = "up"
    stock_return_pct: Optional[float] = 1.0
    simulated_return_pct: Optional[float] = 1.0
    hit_stop_loss: Optional[bool] = False
    hit_take_profit: Optional[bool] = False
    first_hit: Optional[str] = "neither"
    first_hit_trading_days: Optional[int] = None
    operation_advice: Optional[str] = "买入"


class ComputeCalibrationTestCase(unittest.TestCase):
    def test_brier_and_reliability_hand_computed(self) -> None:
        # 手算: (0.8-1)^2 + (0.8-0)^2 + (0.4-0)^2 = 0.04 + 0.64 + 0.16 = 0.84; /3 = 0.28
        # 严格口径: neutral (direction_correct=None) 记 0
        calibration = BacktestEngine.compute_calibration(
            [(0.8, True), (0.8, False), (0.4, None)]
        )

        self.assertEqual(calibration["sample_count"], 3)
        self.assertAlmostEqual(calibration["brier_score"], 0.28)

        table = calibration["reliability_table"]
        self.assertEqual(len(table), 2)

        low_bucket = table[0]
        self.assertEqual(low_bucket["p_low"], 0.4)
        self.assertEqual(low_bucket["p_high"], 0.5)
        self.assertEqual(low_bucket["count"], 1)
        self.assertAlmostEqual(low_bucket["avg_predicted"], 0.4)
        self.assertEqual(low_bucket["hit_count"], 0)
        self.assertAlmostEqual(low_bucket["realized_hit_rate_pct"], 0.0)

        high_bucket = table[1]
        self.assertEqual(high_bucket["p_low"], 0.8)
        self.assertEqual(high_bucket["p_high"], 0.9)
        self.assertEqual(high_bucket["count"], 2)
        self.assertAlmostEqual(high_bucket["avg_predicted"], 0.8)
        self.assertEqual(high_bucket["hit_count"], 1)
        self.assertAlmostEqual(high_bucket["realized_hit_rate_pct"], 50.0)

    def test_supports_fine_grained_numeric_probabilities(self) -> None:
        # 未来数值 p_up：0.55 与 0.62 落入不同的 0.1 宽度桶
        calibration = BacktestEngine.compute_calibration(
            [(0.55, True), (0.52, False), (0.62, True), (1.0, True)]
        )
        table = calibration["reliability_table"]
        self.assertEqual([bucket["p_low"] for bucket in table], [0.5, 0.6, 0.9])
        self.assertEqual(table[0]["count"], 2)
        self.assertAlmostEqual(table[0]["avg_predicted"], 0.535)
        # p=1.0 收敛到最后一个桶 [0.9, 1.0]
        self.assertEqual(table[2]["count"], 1)

    def test_invalid_probabilities_are_skipped(self) -> None:
        calibration = BacktestEngine.compute_calibration(
            [(None, True), (1.5, True), (-0.1, False), (float("nan"), True), ("bad", True)]
        )
        self.assertEqual(calibration["sample_count"], 0)
        self.assertIsNone(calibration["brier_score"])
        self.assertEqual(calibration["reliability_table"], [])


class ComputeSummaryStrictMetricsTestCase(unittest.TestCase):
    def _summary(self, rows, probabilities=None):
        return BacktestEngine.compute_summary(
            results=rows,
            scope="overall",
            code=None,
            eval_window_days=3,
            engine_version="v1",
            probabilities=probabilities,
        )

    def test_strict_accuracy_counts_neutral_in_denominator(self) -> None:
        rows = [
            FakeRow(analysis_history_id=1, outcome="win", direction_correct=True),
            FakeRow(analysis_history_id=2, outcome="win", direction_correct=True),
            FakeRow(analysis_history_id=3, outcome="loss", direction_correct=False),
            FakeRow(
                analysis_history_id=4,
                outcome="neutral",
                direction_correct=None,
                direction_expected="not_down",
                operation_advice="持有",
            ),
            FakeRow(
                analysis_history_id=5,
                outcome="neutral",
                direction_correct=None,
                direction_expected="flat",
                operation_advice="观望",
            ),
        ]
        summary = self._summary(rows)

        # 严格口径: 2 win / 5 scored = 40%
        self.assertEqual(summary["scored_count"], 5)
        self.assertAlmostEqual(summary["strict_accuracy_pct"], 40.0)
        # legacy 口径保持不变: 2 / (2+1) = 66.67%
        self.assertAlmostEqual(summary["win_rate_pct"], 66.67)
        self.assertAlmostEqual(summary["direction_accuracy_pct"], 66.67)
        self.assertEqual(summary["scoring_scheme"], SCORING_SCHEME)

    def test_direction_breakdown_makes_hedging_visible(self) -> None:
        rows = [
            FakeRow(analysis_history_id=1, outcome="win", direction_correct=True),
            FakeRow(analysis_history_id=2, outcome="loss", direction_correct=False),
            FakeRow(
                analysis_history_id=3,
                outcome="neutral",
                direction_correct=None,
                direction_expected="not_down",
                operation_advice="持有",
            ),
        ]
        breakdown = self._summary(rows)["direction_breakdown"]

        self.assertEqual(breakdown["up"]["total"], 2)
        self.assertEqual(breakdown["up"]["win"], 1)
        self.assertEqual(breakdown["up"]["loss"], 1)
        self.assertAlmostEqual(breakdown["up"]["win_rate_pct"], 50.0)
        self.assertAlmostEqual(breakdown["up"]["strict_accuracy_pct"], 50.0)

        self.assertEqual(breakdown["not_down"]["total"], 1)
        self.assertEqual(breakdown["not_down"]["neutral"], 1)
        self.assertIsNone(breakdown["not_down"]["win_rate_pct"])
        self.assertAlmostEqual(breakdown["not_down"]["strict_accuracy_pct"], 0.0)

    def test_calibration_uses_probabilities_keyed_by_analysis_history_id(self) -> None:
        rows = [
            FakeRow(analysis_history_id=1, outcome="win", direction_correct=True),
            FakeRow(analysis_history_id=2, outcome="loss", direction_correct=False),
            FakeRow(
                analysis_history_id=3,
                outcome="neutral",
                direction_correct=None,
                direction_expected="not_down",
            ),
            # 无概率映射的行不计入校准样本
            FakeRow(analysis_history_id=4, outcome="win", direction_correct=True),
        ]
        summary = self._summary(rows, probabilities={1: 0.8, 2: 0.8, 3: 0.4})

        calibration = summary["calibration"]
        self.assertEqual(calibration["sample_count"], 3)
        self.assertAlmostEqual(calibration["brier_score"], 0.28)

    def test_calibration_absent_without_probabilities(self) -> None:
        summary = self._summary([FakeRow(analysis_history_id=1)])
        self.assertIsNone(summary["calibration"])


class CallProbabilityExtractionTestCase(unittest.TestCase):
    def test_p_up_preferred_over_confidence_level(self) -> None:
        raw = json.dumps({"p_up": 0.66, "confidence_level": "高"})
        self.assertEqual(BacktestService._extract_call_probability(raw), (0.66, "p_up"))

    def test_p_up_read_from_nested_metadata(self) -> None:
        raw = json.dumps({"metadata": {"p_up": 0.7}})
        self.assertEqual(BacktestService._extract_call_probability(raw), (0.7, "p_up"))

    def test_confidence_level_fallback(self) -> None:
        for level, expected in (("高", 0.8), ("medium", 0.6), ("低", 0.4)):
            raw = json.dumps({"confidence_level": level})
            self.assertEqual(
                BacktestService._extract_call_probability(raw),
                (expected, "confidence_level"),
            )

    def test_invalid_p_up_falls_back_to_confidence_level(self) -> None:
        raw = json.dumps({"p_up": 180, "confidence_level": "低"})
        self.assertEqual(
            BacktestService._extract_call_probability(raw),
            (0.4, "confidence_level"),
        )

    def test_percent_scale_p_up_is_normalized(self) -> None:
        # 分析端 EV 契约输出 0-100 整数百分比
        raw = json.dumps({"p_up": 55, "confidence_level": "中"})
        self.assertEqual(BacktestService._extract_call_probability(raw), (0.55, "p_up"))

    def test_missing_probability_returns_none(self) -> None:
        self.assertEqual(BacktestService._extract_call_probability(None), (None, None))
        self.assertEqual(BacktestService._extract_call_probability("{}"), (None, None))
        self.assertEqual(BacktestService._extract_call_probability("not json"), (None, None))

    def test_call_probabilities_inverts_p_up_for_cash_stance(self) -> None:
        rows = [
            FakeRow(analysis_history_id=1, direction_expected="up"),
            FakeRow(analysis_history_id=2, direction_expected="flat", operation_advice="观望"),
            FakeRow(analysis_history_id=3, direction_expected="up"),
        ]
        raw_results = {
            1: json.dumps({"p_up": 0.7}),
            2: json.dumps({"p_up": 0.7}),
            3: json.dumps({"confidence_level": "高"}),
        }

        class FakeSession:
            def execute(self, _query):
                return SimpleNamespace(
                    all=lambda: [(hid, raw) for hid, raw in raw_results.items()]
                )

        probabilities, source_counts = BacktestService._call_probabilities(FakeSession(), rows)
        # 多头立场用 p_up；空仓立场取 1 - p_up；confidence_level 直接映射
        self.assertAlmostEqual(probabilities[1], 0.7)
        self.assertAlmostEqual(probabilities[2], 0.3)
        self.assertAlmostEqual(probabilities[3], 0.8)
        self.assertEqual(source_counts, {"p_up": 2, "confidence_level": 1})


class SignalConfidenceCalibrationTestCase(unittest.TestCase):
    @staticmethod
    def _service() -> DecisionSignalOutcomeService:
        # 跳过 __init__ 以避免创建真实 DB 连接；被测方法不依赖仓储层
        return object.__new__(DecisionSignalOutcomeService)

    @staticmethod
    def _stats_row(
        *,
        eval_status="completed",
        outcome="hit",
        direction_correct=True,
        direction_expected="up",
        metadata=None,
    ):
        return SimpleNamespace(
            outcome=SimpleNamespace(
                eval_status=eval_status,
                outcome=outcome,
                direction_correct=direction_correct,
                direction_expected=direction_expected,
            ),
            decision_profile="balanced",
            metadata_json=json.dumps(metadata) if metadata is not None else None,
        )

    def test_signal_probability_prefers_metadata_p_up(self) -> None:
        service = self._service()
        self.assertEqual(
            service._signal_probability(
                json.dumps({"p_up": 0.75, "report_confidence_level": "低"}),
                direction_expected="up",
            ),
            (0.75, "p_up"),
        )
        p, source = service._signal_probability(
            json.dumps({"p_up": 0.75}),
            direction_expected="not_up",
        )
        self.assertAlmostEqual(p, 0.25)
        self.assertEqual(source, "p_up")

    def test_signal_probability_falls_back_to_report_confidence_level(self) -> None:
        service = self._service()
        self.assertEqual(
            service._signal_probability(
                json.dumps({"report_confidence_level": "中"}),
                direction_expected="up",
            ),
            (0.6, "report_confidence_level"),
        )
        self.assertEqual(
            service._signal_probability(None, direction_expected="up"),
            (None, None),
        )

    def test_confidence_calibration_aggregates_completed_rows(self) -> None:
        service = self._service()
        stats_rows = [
            self._stats_row(
                outcome="hit",
                direction_correct=True,
                metadata={"report_confidence_level": "高"},
            ),
            self._stats_row(
                outcome="miss",
                direction_correct=False,
                metadata={"report_confidence_level": "高"},
            ),
            self._stats_row(
                outcome="neutral",
                direction_correct=None,
                metadata={"report_confidence_level": "低"},
            ),
            # 无概率来源 → unscored
            self._stats_row(outcome="hit", direction_correct=True, metadata={}),
            # 未完成 → 完全跳过
            self._stats_row(eval_status="unable", outcome=None, direction_correct=None),
        ]

        calibration = service._confidence_calibration(stats_rows)

        # 手算同 ComputeCalibrationTestCase: brier = 0.28
        self.assertEqual(calibration["sample_count"], 3)
        self.assertAlmostEqual(calibration["brier_score"], 0.28)
        self.assertEqual(calibration["unscored_completed"], 1)
        self.assertEqual(
            calibration["probability_source_counts"],
            {"report_confidence_level": 3},
        )


class ConfidenceMapSyncTestCase(unittest.TestCase):
    def test_engine_map_matches_extractor_map(self) -> None:
        from src.services.decision_signal_extractor import _CONFIDENCE_MAP

        self.assertEqual(CONFIDENCE_LEVEL_PROBABILITY, _CONFIDENCE_MAP)
        self.assertEqual(confidence_level_to_probability("高"), 0.8)
        self.assertEqual(confidence_level_to_probability("HIGH "), 0.8)
        self.assertIsNone(confidence_level_to_probability("unknown"))
        self.assertIsNone(confidence_level_to_probability(None))


if __name__ == "__main__":
    unittest.main()
