# -*- coding: utf-8 -*-
"""Tests for Issue #1381 daily market context decision guardrail.

新契约：大盘环境偏谨慎时守门只做"标注"（风险提示 + 仓位建议 + 元数据），
不改写 decision_type / operation_advice / sentiment_score / confidence_level。
"""

from __future__ import annotations

from src.analyzer import AnalysisResult
from src.daily_market_context_guardrail import apply_daily_market_context_guardrail


def _result() -> AnalysisResult:
    return AnalysisResult(
        code="600519",
        name="贵州茅台",
        sentiment_score=82,
        trend_prediction="看多",
        operation_advice="立即买入并积极加仓",
        decision_type="buy",
        confidence_level="高",
        analysis_summary="个股信号强势",
        dashboard={
            "operation_advice": "立即买入并积极加仓",
            "decision_type": "buy",
            "core_conclusion": {
                "one_sentence": "立即买入并积极加仓",
                "position_advice": {
                    "no_position": "立即买入并积极加仓",
                    "has_position": "继续加仓",
                },
            },
            "battle_plan": {
                "position_strategy": {
                    "suggested_position": "满仓买入",
                    "entry_plan": "突破后立即买入",
                    "risk_control": "回踩继续加仓",
                },
            },
            "phase_decision": {
                "data_limitations": [],
                "confidence_reason": "趋势强",
            },
        },
    )


_CONSERVATIVE_CONTEXT = {
    "region": "cn",
    "trade_date": "2026-06-06",
    "summary": "大盘退潮，高风险，建议观望，仓位上限30%。",
    "risk_tags": ["high_risk", "low_position_cap"],
}


def test_conservative_market_context_annotates_but_preserves_buy() -> None:
    result = _result()

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert adjustments == ["daily_market_context_risk_annotated"]
    # 模型结论保留：方向、建议、评分、置信度均不被守门改写
    assert result.decision_type == "buy"
    assert result.operation_advice == "立即买入并积极加仓"
    assert result.confidence_level == "高"
    assert result.sentiment_score == 82
    # 守门分歧写入元数据
    guardrail_meta = result.dashboard["daily_market_context_guardrail"]
    assert guardrail_meta["applied"] is True
    assert guardrail_meta["mode"] == "annotate"
    assert guardrail_meta["decision_type_preserved"] == "buy"
    assert guardrail_meta["sentiment_score_preserved"] == 82
    # 风险提示追加
    assert "大盘环境偏谨慎" in result.risk_warning
    # 仓位建议附加到既有风控策略上，而不是覆盖入场计划
    position_strategy = result.dashboard["battle_plan"]["position_strategy"]
    assert position_strategy["entry_plan"] == "突破后立即买入"
    assert "回踩继续加仓" in position_strategy["risk_control"]
    assert "下调仓位规模" in position_strategy["risk_control"]
    # phase_decision 限制与置信度理由被补充
    phase_decision = result.dashboard["phase_decision"]
    assert any("大盘环境" in item for item in phase_decision["data_limitations"])
    assert "大盘环境" in phase_decision["confidence_reason"]
    # core_conclusion 不被覆盖
    assert result.dashboard["core_conclusion"]["one_sentence"] == "立即买入并积极加仓"


def test_position_cap_only_market_context_annotates_aggressive_buy() -> None:
    cases = [
        ("zh", "市场震荡，仓位不超过30%。", "立即买入并积极加仓", "高"),
        ("en", "Major indices are mixed. Position limit 30%.", "Buy now and add aggressively.", "High"),
    ]
    for language, summary, advice, confidence in cases:
        result = _result()
        result.operation_advice = advice
        result.confidence_level = confidence

        adjustments = apply_daily_market_context_guardrail(
            result,
            daily_market_context={
                "region": "us" if language == "en" else "cn",
                "trade_date": "2026-06-06",
                "summary": summary,
                "risk_tags": [],
                "position_cap": "30%",
            },
            report_language=language,
        )

        assert "daily_market_context_risk_annotated" in adjustments
        assert result.decision_type == "buy"
        assert result.operation_advice == advice
        assert result.confidence_level == confidence
        assert result.dashboard["daily_market_context_guardrail"]["applied"] is True


