# -*- coding: utf-8 -*-
"""Decision guardrail using daily market context for Issue #1381.

守门原则：大盘环境偏谨慎时只做"标注"（风险提示 + 仓位建议 + 元数据），
不改写模型的 decision_type / operation_advice / sentiment_score / confidence_level。
守门分歧记录在 ``dashboard.daily_market_context_guardrail``，供下游展示与统计。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, List

from src.report_language import normalize_report_language


_CONSERVATIVE_TAGS = {"high_risk", "market_cooling", "conservative", "low_position_cap"}
_CONSERVATIVE_TEXT_MARKERS_ZH = ("退潮", "观望", "高风险", "谨慎", "保守", "仓位上限", "仓位不超过", "轻仓")
_CONSERVATIVE_TEXT_MARKERS_EN = ("high risk", "risk-off", "risk off", "watch", "cautious", "conservative", "position cap", "position limit")
_CONSERVATIVE_TEXT_MARKERS_KO = ("고위험", "관망", "위험", "신중", "보수", "비중 상한", "비중 축소", "경량")
_AGGRESSIVE_BUY_MARKERS_ZH = (
    "立即买入",
    "马上买入",
    "建议买入",
    "分批买入",
    "分批低吸",
    "回踩买入",
    "积极买入",
    "激进买入",
    "追高",
    "加仓",
)
_AGGRESSIVE_BUY_MARKERS_EN = ("buy now", "strong buy", "aggressive buy", "chase", "add aggressively")
_AGGRESSIVE_BUY_MARKERS_KO = (
    "즉시 매수",
    "지금 매수",
    "매수 추천",
    "분할 매수",
    "적극 매수",
    "공격적 매수",
    "추격 매수",
    "비중 확대",
)
_NEGATION_HINTS_ZH = ("暂不", "不建议", "不应", "不宜", "不能", "无法", "不允许", "禁止", "避免", "不要", "别", "先不")
_NEGATION_HINTS_EN = (" not ", "do not", "don't", "no ", "never", "avoid")
_NEGATION_HINTS_KO = ("권하지 않", "하지 않", "하지 마", "불가", "금지", "피하", "보류", "않", "말")
_NEGATION_LOOKBACK = 16


def _negation_hints_for(language: str) -> tuple[str, ...]:
    if language == "en":
        return _NEGATION_HINTS_EN
    if language == "ko":
        return _NEGATION_HINTS_KO
    return _NEGATION_HINTS_ZH


def apply_daily_market_context_guardrail(
    result: Any,
    *,
    daily_market_context: Any,
    report_language: str = "zh",
) -> List[str]:
    """Annotate aggressive buy advice when daily market context is conservative.

    只补充风险提示、仓位建议与元数据；保留模型的方向、评分与置信度。
    """

    if result is None or not _is_conservative_context(daily_market_context):
        return []

    language = normalize_report_language(report_language or getattr(result, "report_language", "zh"))
    if not _has_aggressive_buy_signal(result, language=language):
        return []

    adjustments: List[str] = ["daily_market_context_risk_annotated"]

    caution_note = _caution_note(language)

    risk_warning = str(getattr(result, "risk_warning", "") or "")
    if caution_note not in risk_warning:
        separator = "; " if language == "en" else "；"
        result.risk_warning = f"{risk_warning}{separator}{caution_note}" if risk_warning else caution_note

    dashboard = getattr(result, "dashboard", None)
    if not isinstance(dashboard, dict):
        dashboard = {}
        result.dashboard = dashboard

    dashboard["daily_market_context_guardrail"] = {
        "applied": True,
        "mode": "annotate",
        "reason": caution_note,
        "decision_type_preserved": str(getattr(result, "decision_type", "") or ""),
        "sentiment_score_preserved": getattr(result, "sentiment_score", None),
        "sizing_note": _sizing_note(language),
    }

    _append_sizing_note_to_position_strategy(dashboard, language=language)

    phase_decision = dashboard.get("phase_decision")
    if not isinstance(phase_decision, dict):
        phase_decision = {}
        dashboard["phase_decision"] = phase_decision
    _append_softening_limitation(phase_decision, language=language)

    return adjustments


def _caution_note(language: str) -> str:
    if language == "en":
        return (
            "Daily market context is conservative/high risk; keep the buy plan but "
            "prefer a smaller position and strict risk control."
        )
    if language == "ko":
        return "대시장 환경이 보수적/고위험이므로 매수 계획은 유지하되 비중을 줄이고 리스크 관리를 엄격히 하세요."
    return "大盘环境偏谨慎/高风险：买入计划可执行，但建议降低仓位并严格风控。"


def _sizing_note(language: str) -> str:
    if language == "en":
        return "Market-context caution: reduce suggested position size; do not add aggressively until market risk eases."
    if language == "ko":
        return "시장 환경 주의: 제안 비중을 줄이고, 시장 위험이 완화되기 전에는 공격적으로 늘리지 마세요."
    return "大盘环境提示：建议下调仓位规模，大盘风险缓解前不激进加仓。"


def _append_sizing_note_to_position_strategy(dashboard: dict[str, Any], *, language: str) -> None:
    """在既有仓位策略上附加提示，不覆盖模型给出的入场计划。"""
    battle_plan = dashboard.get("battle_plan")
    if not isinstance(battle_plan, dict):
        return
    position_strategy = battle_plan.get("position_strategy")
    if not isinstance(position_strategy, dict):
        position_strategy = {}
        battle_plan["position_strategy"] = position_strategy
    note = _sizing_note(language)
    risk_control = str(position_strategy.get("risk_control") or "")
    if note not in risk_control:
        separator = "; " if language == "en" else "；"
        position_strategy["risk_control"] = (
            f"{risk_control}{separator}{note}" if risk_control else note
        )


def _append_softening_limitation(phase_decision: dict[str, Any], *, language: str) -> None:
    limitations = phase_decision.get("data_limitations")
    if not isinstance(limitations, list):
        limitations = []
    if language == "en":
        limitation = "Daily market context is conservative/high risk; a risk/position-sizing annotation was added."
    elif language == "ko":
        limitation = "대시장 환경이 보수적/고위험이라 리스크·비중 관련 주석을 추가했습니다."
    else:
        limitation = "大盘环境偏谨慎/高风险，已附加风险与仓位标注。"
    if limitation not in limitations:
        limitations.append(limitation)
    phase_decision["data_limitations"] = limitations
    reason = str(phase_decision.get("confidence_reason") or "").strip()
    if language == "en":
        reason_note = "Market context suggests conservative position sizing."
    elif language == "ko":
        reason_note = "시장 환경상 보수적인 비중 관리가 필요합니다."
    else:
        reason_note = "大盘环境建议降低仓位规模并控制风险。"
    separator = "; " if language == "en" else "；"
    phase_decision["confidence_reason"] = (
        f"{reason}{separator}{reason_note}" if reason else reason_note
    )


def _is_conservative_context(context: Any) -> bool:
    if not isinstance(context, Mapping):
        return False
    tags = context.get("risk_tags")
    if isinstance(tags, list) and any(str(tag) in _CONSERVATIVE_TAGS for tag in tags):
        return True
    if str(context.get("position_cap") or "").strip():
        return True
    summary = str(context.get("summary") or "")
    lowered = summary.lower()
    return (
        any(marker in summary for marker in _CONSERVATIVE_TEXT_MARKERS_ZH)
        or any(marker in summary for marker in _CONSERVATIVE_TEXT_MARKERS_KO)
        or any(marker in lowered for marker in _CONSERVATIVE_TEXT_MARKERS_EN)
    )


def _has_aggressive_buy_signal(result: Any, *, language: str) -> bool:
    decision_type = str(getattr(result, "decision_type", "") or "").lower()
    if decision_type == "buy":
        advice = str(getattr(result, "operation_advice", "") or "")
        markers = _buy_markers(language)
        if _contains_any(advice, markers, language=language):
            return True
        if _contains_any(advice, markers, language=language, require_negation=True):
            return False
        return True
    advice = str(getattr(result, "operation_advice", "") or "")
    return _contains_any(advice, _buy_markers(language), language=language)


def _buy_markers(language: str) -> tuple[str, ...]:
    if language == "en":
        return _AGGRESSIVE_BUY_MARKERS_EN
    if language == "ko":
        return _AGGRESSIVE_BUY_MARKERS_KO
    return _AGGRESSIVE_BUY_MARKERS_ZH


def _contains_any(
    text: str,
    markers: tuple[str, ...],
    *,
    language: str = "zh",
    require_negation: bool = False,
) -> bool:
    lowered = text.lower()
    negation_hints = _negation_hints_for(language)
    for marker in markers:
        marker_lower = marker.lower()
        marker_pos = 0
        while True:
            marker_pos = lowered.find(marker_lower, marker_pos)
            if marker_pos == -1:
                break
            context = lowered[max(0, marker_pos - _NEGATION_LOOKBACK):marker_pos]
            has_negation = _contains_negation_near_marker(context, negation_hints)
            if require_negation:
                if has_negation:
                    return True
            elif not has_negation:
                return True
            marker_pos += len(marker_lower)
    return False


def _contains_negation_near_marker(context: str, negation_hints: tuple[str, ...]) -> bool:
    separators = ("，", ",", "。", "；", ";", "：", ":", "？", "!", "！", "）", ")", "（", "(")
    tail = context
    sep_pos = -1
    for separator in separators:
        candidate = context.rfind(separator)
        if candidate > sep_pos:
            sep_pos = candidate
    if sep_pos >= 0:
        tail = context[sep_pos + 1 :]
    return any(hint in tail for hint in negation_hints)
