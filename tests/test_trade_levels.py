# -*- coding: utf-8 -*-
"""Tests for ATR/swing indicators, computed trade levels, and sniper-point validation."""

import math
import unittest

import pandas as pd

from src.stock_analyzer import StockTrendAnalyzer
from src.utils.sniper_points import parse_sniper_value, validate_levels
from src.utils.trade_levels import TradeLevels, compute_trade_levels


# 小型 OHLC fixture（5 根K线，便于手算 TR/ATR）
ATR_FIXTURE = pd.DataFrame(
    {
        "high": [11.0, 12.0, 11.5, 13.0, 12.5],
        "low": [10.0, 10.5, 10.8, 11.5, 11.8],
        "close": [10.5, 11.5, 11.0, 12.5, 12.0],
    }
)


def _wilder_atr_by_hand(df: pd.DataFrame, period: int = 14) -> float:
    """逐bar手算 Wilder ATR：ATR_t = ATR_{t-1} + (TR_t - ATR_{t-1}) / period"""
    trs = []
    prev_close = None
    for _, row in df.iterrows():
        if prev_close is None:
            tr = row["high"] - row["low"]
        else:
            tr = max(
                row["high"] - row["low"],
                abs(row["high"] - prev_close),
                abs(row["low"] - prev_close),
            )
        trs.append(tr)
        prev_close = row["close"]
    atr = trs[0]
    for tr in trs[1:]:
        atr = atr + (tr - atr) / period
    return atr


class CalculateAtrTestCase(unittest.TestCase):
    def test_atr_matches_hand_computed_wilder_smoothing(self) -> None:
        analyzer = StockTrendAnalyzer()
        result = analyzer._calculate_atr(ATR_FIXTURE)

        expected = _wilder_atr_by_hand(ATR_FIXTURE, analyzer.ATR_PERIOD)
        self.assertAlmostEqual(float(result[f"ATR{analyzer.ATR_PERIOD}"].iloc[-1]), expected)

    def test_analyze_exposes_atr_and_swing_levels(self) -> None:
        analyzer = StockTrendAnalyzer()
        # 30 根上升K线，满足 analyze 的最小数据要求
        closes = [10 + i * 0.1 for i in range(30)]
        df = pd.DataFrame(
            {
                "date": pd.date_range("2025-01-01", periods=30, freq="D"),
                "open": closes,
                "high": [c * 1.02 for c in closes],
                "low": [c * 0.98 for c in closes],
                "close": closes,
                "volume": [1_000_000] * 30,
            }
        )

        result = analyzer.analyze(df, "600000")

        self.assertGreater(result.atr_14, 0)
        self.assertGreater(result.atr_pct, 0)
        self.assertAlmostEqual(result.swing_low_10, min(df["low"].iloc[-10:]))
        self.assertAlmostEqual(result.swing_low_20, min(df["low"].iloc[-20:]))
        self.assertAlmostEqual(result.swing_high_20, max(df["high"].iloc[-20:]))
        # 结构低点应补充进支撑位列表（追加在均线支撑之后，不改变列表头部）
        self.assertIn(result.swing_low_20, result.support_levels)

        d = result.to_dict()
        for key in ("atr_14", "atr_pct", "swing_low_10", "swing_low_20", "swing_high_20"):
            self.assertIn(key, d)


class ComputeTradeLevelsTestCase(unittest.TestCase):
    def test_atr_stop_and_rr_math(self) -> None:
        # 无结构位约束：止损 = entry - 1.5*ATR，目标 = entry + 3*ATR → R:R = 2
        levels = compute_trade_levels({"atr_14": 2.0}, current_price=100.0)

        self.assertEqual(levels.entry, 100.0)
        self.assertAlmostEqual(levels.stop, 97.0)     # 100 - 1.5*2
        self.assertAlmostEqual(levels.target, 106.0)  # 100 + 3*2
        self.assertAlmostEqual(levels.r_multiple, 2.0)
        self.assertEqual(levels.quality, "good")

    def test_swing_low_tightens_stop_and_resistance_caps_target(self) -> None:
        # 结构低点 98 高于 ATR 止损 97 → 取较高者（98*0.995=97.51）
        # 压力位 103 低于 ATR 目标 106 → 目标被压至 103
        levels = compute_trade_levels(
            {"atr_14": 2.0, "swing_low_20": 98.0, "swing_high_20": 103.0},
            current_price=100.0,
        )

        self.assertAlmostEqual(levels.stop, 98.0 * 0.995)
        self.assertAlmostEqual(levels.target, 103.0)
        expected_rr = (103.0 - 100.0) / (100.0 - 98.0 * 0.995)
        self.assertAlmostEqual(levels.r_multiple, round(expected_rr, 4))
        # R:R ≈ 1.2 < 1.5 → 压力位过近，质量判定为 poor_risk_reward
        self.assertEqual(levels.quality, "poor_risk_reward")

    def test_atr_target_mult_raised_to_reach_min_rr(self) -> None:
        # 结构止损较宽（risk > 1.5*ATR）时，ATR 目标倍数应放大以保证 R:R >= 2
        levels = compute_trade_levels(
            {"atr_14": 1.0, "swing_low_20": 90.0},  # ATR止损 98.5 更紧 → risk=1.5
            current_price=100.0,
        )
        self.assertAlmostEqual(levels.stop, 98.5)
        self.assertGreaterEqual(levels.r_multiple, 2.0)
        self.assertEqual(levels.quality, "good")

    def test_missing_data_returns_none_fields_without_raising(self) -> None:
        for data, price in (
            (None, 100.0),
            ({}, 100.0),
            ({"atr_14": float("nan")}, 100.0),
            ({"atr_14": 2.0}, None),
            ({"atr_14": 2.0}, float("nan")),
        ):
            levels = compute_trade_levels(data, price)
            self.assertIsInstance(levels, TradeLevels)
            self.assertIsNone(levels.stop)
            self.assertIsNone(levels.target)
            self.assertIsNone(levels.r_multiple)
            self.assertEqual(levels.quality, "insufficient_data")

    def test_accepts_ohlc_dataframe_input(self) -> None:
        closes = [10 + i * 0.1 for i in range(30)]
        df = pd.DataFrame(
            {
                "high": [c * 1.02 for c in closes],
                "low": [c * 0.98 for c in closes],
                "close": closes,
            }
        )
        levels = compute_trade_levels(df, current_price=float(closes[-1]))

        self.assertIsNotNone(levels.stop)
        self.assertLess(levels.stop, levels.entry)
        self.assertIsNotNone(levels.atr)

    def test_us_price_scale_is_transparent(self) -> None:
        # 美股价位只是数值尺度不同，计算逻辑一致
        levels = compute_trade_levels({"atr_14": 5.0}, current_price=250.0)
        self.assertAlmostEqual(levels.stop, 242.5)
        self.assertAlmostEqual(levels.target, 265.0)
        self.assertEqual(levels.quality, "good")

    def test_short_direction_degrades_gracefully(self) -> None:
        levels = compute_trade_levels({"atr_14": 2.0}, current_price=100.0, direction="short")
        self.assertIsNone(levels.stop)
        self.assertEqual(levels.quality, "insufficient_data")