def test_neutral_market_context_leaves_hold_unchanged() -> None:
    result = _result()
    result.decision_type = "hold"
    result.operation_advice = "持有观察"
    result.confidence_level = "中"

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context={
            "region": "cn",
            "trade_date": "2026-06-06",
            "summary": "市场震荡，结构分化。",
            "risk_tags": [],
        },
        report_language="zh",
    )

    assert adjustments == []
    assert result.decision_type == "hold"
    assert result.operation_advice == "持有观察"
    assert "daily_market_context_guardrail" not in result.dashboard


def test_conservative_market_context_does_not_annotate_negative_buy_language() -> None:
    result = _result()
    result.decision_type = "buy"
    result.operation_advice = "暂不加仓，继续持有观察。"
    result.confidence_level = "高"

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert adjustments == []
    assert result.decision_type == "buy"
    assert result.operation_advice == "暂不加仓，继续持有观察。"


def test_conservative_market_context_does_not_annotate_no_action_in_english() -> None:
    result = _result()
    result.decision_type = "hold"
    result.operation_advice = "No add now; keep watching for confirmation."

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context={
            "region": "us",
            "trade_date": "2026-06-06",
            "summary": "Market cooling and elevated risk. Cautious on new positions."
        },
        report_language="en",
    )

    assert adjustments == []
    assert result.decision_type == "hold"
    assert result.operation_advice == "No add now; keep watching for confirmation."


def test_conservative_market_context_does_not_annotate_explicit_negative_add_position() -> None:
    result = _result()
    result.decision_type = "buy"
    result.operation_advice = "不建议加仓，等待窗口更清晰。"
    result.confidence_level = "高"

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert adjustments == []
    assert result.decision_type == "buy"
    assert result.operation_advice == "不建议加仓，等待窗口更清晰。"


def test_conservative_market_context_annotates_generic_buy_advice_phrase() -> None:
    result = _result()
    result.operation_advice = "回踩买入，强支撑上攻。"
    result.confidence_level = "高"

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert "daily_market_context_risk_annotated" in adjustments
    assert result.decision_type == "buy"
    assert result.operation_advice == "回踩买入，强支撑上攻。"
    assert "大盘环境偏谨慎" in result.risk_warning


def test_conservative_market_context_annotates_when_risk_warning_then_recommend_buy() -> None:
    result = _result()
    result.decision_type = "buy"
    result.operation_advice = "风险不能忽视，但建议买入等待确认信号。"
    result.confidence_level = "高"

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert "daily_market_context_risk_annotated" in adjustments
    assert result.decision_type == "buy"
    assert result.operation_advice == "风险不能忽视，但建议买入等待确认信号。"


def test_conservative_market_context_annotates_when_negated_chase_then_recommend_buy() -> None:
    result = _result()
    result.decision_type = "buy"
    result.operation_advice = "不建议追高，但建议分批买入。"
    result.confidence_level = "高"

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert "daily_market_context_risk_annotated" in adjustments
    assert result.decision_type == "buy"
    assert result.operation_advice == "不建议追高，但建议分批买入。"


def test_conservative_market_context_does_not_annotate_buy_when_negated_explicitly_in_english() -> None:
    result = _result()
    result.decision_type = "buy"
    result.operation_advice = "No buy now; avoid adding."

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="en",
    )

    assert adjustments == []
    assert result.decision_type == "buy"
    assert result.operation_advice == "No buy now; avoid adding."


def test_conservative_market_context_does_not_annotate_do_not_buy_in_english() -> None:
    result = _result()
    result.decision_type = "buy"
    result.operation_advice = "Do not buy now; sell into strength."

    adjustments = apply_daily_market_context_guardrail(
        result,
        daily_market_context={
            "region": "us",
            "trade_date": "2026-06-06",
            "summary": "Market cooling and elevated risk. Cautious on new positions.",
        },
        report_language="en",
    )

    assert adjustments == []
    assert result.decision_type == "buy"
    assert result.operation_advice == "Do not buy now; sell into strength."


def test_annotation_is_idempotent_for_risk_warning() -> None:
    result = _result()

    apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )
    first_warning = result.risk_warning
    apply_daily_market_context_guardrail(
        result,
        daily_market_context=_CONSERVATIVE_CONTEXT,
        report_language="zh",
    )

    assert result.risk_warning == first_warning
