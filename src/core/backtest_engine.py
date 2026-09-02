# -*- coding: utf-8 -*-
"""Backtesting evaluation engine (pure logic).

This module is intentionally DB-agnostic: it operates on plain values or
objects that look like daily OHLC bars.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple


OVERALL_SENTINEL_CODE = "__overall__"

# 结果打分方案标识：基准对称打分（以“空仓不动”为基准，多头/空仓立场对称使用同一 band）。
SCORING_SCHEME = "benchmark_relative_symmetric_v1"

# 置信度文本 → 概率 映射。
# 注意：与 src/services/decision_signal_extractor._CONFIDENCE_MAP 保持一致
# （extractor 依赖 analyzer 等重模块，回测引擎需保持 DB/LLM 无关，故在此镜像一份；
# tests/test_backtest_calibration.py 中有同步校验测试）。
CONFIDENCE_LEVEL_PROBABILITY = {
    "高": 0.8,
    "high": 0.8,
    "中": 0.6,
    "medium": 0.6,
    "mid": 0.6,
    "低": 0.4,
    "low": 0.4,
}


def confidence_level_to_probability(value: Any) -> Optional[float]:
    """将 高/中/低 等置信度文本映射为概率；无法识别时返回 None。"""
    key = str(value or "").strip().lower()
    return CONFIDENCE_LEVEL_PROBABILITY.get(key)


class DailyBarLike(Protocol):
    """Protocol for objects representing a daily OHLC bar."""

    date: date
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]


class BacktestResultLike(Protocol):
    """Protocol for objects that behave like a stored BacktestResult."""

    eval_status: str
    position_recommendation: Optional[str]
    outcome: Optional[str]
    direction_correct: Optional[bool]
    stock_return_pct: Optional[float]
    simulated_return_pct: Optional[float]
    hit_stop_loss: Optional[bool]
    hit_take_profit: Optional[bool]
    first_hit: Optional[str]
    first_hit_trading_days: Optional[int]
    operation_advice: Optional[str]


@dataclass(frozen=True)
class EvaluationConfig:
    eval_window_days: int
    neutral_band_pct: float = 2.0
    engine_version: str = "v1"


class BacktestEngine:
    """Long-only daily-bar backtesting engine."""

    # Operation advice keywords (Chinese + English)
    _BULLISH_KEYWORDS = (
        "买入",
        "加仓",
        "强烈买入",
        "增持",
        "建仓",
        "strong buy",
        "buy",
        "add",
    )
    _BEARISH_KEYWORDS = (
        "卖出",
        "减仓",
        "强烈卖出",
        "清仓",
        "strong sell",
        "sell",
        "reduce",
    )
    _HOLD_KEYWORDS = (
        "持有",
        "震荡观望",
        "洗盘观察",
        "持有观察",
        "hold",
        "range-bound watch",
        "shakeout watch",
        "hold and watch",
    )
    _WAIT_KEYWORDS = (
        "观望",
        "等待",
        "wait",
    )

    # Negation prefixes (trailing spaces stripped for suffix-matching against prefix text).
    # English patterns include trailing space in their canonical form; rstrip is
    # applied during matching so "do not" matches prefix "do not " or "do not".
    _NEGATION_PATTERNS = (
        "not", "don't", "do not", "no", "never", "avoid",  # English
        "不要", "不", "别", "勿", "没有",  # Chinese
    )

    _NEGATION_CONNECTOR_WORDS = (
        "建议",
        "应",
        "应当",
        "宜",
        "先",
        "再",
        "暂",
        "不必",
        "必须",
        "无需",
    )

    @classmethod
    def infer_direction_expected(cls, operation_advice: Optional[str]) -> str:
        """Infer expected direction: up/down/not_down/flat."""
        text = cls._normalize_text(operation_advice)
        if cls._matches_intent(text, cls._BEARISH_KEYWORDS):
            return "down"
        if cls._first_intent_position(text, cls._WAIT_KEYWORDS) is not None:
            wait_pos = cls._first_intent_position(text, cls._WAIT_KEYWORDS)
            bullish_pos = cls._first_intent_position(text, cls._BULLISH_KEYWORDS)
            hold_pos = cls._first_intent_position(text, cls._HOLD_KEYWORDS)
            if (bullish_pos is None or wait_pos < bullish_pos) and (
                hold_pos is None or wait_pos < hold_pos
            ):
                return "flat"
        if cls._matches_intent(text, cls._BULLISH_KEYWORDS):
            return "up"
        if cls._matches_intent(text, cls._HOLD_KEYWORDS):
            return "not_down"
        if cls._matches_intent(text, cls._WAIT_KEYWORDS):
            return "flat"
        return "flat"

    @classmethod
    def infer_position_recommendation(cls, operation_advice: Optional[str]) -> str:
        """Infer recommended position: long/cash (long-only system).

        Priority: bearish/wait -> cash, bullish/hold -> long, unrecognized -> cash.
        """
        text = cls._normalize_text(operation_advice)
        if cls._matches_intent(text, cls._BEARISH_KEYWORDS):
            return "cash"
        wait_pos = cls._first_intent_position(text, cls._WAIT_KEYWORDS)
        if wait_pos is not None:
            bullish_pos = cls._first_intent_position(text, cls._BULLISH_KEYWORDS)
            hold_pos = cls._first_intent_position(text, cls._HOLD_KEYWORDS)
            if (bullish_pos is None or wait_pos < bullish_pos) and (
                hold_pos is None or wait_pos < hold_pos
            ):
                return "cash"
        if cls._matches_intent(text, cls._BULLISH_KEYWORDS) or cls._matches_intent(text, cls._HOLD_KEYWORDS):
            return "long"
        if cls._matches_intent(text, cls._WAIT_KEYWORDS):
            return "cash"
        return "cash"

    @classmethod
    def evaluate_single(
        cls,
        *,
        operation_advice: Optional[str],
        analysis_date: date,
        start_price: float,
        forward_bars: Sequence[DailyBarLike],
        stop_loss: Optional[float],
        take_profit: Optional[float],
        config: EvaluationConfig,
    ) -> Dict[str, Any]:
        """Evaluate one historical analysis against forward daily bars.

        Notes:
        - Daily bars cannot determine intraday ordering. If stop-loss and
          take-profit are both touched in the same bar, we record
          first_hit="ambiguous" and assume stop-loss first for simulated exit.
        """

        if start_price is None or start_price <= 0:
            return {
                "analysis_date": analysis_date,
                "operation_advice": operation_advice,
                "position_recommendation": cls.infer_position_recommendation(operation_advice),
                "direction_expected": cls.infer_direction_expected(operation_advice),
                "eval_status": "error",
            }

        eval_days = int(config.eval_window_days)
        if eval_days <= 0:
            raise ValueError("eval_window_days must be positive")

        if len(forward_bars) < eval_days:
            return {
                "analysis_date": analysis_date,
                "operation_advice": operation_advice,
                "position_recommendation": cls.infer_position_recommendation(operation_advice),
                "direction_expected": cls.infer_direction_expected(operation_advice),
                "eval_status": "insufficient_data",
                "eval_window_days": eval_days,
            }

        window_bars = list(forward_bars[:eval_days])
        end_close = window_bars[-1].close
        highs = [b.high for b in window_bars if b.high is not None]
        lows = [b.low for b in window_bars if b.low is not None]
        max_high = max(highs) if highs else None
        min_low = min(lows) if lows else None

        stock_return_pct: Optional[float]
        if end_close is None:
            stock_return_pct = None
        else:
            stock_return_pct = (end_close - start_price) / start_price * 100

        direction_expected = cls.infer_direction_expected(operation_advice)
        position = cls.infer_position_recommendation(operation_advice)

        outcome, direction_correct = cls._classify_outcome(
            stock_return_pct=stock_return_pct,
            direction_expected=direction_expected,
            neutral_band_pct=config.neutral_band_pct,
        )

        (
            hit_stop_loss,
            hit_take_profit,
            first_hit,
            first_hit_date,
            first_hit_days,
            simulated_exit_price,
            simulated_exit_reason,
        ) = cls._evaluate_targets(
            position=position,
            stop_loss=stop_loss,
            take_profit=take_profit,
            window_bars=window_bars,
            end_close=end_close,
        )

        simulated_entry_price = start_price if position == "long" else None
        simulated_return_pct: Optional[float]
        if position != "long":
            simulated_return_pct = 0.0
        elif simulated_exit_price is None:
            simulated_return_pct = None
        else:
            simulated_return_pct = (simulated_exit_price - start_price) / start_price * 100

        return {
            "analysis_date": analysis_date,
            "eval_window_days": eval_days,
            "engine_version": config.engine_version,
            "eval_status": "completed",
            "operation_advice": operation_advice,
            "position_recommendation": position,
            "start_price": start_price,
            "end_close": end_close,
            "max_high": max_high,
            "min_low": min_low,
            "stock_return_pct": stock_return_pct,
            "direction_expected": direction_expected,
            "direction_correct": direction_correct,
            "outcome": outcome,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "hit_stop_loss": hit_stop_loss,
            "hit_take_profit": hit_take_profit,
            "first_hit": first_hit,
            "first_hit_date": first_hit_date,
            "first_hit_trading_days": first_hit_days,
            "simulated_entry_price": simulated_entry_price,
            "simulated_exit_price": simulated_exit_price,
            "simulated_exit_reason": simulated_exit_reason,
            "simulated_return_pct": simulated_return_pct,
        }

    @classmethod
    def evaluate_decision_signal(
        cls,
        *,
        direction_expected: str,
        anchor_date: date,
        start_price: float,
        forward_bars: Sequence[DailyBarLike],
        config: EvaluationConfig,
    ) -> Dict[str, Any]:
        """Evaluate a structured DecisionSignal action without text inference."""

        start_price_value = cls._finite_optional_float(start_price)
        if start_price_value is None or start_price_value <= 0:
            return {
                "anchor_date": anchor_date,
                "direction_expected": direction_expected,
                "eval_status": "unable",
                "unable_reason": "invalid_anchor_price",
            }

        eval_days = int(config.eval_window_days)
        if eval_days <= 0:
            raise ValueError("eval_window_days must be positive")

        if len(forward_bars) < eval_days:
            return {
                "anchor_date": anchor_date,
                "eval_window_days": eval_days,
                "engine_version": config.engine_version,
                "direction_expected": direction_expected,
                "eval_status": "unable",
                "unable_reason": "insufficient_forward_bars",
            }

        window_bars = list(forward_bars[:eval_days])
        raw_end_close = window_bars[-1].close
        end_close = cls._finite_optional_float(raw_end_close)
        highs: List[float] = []
        lows: List[float] = []
        for bar in window_bars:
            high = cls._finite_optional_float(bar.high)
            low = cls._finite_optional_float(bar.low)
            if high is not None:
                highs.append(high)
            if low is not None:
                lows.append(low)
        max_high = max(highs) if highs else None
        min_low = min(lows) if lows else None

        stock_return_pct: Optional[float]
        if end_close is None:
            stock_return_pct = None
        else:
            stock_return_pct = (end_close - start_price_value) / start_price_value * 100

        outcome, direction_correct = cls._classify_signal_outcome(
            stock_return_pct=stock_return_pct,
            direction_expected=direction_expected,
            neutral_band_pct=config.neutral_band_pct,
        )

        if stock_return_pct is None:
            return {
                "anchor_date": anchor_date,
                "eval_window_days": eval_days,
                "engine_version": config.engine_version,
                "direction_expected": direction_expected,
                "eval_status": "unable",
                "unable_reason": "missing_end_close" if raw_end_close is None else "invalid_end_close",
                "start_price": start_price_value,
                "end_close": end_close,
                "max_high": max_high,
                "min_low": min_low,
            }

        return {
            "anchor_date": anchor_date,
            "eval_window_days": eval_days,
            "engine_version": config.engine_version,
            "eval_status": "completed",
            "direction_expected": direction_expected,
            "direction_correct": direction_correct,
            "outcome": outcome,
            "start_price": start_price_value,
            "end_close": end_close,
            "max_high": max_high,
            "min_low": min_low,
            "stock_return_pct": stock_return_pct,
        }

    @classmethod
    def compute_summary(
        cls,
        *,
        results: Iterable[BacktestResultLike],
        scope: str,
        code: Optional[str],
        eval_window_days: int,
        engine_version: str,
        probabilities: Optional[Mapping[Any, float]] = None,
    ) -> Dict[str, Any]:
        """Aggregate BacktestResult rows into summary metrics.

        指标口径说明：
        - direction_accuracy_pct / win_rate_pct（legacy）：分母只含 win+loss，
          neutral（含对冲/观望型建议）被排除，保留以兼容旧看板。
        - strict_accuracy_pct（新增）：win / (win+loss+neutral)，neutral 计入分母，
          对冲不再免费提升整体准确率。
        - direction_breakdown（新增）：按 direction_expected（up/not_down/flat/down）
          分组的命中统计，用于观察“对冲型”建议的占比与表现。
        - calibration（新增，需传入 probabilities）：Brier 分数 + 可靠性表。
          probabilities 为 {analysis_history_id: 预测概率} 映射，概率取分析输出中的
          数值 p_up（若有），否则回退到 高/中/低 置信度映射（0.8/0.6/0.4）。
        """
        results_list = list(results)

        total = len(results_list)
        completed = [r for r in results_list if (r.eval_status or "") == "completed"]
        insufficient_count = sum(1 for r in results_list if (r.eval_status or "") == "insufficient_data")

        long_count = sum(1 for r in completed if (r.position_recommendation or "") == "long")
        cash_count = sum(1 for r in completed if (r.position_recommendation or "") == "cash")

        win_count = sum(1 for r in completed if (r.outcome or "") == "win")
        loss_count = sum(1 for r in completed if (r.outcome or "") == "loss")
        neutral_count = sum(1 for r in completed if (r.outcome or "") == "neutral")

        direction_denominator = sum(1 for r in completed if r.direction_correct is not None)
        direction_numerator = sum(1 for r in completed if r.direction_correct is True)
        direction_accuracy_pct = (
            round(direction_numerator / direction_denominator * 100, 2) if direction_denominator else None
        )

        win_loss_denominator = win_count + loss_count
        win_rate_pct = round(win_count / win_loss_denominator * 100, 2) if win_loss_denominator else None
        neutral_rate_pct = round(neutral_count / len(completed) * 100, 2) if completed else None

        # 严格口径：neutral 计入分母（对冲/观望不再被排除在问责之外）
        scored_count = win_count + loss_count + neutral_count
        strict_accuracy_pct = round(win_count / scored_count * 100, 2) if scored_count else None

        avg_stock_return_pct = cls._average([r.stock_return_pct for r in completed])
        avg_simulated_return_pct = cls._average([r.simulated_return_pct for r in completed])

        stop_applicable = [
            r
            for r in completed
            if (r.position_recommendation or "") == "long" and r.hit_stop_loss is not None
        ]
        stop_loss_trigger_rate = (
            round(sum(1 for r in stop_applicable if r.hit_stop_loss is True) / len(stop_applicable) * 100, 2)
            if stop_applicable
            else None
        )

        take_profit_applicable = [
            r
            for r in completed
            if (r.position_recommendation or "") == "long" and r.hit_take_profit is not None
        ]
        take_profit_trigger_rate = (
            round(
                sum(1 for r in take_profit_applicable if r.hit_take_profit is True) / len(take_profit_applicable) * 100,
                2,
            )
            if take_profit_applicable
            else None
        )

        any_target_applicable = [
            r
            for r in completed
            if (r.position_recommendation or "") == "long"
            and (r.hit_stop_loss is not None or r.hit_take_profit is not None)
        ]
        ambiguous_rate = (
            round(
                sum(1 for r in any_target_applicable if (r.first_hit or "") == "ambiguous")
                / len(any_target_applicable)
                * 100,
                2,
            )
            if any_target_applicable
            else None
        )
        avg_days_to_first_hit = cls._average(
            [
                float(r.first_hit_trading_days)
                for r in any_target_applicable
                if r.first_hit_trading_days is not None and (r.first_hit or "") in ("stop_loss", "take_profit", "ambiguous")
            ]
        )

        advice_breakdown = cls._compute_advice_breakdown(completed)
        direction_breakdown = cls._compute_direction_breakdown(completed)
        diagnostics = cls._compute_diagnostics(results_list)

        calibration: Optional[Dict[str, Any]] = None
        if probabilities is not None:
            calls = []
            for r in completed:
                if (r.outcome or "") not in ("win", "loss", "neutral"):
                    continue
                key = getattr(r, "analysis_history_id", None)
                if key is None:
                    continue
                probability = probabilities.get(key)
                if probability is None:
                    continue
                calls.append((probability, r.direction_correct))
            calibration = cls.compute_calibration(calls)

        return {
            "scope": scope,
            "code": code,
            "eval_window_days": int(eval_window_days),
            "engine_version": engine_version,
            "total_evaluations": total,
            "completed_count": len(completed),
            "insufficient_count": insufficient_count,
            "long_count": long_count,
            "cash_count": cash_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "neutral_count": neutral_count,
            "direction_accuracy_pct": direction_accuracy_pct,
            "win_rate_pct": win_rate_pct,
            "neutral_rate_pct": neutral_rate_pct,
            "avg_stock_return_pct": avg_stock_return_pct,
            "avg_simulated_return_pct": avg_simulated_return_pct,
            "stop_loss_trigger_rate": stop_loss_trigger_rate,
            "take_profit_trigger_rate": take_profit_trigger_rate,
            "ambiguous_rate": ambiguous_rate,
            "avg_days_to_first_hit": avg_days_to_first_hit,
            "advice_breakdown": advice_breakdown,
            "diagnostics": diagnostics,
            # 新增指标（保留旧字段，不复用旧名）
            "scoring_scheme": SCORING_SCHEME,
            "scored_count": scored_count,
            "strict_accuracy_pct": strict_accuracy_pct,
            "direction_breakdown": direction_breakdown,
            "calibration": calibration,
        }

    @staticmethod
    def _normalize_text(value: Optional[str]) -> str:
        return str(value or "").strip().lower()

    @classmethod
    def _matches_intent(cls, text: str, keywords: Sequence[str]) -> bool:
        """Check if text expresses the intent of any keyword, accounting for negation.

        Tier 1: exact match (covers clean labels like "买入", "hold").
        Tier 2: substring match with negation guard.
        Keywords are assumed to be lowercase (matching _normalize_text output).
        """
        return cls._first_intent_position(text, keywords) is not None

    @classmethod
    def _first_intent_position(cls, text: str, keywords: Sequence[str]) -> Optional[int]:
        """Return the earliest match position for intent keywords, or None."""
        if not text:
            return None

        best_pos: Optional[int] = None

        for kw in keywords:
            if not kw:
                continue
            if text == kw:
                return 0

            keyword = kw.lower().strip()
            if not keyword:
                continue

            # Use word-boundary matching for ASCII keywords to avoid
            # false positives such as "watch" matching "wait".
            if bool(re.search(r"[a-z]", keyword)):
                for match in re.finditer(
                    rf"(?<![a-zA-Z0-9_]){re.escape(keyword)}(?![a-zA-Z0-9_])",
                    text,
                ):
                    if not cls._is_negated(text[: match.start()], keyword):
                        pos = match.start()
                        if best_pos is None or pos < best_pos:
                            best_pos = pos
                            break
                    continue

            # For non-ASCII terms (Chinese), use substring matching to keep
            # natural language phrasings like "建议买入" effective.
            if re.search(r"[\u4e00-\u9fff]", keyword):
                start = 0
                while True:
                    match_idx = text.find(keyword, start)
                    if match_idx < 0:
                        break
                    if not cls._is_negated(text[:match_idx], keyword):
                        if best_pos is None or match_idx < best_pos:
                            best_pos = match_idx
                        break
                    start = match_idx + len(keyword)
                continue

        return best_pos

    @classmethod
    def _is_negated(cls, prefix: str, keyword: str) -> bool:
        """Check if the prefix text indicates negation for a candidate intent."""
        stripped = prefix.rstrip()
        target = (keyword or "").lower().strip()
        if not target:
            return False

        if any(stripped.endswith(neg) for neg in cls._NEGATION_PATTERNS):
            return True

        # 限定“否定 + 动作动词”匹配，避免将“条件位否定”误伤核心建议意图。
        lookback = stripped[-12:]
        for neg in cls._NEGATION_PATTERNS:
            if not neg:
                continue
            neg_idx = lookback.rfind(neg)
            if neg_idx < 0:
                continue

            suffix_gap = lookback[neg_idx + len(neg):].strip()
            if not suffix_gap:
                return True
            if any(ch in suffix_gap for ch in "，,。；;:!?！？"):
                continue

            if cls._contains_keyword(suffix_gap, target):
                return True

            # Keep English short-gap behavior where negation words are followed by
            # connector words such as "to" (e.g. "not to sell").
            if not any(ch >= "\u4e00" and ch <= "\u9fff" for ch in suffix_gap):
                if len(suffix_gap) <= 6:
                    return True
                continue

            if cls._is_negation_connector_gap(suffix_gap):
                return True

        return False

    @classmethod
    def _contains_keyword(cls, text: str, keyword: str) -> bool:
        """Check whether *keyword* exists in text with intent-aware boundaries."""
        if not text or not keyword:
            return False
        if bool(re.search(r"[a-z]", keyword)):
            return bool(re.search(rf"(?<![a-zA-Z0-9_]){re.escape(keyword)}(?![a-zA-Z0-9_])", text))
        return keyword in text

    @classmethod
    def _is_negation_connector_gap(cls, gap: str) -> bool:
        """Whether a short Chinese negation gap is still a valid negation bridge."""
        compact = re.sub(r"[\s,，。；;:!?！？]", "", gap).strip()
        if not compact:
            return True
        return compact in cls._NEGATION_CONNECTOR_WORDS

    # 多头立场（继续持有敞口的建议）与空仓立场（放弃敞口的建议）。
    _LONG_STANCE_DIRECTIONS = frozenset({"up", "not_down"})
    _CASH_STANCE_DIRECTIONS = frozenset({"down", "flat", "not_up"})

    @classmethod
    def _classify_outcome(
        cls,
        *,
        stock_return_pct: Optional[float],
        direction_expected: str,
        neutral_band_pct: float,
    ) -> tuple[Optional[str], Optional[bool]]:
        """基准对称打分（SCORING_SCHEME = benchmark_relative_symmetric_v1）。

        以“空仓不动（abstain）”作为收益基准，衡量每个建议相对基准的价值。
        统一规则（band = |neutral_band_pct|，r = 窗口收益率%）：

        - 多头立场（direction_expected ∈ {up, not_down}，即 买入/持有）：
          r >= +band → win；r <= -band → loss；|r| < band → neutral。
        - 空仓立场（direction_expected ∈ {down, flat}，即 卖出/观望）：
          r <= -band → win（成功规避超过 band 的下跌）；
          r >= +band → loss（踏空超过 band 的上涨，机会成本计为失误）；
          |r| < band → neutral。

        与旧版（非对称）方案的区别：
        - 持有(not_down)不再“r >= 0 即胜”，与买入使用相同的 +band 胜利门槛；
        - 观望(flat)不再因“什么都没发生”(|r| <= band) 免费得胜，改计 neutral；
          且踏空 > band 的上涨记为 loss，规避 > band 的下跌记为 win。

        neutral 仍返回 direction_correct=None（供 legacy win/(win+loss) 口径使用）；
        compute_summary 中新增的 strict_accuracy_pct 会把 neutral 计入分母。
        """
        if stock_return_pct is None:
            return None, None

        band = abs(float(neutral_band_pct))
        r = float(stock_return_pct)

        if direction_expected in cls._LONG_STANCE_DIRECTIONS:
            if r >= band:
                return "win", True
            if r <= -band:
                return "loss", False
            return "neutral", None

        # 空仓立场（down/flat 及未识别方向，与 infer_position_recommendation 的 cash 默认一致）
        if r <= -band:
            return "win", True
        if r >= band:
            return "loss", False
        return "neutral", None

    @classmethod
    def _classify_signal_outcome(
        cls,
        *,
        stock_return_pct: Optional[float],
        direction_expected: str,
        neutral_band_pct: float,
    ) -> tuple[Optional[str], Optional[bool]]:
        """DecisionSignal 版基准对称打分，规则与 _classify_outcome 相同。

        - up / not_down（buy、add / hold）：r >= +band → hit；r <= -band → miss；否则 neutral。
        - not_up（sell、reduce、avoid）：r <= -band → hit；r >= +band → miss；否则 neutral。

        与旧版的区别：not_down 不再“r >= 0 即 hit”；not_up 不再“r <= +band 即 hit”
        （原方案下横盘也算 hit），横盘一律记 neutral。
        """
        if stock_return_pct is None:
            return None, None

        band = abs(float(neutral_band_pct))
        r = float(stock_return_pct)

        if direction_expected in ("up", "not_down"):
            if r >= band:
                return "hit", True
            if r <= -band:
                return "miss", False
            return "neutral", None

        if direction_expected in ("not_up", "down"):
            if r <= -band:
                return "hit", True
            if r >= band:
                return "miss", False
            return "neutral", None

        return None, None

    @staticmethod
    def _finite_optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _evaluate_targets(
        cls,
        *,
        position: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        window_bars: List[DailyBarLike],
        end_close: Optional[float],
    ) -> tuple[
        Optional[bool],
        Optional[bool],
        str,
        Optional[date],
        Optional[int],
        Optional[float],
        str,
    ]:
        if position != "long":
            return (
                None,
                None,
                "not_applicable",
                None,
                None,
                None,
                "cash",
            )

        has_any_target = stop_loss is not None or take_profit is not None
        if not has_any_target:
            return (
                None,
                None,
                "neither",
                None,
                None,
                end_close,
                "window_end",
            )

        hit_sl: Optional[bool] = None if stop_loss is None else False
        hit_tp: Optional[bool] = None if take_profit is None else False
        first_hit = "neither"
        first_hit_date: Optional[date] = None
        first_hit_days: Optional[int] = None
        exit_price: Optional[float] = end_close
        exit_reason = "window_end"

        for idx, bar in enumerate(window_bars, start=1):
            low = bar.low
            high = bar.high
            stop_hit = stop_loss is not None and low is not None and low <= stop_loss
            tp_hit = take_profit is not None and high is not None and high >= take_profit

            if stop_hit:
                hit_sl = True
            if tp_hit:
                hit_tp = True

            if not stop_hit and not tp_hit:
                continue

            first_hit_date = bar.date
            first_hit_days = idx

            if stop_hit and tp_hit:
                first_hit = "ambiguous"
                exit_price = stop_loss
                exit_reason = "ambiguous_stop_loss"
                break

            if stop_hit:
                first_hit = "stop_loss"
                exit_price = stop_loss
                exit_reason = "stop_loss"
                break

            first_hit = "take_profit"
            exit_price = take_profit
            exit_reason = "take_profit"
            break

        return (
            hit_sl,
            hit_tp,
            first_hit,
            first_hit_date,
            first_hit_days,
            exit_price,
            exit_reason,
        )

    @staticmethod
    def _average(values: Iterable[Optional[float]]) -> Optional[float]:
        items = [float(v) for v in values if v is not None]
        if not items:
            return None
        return round(sum(items) / len(items), 4)

    @staticmethod
    def _compute_advice_breakdown(results: List[BacktestResultLike]) -> Dict[str, Any]:
        breakdown: Dict[str, Dict[str, int]] = {}
        for row in results:
            raw_advice = row.operation_advice
            advice = (raw_advice if isinstance(raw_advice, str) else str(raw_advice or "")).strip() or "(unknown)"
            bucket = breakdown.setdefault(advice, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
            bucket["total"] += 1
            outcome = (row.outcome or "").strip()
            if outcome in ("win", "loss", "neutral"):
                bucket[outcome] += 1

        enriched: Dict[str, Any] = {}
        for advice, bucket in breakdown.items():
            win = bucket["win"]
            loss = bucket["loss"]
            denom = win + loss
            win_rate = round(win / denom * 100, 2) if denom else None
            scored = win + loss + bucket["neutral"]
            strict_accuracy = round(win / scored * 100, 2) if scored else None
            enriched[advice] = {
                **bucket,
                "win_rate_pct": win_rate,
                "strict_accuracy_pct": strict_accuracy,
            }
        return enriched

    @staticmethod
    def _compute_direction_breakdown(results: List[BacktestResultLike]) -> Dict[str, Any]:
        """按 direction_expected 分组统计命中情况（up/not_down/flat/down）。

        用于让“对冲型”建议（hold→not_down、观望→flat）的表现单独可见，
        而不是混在整体胜率里。
        """
        breakdown: Dict[str, Dict[str, int]] = {}
        for row in results:
            direction = str(getattr(row, "direction_expected", None) or "").strip() or "(unknown)"
            bucket = breakdown.setdefault(direction, {"total": 0, "win": 0, "loss": 0, "neutral": 0})
            bucket["total"] += 1
            outcome = (row.outcome or "").strip()
            if outcome in ("win", "loss", "neutral"):
                bucket[outcome] += 1

        enriched: Dict[str, Any] = {}
        for direction, bucket in breakdown.items():
            win = bucket["win"]
            loss = bucket["loss"]
            denom = win + loss
            scored = denom + bucket["neutral"]
            enriched[direction] = {
                **bucket,
                "win_rate_pct": round(win / denom * 100, 2) if denom else None,
                "strict_accuracy_pct": round(win / scored * 100, 2) if scored else None,
            }
        return enriched

    @staticmethod
    def compute_calibration(
        calls: Iterable[Tuple[Optional[float], Optional[bool]]],
    ) -> Dict[str, Any]:
        """计算置信度校准指标：Brier 分数 + 可靠性表。

        calls: (predicted_probability, direction_correct) 序列。
        - 严格口径：direction_correct 为 True 记 1，其余（False 或 neutral 的 None）记 0，
          与 strict_accuracy_pct 保持一致——对冲/中性结果同样计入校准。
        - 概率非法（None、越界、非有限）的样本跳过，不计入 sample_count。

        可靠性表按 0.1 宽度分桶（[0.0,0.1) ... [0.9,1.0]），当前 高/中/低 映射的
        0.8/0.6/0.4 会落入互不重叠的桶；未来更细的数值 p_up 无需改动即可使用。
        每桶报告样本数、平均预测概率与实际命中率，便于对比预测 vs 实际。
        """
        samples: List[Tuple[float, float]] = []
        for probability, correct in calls:
            if probability is None:
                continue
            try:
                p = float(probability)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(p) or p < 0.0 or p > 1.0:
                continue
            y = 1.0 if correct is True else 0.0
            samples.append((p, y))

        if not samples:
            return {"brier_score": None, "sample_count": 0, "reliability_table": []}

        brier = sum((p - y) ** 2 for p, y in samples) / len(samples)

        buckets: Dict[int, Dict[str, float]] = {}
        for p, y in samples:
            idx = min(int(p * 10), 9)
            bucket = buckets.setdefault(idx, {"count": 0, "predicted_sum": 0.0, "hits": 0})
            bucket["count"] += 1
            bucket["predicted_sum"] += p
            bucket["hits"] += int(y)

        reliability_table = [
            {
                "p_low": round(idx / 10, 2),
                "p_high": round((idx + 1) / 10, 2),
                "count": int(bucket["count"]),
                "avg_predicted": round(bucket["predicted_sum"] / bucket["count"], 4),
                "hit_count": int(bucket["hits"]),
                "realized_hit_rate_pct": round(bucket["hits"] / bucket["count"] * 100, 2),
            }
            for idx, bucket in sorted(buckets.items())
        ]

        return {
            "brier_score": round(brier, 4),
            "sample_count": len(samples),
            "reliability_table": reliability_table,
        }

    @staticmethod
    def _compute_diagnostics(results: List[BacktestResultLike]) -> Dict[str, Any]:
        status_counts: Dict[str, int] = {}
        first_hit_counts: Dict[str, int] = {}
        for row in results:
            status = (row.eval_status or "").strip() or "(unknown)"
            status_counts[status] = status_counts.get(status, 0) + 1
            first_hit = (row.first_hit or "").strip() or "(none)"
            first_hit_counts[first_hit] = first_hit_counts.get(first_hit, 0) + 1
        return {
            "eval_status": status_counts,
            "first_hit": first_hit_counts,
        }