class ParseSniperValueCurrencyTestCase(unittest.TestCase):
    def test_existing_yuan_parsing_unchanged(self) -> None:
        self.assertEqual(parse_sniper_value("回踩MA5附近，约41元"), 41.0)
        self.assertEqual(parse_sniper_value("止损：61元（跌破MA20）"), 61.0)
        self.assertEqual(parse_sniper_value("1680"), 1680.0)
        self.assertIsNone(parse_sniper_value("N/A"))
        self.assertIsNone(parse_sniper_value(None))

    def test_dollar_prefixed_prices(self) -> None:
        self.assertEqual(parse_sniper_value("$150.50"), 150.5)
        self.assertEqual(parse_sniper_value("US$150.50"), 150.5)
        self.assertEqual(parse_sniper_value("回踩支撑位 $195 附近"), 195.0)
        # "元"路径优先解析不受影响的同时，纯MA描述+美元括号也能取到价格
        self.assertEqual(parse_sniper_value("回踩MA20（$145）"), 145.0)

    def test_plain_and_usd_suffix_numbers(self) -> None:
        self.assertEqual(parse_sniper_value("150.5"), 150.5)
        self.assertEqual(parse_sniper_value("目标 195 附近"), 195.0)
        self.assertEqual(parse_sniper_value("155美元"), 155.0)


class ValidateLevelsTestCase(unittest.TestCase):
    def test_valid_plan_passes(self) -> None:
        check = validate_levels(100.0, 95.0, 112.0)
        self.assertTrue(check["valid"])
        self.assertEqual(check["issues"], [])
        self.assertAlmostEqual(check["r_multiple"], 2.4)

    def test_inverted_stop_is_flagged(self) -> None:
        check = validate_levels(100.0, 105.0, 112.0)
        self.assertFalse(check["valid"])
        self.assertIn("stop_not_below_entry", check["issues"])

    def test_target_below_entry_is_flagged(self) -> None:
        check = validate_levels(100.0, 95.0, 98.0)
        self.assertFalse(check["valid"])
        self.assertIn("target_not_above_entry", check["issues"])

    def test_poor_risk_reward_is_flagged(self) -> None:
        # 风险 8，回报 2 → R:R = 0.25
        check = validate_levels(100.0, 92.0, 102.0)
        self.assertFalse(check["valid"])
        self.assertIn("poor_risk_reward", check["issues"])
        self.assertAlmostEqual(check["r_multiple"], 0.25)

    def test_far_from_price_sanity_bound(self) -> None:
        check = validate_levels(100.0, 95.0, 200.0, current_price=100.0)
        self.assertFalse(check["valid"])
        self.assertIn("target_far_from_price", check["issues"])

    def test_missing_and_nan_values_never_raise(self) -> None:
        for entry, stop, target in (
            (None, None, None),
            (float("nan"), 95.0, 110.0),
            ("bad", 95.0, 110.0),
            (100.0, None, 110.0),
            (-5, 95.0, 110.0),
        ):
            check = validate_levels(entry, stop, target)
            self.assertIsInstance(check["issues"], list)
            self.assertIsInstance(check["valid"], bool)

    def test_partial_plan_only_checks_available_levels(self) -> None:
        # 只有 entry+stop：几何正确 → valid，且不计算 R:R
        check = validate_levels(100.0, 95.0, None)
        self.assertTrue(check["valid"])
        self.assertIsNone(check["r_multiple"])


if __name__ == "__main__":
    unittest.main()
