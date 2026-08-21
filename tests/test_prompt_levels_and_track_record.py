# -*- coding: utf-8 -*-
"""
系统计算参考位 / 历史战绩 / 财报日历注入的单元测试（annotate-not-veto 口径）。

覆盖：
- _enhance_context 注入 computed_trade_levels 与 current_price
- _format_prompt：数据齐备时出现"系统计算参考位"块，数据不足时省略
- _format_prompt：track_record 存在时出现"历史战绩"块，缺失时省略
- _format_prompt：earnings_calendar 注入"下次财报"行与临近提示
- _build_track_record_context：无持久化汇总时返回 None（首跑跳过）
- _annotate_earnings_risk：买入 + 7 天内财报 → 只标注不否决
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    from tests.litellm_stub import ensure_litellm_stub

    ensure_litellm_stub()

from src.analyzer import AnalysisResult, GeminiAnalyzer


def _make_analyzer() -> GeminiAnalyzer:
    with patch.object(GeminiAnalyzer, "_init_litellm", return_value=None):
        return GeminiAnalyzer()


def _base_context(**overrides):
    context = {
        "code": "600519",
        "stock_name": "贵州茅台",
        "date": "2026-08-20",
        "today": {"close": 100.0, "ma5": 99.0, "ma10": 98.0, "ma20": 97.0},
        "news_window_days": 3,
    }
    context.update(overrides)
    return context


class ComputedLevelsPromptTestCase(unittest.TestCase):
    def test_levels_block_present_when_data_available(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(
            computed_trade_levels={
                "entry": 100.0,
                "stop": 96.7,
                "target": 106.6,
                "r_multiple": 2.0,
                "quality": "good",
                "atr": 2.2,
                "notes": ["ATR止损 = entry - 1.5*ATR(2.20)"],
            }
        )

        prompt = analyzer._format_prompt(context, "贵州茅台", news_context=None)

        self.assertIn("系统计算参考位（ATR基准）", prompt)
        self.assertIn("96.7", prompt)
        self.assertIn("106.6", prompt)
        self.assertIn("为锚定给出狙击点位", prompt)
        self.assertIn("必须按你最终给出的价位重新计算", prompt)

    def test_levels_block_absent_when_insufficient_data(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(
            computed_trade_levels={
                "entry": 100.0,
                "stop": None,
                "target": None,
                "r_multiple": None,
                "quality": "insufficient_data",
                "atr": None,
                "notes": ["缺少 ATR 与结构低点，无法计算止损"],
            }
        )

        prompt = analyzer._format_prompt(context, "贵州茅台", news_context=None)

        self.assertNotIn("系统计算参考位", prompt)

    def test_levels_block_absent_without_context_key(self) -> None:
        analyzer = _make_analyzer()
        prompt = analyzer._format_prompt(_base_context(), "贵州茅台", news_context=None)
        self.assertNotIn("系统计算参考位", prompt)

    def test_levels_block_absent_when_invalid_quality(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(
            computed_trade_levels={
                "entry": 100.0,
                "stop": 96.0,
                "target": None,
                "r_multiple": None,
                "quality": "invalid",
                "atr": 2.0,
                "notes": ["目标价不高于入场价"],
            }
        )
        prompt = analyzer._format_prompt(context, "贵州茅台", news_context=None)
        self.assertNotIn("系统计算参考位", prompt)


class TrackRecordPromptTestCase(unittest.TestCase):
    def test_track_record_block_present_with_summary_data(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(
            track_record={
                "overall": {
                    "strict_accuracy_pct": 46.15,
                    "scored_count": 65,
                    "brier_score": 0.2712,
                    "advice_breakdown": {
                        "买入": {"strict_accuracy_pct": 52.0, "total": 25},
                        "持有": {"strict_accuracy_pct": 40.0, "total": 30},
                    },
                },
                "stock": {
                    "code": "600519",
                    "strict_accuracy_pct": 60.0,
                    "scored_count": 5,
                },
                "calibration_hint": "你历史上标注p_up≈70%的交易实际命中率为48%（样本21笔）— 请据此校准本次 p_up",
            }
        )

        prompt = analyzer._format_prompt(context, "贵州茅台", news_context=None)

        self.assertIn("历史战绩", prompt)
        self.assertIn("整体严格准确率：46.15%", prompt)
        self.assertIn("Brier 分数：0.2712", prompt)
        self.assertIn("买入 52.0%（25笔）", prompt)
        self.assertIn("本股历史严格准确率：60.0%", prompt)
        self.assertIn("实际命中率为48%", prompt)
        # annotate-not-veto：明确不因战绩改方向
        self.assertIn("不要因历史战绩直接改变买卖方向", prompt)

    def test_track_record_block_skipped_without_data(self) -> None:
        analyzer = _make_analyzer()
        prompt = analyzer._format_prompt(_base_context(), "贵州茅台", news_context=None)
        self.assertNotIn("历史战绩", prompt)

    def test_track_record_block_skipped_when_empty_dict(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(track_record={})
        prompt = analyzer._format_prompt(context, "贵州茅台", news_context=None)
        self.assertNotIn("历史战绩", prompt)


class EarningsCalendarPromptTestCase(unittest.TestCase):
    def test_next_earnings_row_injected(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(
            code="AAPL",
            stock_name="Apple",
            earnings_calendar={"next_earnings_date": "2026-09-10", "days_until": 21},
        )
        prompt = analyzer._format_prompt(context, "Apple", news_context=None)
        self.assertIn("下次财报 | 2026-09-10 (21天后)", prompt)
        self.assertNotIn("财报临近", prompt)

    def test_earnings_proximity_note_when_within_seven_days(self) -> None:
        analyzer = _make_analyzer()
        context = _base_context(
            code="AAPL",
            stock_name="Apple",
            earnings_calendar={"next_earnings_date": "2026-08-24", "days_until": 4},
        )
        prompt = analyzer._format_prompt(context, "Apple", news_context=None)
        self.assertIn("下次财报 | 2026-08-24 (4天后)", prompt)
        self.assertIn("财报临近（4天后）", prompt)

    def test_no_earnings_row_without_context(self) -> None:
        analyzer = _make_analyzer()
        prompt = analyzer._format_prompt(_base_context(), "贵州茅台", news_context=None)
        self.assertNotIn("下次财报", prompt)


class PipelineInjectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._db_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "test_prompt_injection.db"
        )
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with patch.dict(os.environ, {"DATABASE_PATH": self._db_path}):
            from src.config import Config

            Config._instance = None
            self.config = Config._load_from_env()
        from src.core.pipeline import StockAnalysisPipeline

        self.pipeline = StockAnalysisPipeline(config=self.config)

    def _trend_result(self):
        from src.stock_analyzer import TrendAnalysisResult, TrendStatus

        return TrendAnalysisResult(
            code="600519",
            trend_status=TrendStatus.BULL,
            ma5=99.0,
            ma10=98.0,
            ma20=97.0,
            atr_14=2.0,
            swing_low_20=95.0,
            swing_high_20=108.0,
        )

    def test_enhance_context_injects_computed_levels_and_current_price(self) -> None:
        from data_provider.realtime_types import UnifiedRealtimeQuote, RealtimeSource

        quote = UnifiedRealtimeQuote(
            code="600519",
            name="贵州茅台",
            source=RealtimeSource.TENCENT,
            price=100.0,
        )
        context = {
            "code": "600519",
            "date": "2026-08-20",
            "today": {"close": 99.5},
            "yesterday": {"close": 98.0},
        }
        enhanced = self.pipeline._enhance_context(
            context, quote, None, self._trend_result(), "贵州茅台"
        )

        self.assertEqual(enhanced["current_price"], 100.0)
        levels = enhanced["computed_trade_levels"]
        self.assertEqual(levels["entry"], 100.0)
        self.assertIsNotNone(levels["stop"])
        self.assertIsNotNone(levels["target"])
        self.assertIn(levels["quality"], ("good", "acceptable", "poor_risk_reward"))

    def test_enhance_context_uses_today_close_without_realtime(self) -> None:
        context = {
            "code": "600519",
            "date": "2026-08-20",
            "today": {"close": 99.5},
            "yesterday": {"close": 98.0},
        }
        enhanced = self.pipeline._enhance_context(
            context, None, None, self._trend_result(), "贵州茅台"
        )
        self.assertEqual(enhanced["current_price"], 99.5)
        self.assertIn("computed_trade_levels", enhanced)

    def test_enhance_context_skips_levels_without_trend_result(self) -> None:
        context = {
            "code": "600519",
            "date": "2026-08-20",
            "today": {"close": 99.5},
            "yesterday": {"close": 98.0},
        }
        enhanced = self.pipeline._enhance_context(context, None, None, None, "贵州茅台")
        self.assertNotIn("computed_trade_levels", enhanced)

    def test_build_track_record_context_skips_when_no_summaries(self) -> None:
        from src.services.backtest_service import BacktestService

        with patch.object(BacktestService, "get_summary", return_value=None):
            self.assertIsNone(self.pipeline._build_track_record_context("600519"))

    def test_build_track_record_context_reads_persisted_summaries(self) -> None:
        from src.services.backtest_service import BacktestService

        overall = {
            "strict_accuracy_pct": 46.15,
            "scored_count": 65,
            "completed_count": 70,
            "calibration": {
                "brier_score": 0.2712,
                "sample_count": 60,
                "reliability_table": [
                    {
                        "p_low": 0.7,
                        "p_high": 0.8,
                        "count": 21,
                        "avg_predicted": 0.7,
                        "hit_count": 10,
                        "realized_hit_rate_pct": 47.62,
                    }
                ],
            },
            "advice_breakdown": {
                "买入": {"total": 25, "win": 13, "loss": 12, "neutral": 0, "strict_accuracy_pct": 52.0},
            },
        }

        def fake_get_summary(*, scope, code, **kwargs):
            if scope == "overall":
                return overall
            return None

        with patch.object(BacktestService, "get_summary", side_effect=fake_get_summary):
            track = self.pipeline._build_track_record_context("600519")

        self.assertIsNotNone(track)
        self.assertEqual(track["overall"]["strict_accuracy_pct"], 46.15)
        self.assertEqual(track["overall"]["brier_score"], 0.2712)
        self.assertIn("买入", track["overall"]["advice_breakdown"])
        self.assertIn("p_up≈70%", track["calibration_hint"])
        self.assertIn("47.62%", track["calibration_hint"])
        self.assertNotIn("stock", track)

    def _buy_result(self) -> AnalysisResult:
        return AnalysisResult(
            code="AAPL",
            name="Apple",
            sentiment_score=72,
            trend_prediction="看多",
            operation_advice="买入",
            action="buy",
            risk_warning="原有风险提示",
            dashboard={"intelligence": {"risk_alerts": ["既有风险点"]}},
        )

    def test_annotate_earnings_risk_flags_buy_within_horizon(self) -> None:
        result = self._buy_result()
        self.pipeline._annotate_earnings_risk(
            result, {"next_earnings_date": "2026-08-25", "days_until": 5}
        )

        self.assertIn("财报事件风险", result.risk_warning)
        self.assertIn("原有风险提示", result.risk_warning)
        self.assertIn("仓位", result.risk_warning)
        self.assertTrue(result.dashboard["earnings_within_horizon"])
        self.assertEqual(result.dashboard["next_earnings_date"], "2026-08-25")
        # annotate-not-veto：模型结论与评分保持不变
        self.assertEqual(result.action, "buy")
        self.assertEqual(result.operation_advice, "买入")
        self.assertEqual(result.sentiment_score, 72)
        # 风险警报列表被追加而非覆盖
        alerts = result.dashboard["intelligence"]["risk_alerts"]
        self.assertEqual(alerts[0], "既有风险点")
        self.assertTrue(any("财报事件风险" in alert for alert in alerts[1:]))

    def test_annotate_earnings_risk_skips_watch_calls(self) -> None:
        result = self._buy_result()
        result.action = "watch"
        result.operation_advice = "观望"
        self.pipeline._annotate_earnings_risk(
            result, {"next_earnings_date": "2026-08-25", "days_until": 5}
        )
        self.assertNotIn("财报事件风险", result.risk_warning)
        self.assertNotIn("earnings_within_horizon", result.dashboard)

    def test_annotate_earnings_risk_skips_far_earnings(self) -> None:
        result = self._buy_result()
        self.pipeline._annotate_earnings_risk(
            result, {"next_earnings_date": "2026-09-30", "days_until": 41}
        )
        self.assertNotIn("财报事件风险", result.risk_warning)
        self.assertNotIn("earnings_within_horizon", result.dashboard)

    def test_annotate_earnings_risk_handles_missing_context(self) -> None:
        result = self._buy_result()
        self.pipeline._annotate_earnings_risk(result, None)
        self.assertNotIn("财报事件风险", result.risk_warning)


if __name__ == "__main__":
    unittest.main()
