# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - AI分析层
===================================

职责：
1. 封装 LLM 调用逻辑（通过 LiteLLM 统一调用 Gemini/Anthropic/OpenAI 等）
2. 结合技术面和消息面生成分析报告
3. 解析 LLM 响应为结构化 AnalysisResult
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Tuple, Callable

import litellm
from json_repair import repair_json
from litellm import Router

from src.agent.llm_adapter import (
    get_thinking_extra_body,
    resolve_fallback_litellm_wire_models,
    register_fallback_model_pricing,
)
from src.agent.provider_trace import resolved_model_provider_identity
from src.agent.skills.defaults import get_default_trading_skill_policy
from src.config import (
    Config,
    extra_litellm_params,
    get_api_keys_for_model,
    get_config,
    get_configured_llm_models,
    resolve_news_window_days,
)
from src.llm.hermes import (
    HERMES_CHANNEL_NAME,
    build_hermes_redaction_values,
    canonicalize_hermes_model_ref,
    filter_non_hermes_deployments,
    hermes_blocked_route_candidates,
    is_masked_secret_placeholder,
    open_hermes_no_proxy_client,
    route_deployment_origins,
    route_has_hermes,
    sanitize_hermes_error_text,
)
from src.llm.generation_params import apply_litellm_generation_params
from src.llm.errors import call_litellm_with_param_recovery
from src.llm.backend_registry import (
    LOCAL_CLI_GENERATION_BACKEND_IDS,
    LITELLM_BACKEND_ID,
    resolve_generation_backend_id,
    resolve_generation_fallback_backend_id,
)
from src.llm.backend_factory import create_generation_backend
from src.llm.generation_backend import (
    GenerationBackend,
    GenerationError,
    GenerationErrorCode,
)
from src.llm.usage import (
    attach_legacy_message_stability_audit,
    attach_message_hmacs,
    extract_usage_payload,
    normalize_litellm_usage,
    should_persist_usage_telemetry,
)
from src.llm.local_cli_backend import redact_diagnostic_text
from src.llm.provider_cache import (
    apply_prompt_cache_hints,
    build_provider_cache_route_context,
    filter_prompt_cache_telemetry,
)
from src.llm.response_content import strip_leading_think_wrapper
from src.storage import persist_llm_usage
from src.data.stock_mapping import STOCK_NAME_MAP
from src.report_language import (
    get_signal_level,
    get_no_data_text,
    get_placeholder_text,
    get_unknown_text,
    get_chip_unavailable_text,
    infer_decision_type_from_advice,
    is_chip_placeholder_value,
    localize_chip_health,
    localize_confidence_level,
    localize_operation_advice,
    localize_trend_prediction,
    normalize_report_language,
)
from src.schemas.decision_action import build_action_fields
from src.schemas.decision_scale import (
    CANONICAL_DECISION_SCALE_PROMPT,
    score_band_metadata,
)
from src.schemas.report_schema import AnalysisReportSchema
from src.market_context import detect_market, get_market_role, get_market_guidelines
from src.services.daily_market_context import format_daily_market_context_prompt_section
from src.market_phase_prompt import format_market_phase_prompt_section
from src.market_structure_prompt import format_market_structure_prompt_section

logger = logging.getLogger(__name__)


def _localized_text(language: Any, *, en: str, zh: str, ko: str) -> str:
    """Pick a deterministic fallback string for the report language (zh/en/ko)."""
    normalized = normalize_report_language(language)
    if normalized == "en":
        return en
    if normalized == "ko":
        return ko
    return zh


def _normalize_risk_warning_values(value: Any) -> List[str]:
    """Normalize arbitrary risk_warning values into a flat list of text alerts."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        normalized: List[str] = []
        for item in value:
            normalized.extend(_normalize_risk_warning_values(item))
        return normalized
    if isinstance(value, dict):
        if not value:
            return []
        try:
            dumped = json.dumps(value, ensure_ascii=False)
            text = dumped.strip()
        except (TypeError, ValueError):
            text = str(value).strip()
        return [text] if text else []
    text = str(value).strip()
    return [text] if text else []


def _today_has_realtime_overlay(today: Any) -> bool:
    if not isinstance(today, dict):
        return False
    data_source = today.get("data_source") or today.get("dataSource")
    if isinstance(data_source, str) and data_source.startswith("realtime:"):
        return True
    if today.get("is_partial_bar") is True or today.get("isPartialBar") is True:
        return True
    if today.get("is_estimated") is True or today.get("isEstimated") is True:
        return True
    return bool(today.get("estimated_fields") or today.get("estimatedFields"))


def _today_looks_complete_daily_bar(
    context: Dict[str, Any],
    phase_context: Dict[str, Any],
) -> bool:
    today = context.get("today")
    if (
        not isinstance(today, dict)
        or today.get("close") in (None, "")
        or _today_has_realtime_overlay(today)
    ):
        return False

    effective_date = phase_context.get("effective_daily_bar_date")
    today_date = today.get("date") or today.get("trade_date") or context.get("date")
    if effective_date and today_date and str(today_date) != str(effective_date):
        return False
    return True


def _phase_aware_quote_labels(context: Dict[str, Any]) -> Tuple[str, str]:
    """Choose Chinese quote-table labels that do not conflict with phase context."""
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return "Today's quote", "Close"

    phase = str(phase_context.get("phase") or "").strip()
    if phase in {"premarket", "non_trading"}:
        today = context.get("today")
        if _today_looks_complete_daily_bar(context, phase_context):
            return "Last completed session quote", "Last completed session close"
        if _today_has_realtime_overlay(today):
            return "Latest quote", "Real-time estimated price"
        if isinstance(today, dict) and today.get("close") not in (None, ""):
            return "Latest quote", "Latest price"
        return "Today's quote", "Close"

    if (
        phase in {"intraday", "lunch_break", "closing_auction"}
        and phase_context.get("is_partial_bar") is True
    ):
        return "Latest quote", "Intraday estimated price"

    return "Today's quote", "Close"


def _should_hide_regular_session_ohlc(context: Dict[str, Any]) -> bool:
    phase_context = context.get("market_phase_context")
    if not isinstance(phase_context, dict):
        return False

    phase = str(phase_context.get("phase") or "").strip()
    return phase in {"premarket", "non_trading"} and not _today_looks_complete_daily_bar(
        context,
        phase_context,
    )


def _legacy_market_group(stock_code: Any) -> str:
    code = str(stock_code or "").strip()
    if not code or code.lower() == "unknown":
        return "unknown"
    market = detect_market(code)
    return market if market in {"cn", "hk", "us"} else "unknown"


def _legacy_audit_marker_specs(
    context: Dict[str, Any],
    *,
    code: str,
    stock_name: str,
    report_language: str,
    news_context: Optional[str],
    analysis_context_pack_summary: Optional[str],
) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []

    def add(marker_name: str, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text:
            return
        markers.append(
            {
                "marker_name": marker_name,
                "message_role": "user",
                "text": text,
            }
        )

    add("stock_code", code)
    add("stock_name", stock_name)
    add("analysis_date", context.get("date"))
    add("market_phase", "## Market Phase Context" if report_language in ("en", "ko") else "## 市场阶段上下文")
    add("daily_market_context", "## Daily Market Context" if report_language in ("en", "ko") else "## 大盘环境摘要")
    add("market_structure_context", "## Market Structure Context" if report_language in ("en", "ko") else "## 市场结构上下文")
    add("analysis_context_pack", analysis_context_pack_summary)
    # NOTE: the "quote" and "news_context" section headers rendered by _format_prompt
    # (below) are intentionally unconditional English regardless of report_language,
    # same as the other structural/instructional prompt text; these marker strings
    # must match that real prompt text exactly so offset lookups still find them.
    add("quote", "## 📈 Technical Data")
    add("news_context", "## 📰 News Intelligence" if news_context else None)
    return markers


class _LiteLLMStreamError(RuntimeError):
    """Internal error wrapper that records whether any text was streamed."""

    def __init__(self, message: str, *, partial_received: bool = False):
        super().__init__(message)
        self.partial_received = partial_received


class _AllModelsFailedError(Exception):
    """Raised when every model in the fallback chain fails.

    This includes both LLM call errors and JSON parse errors (when a
    ``response_validator`` is provided to :meth:`GeminiAnalyzer._call_litellm`).

    The ``last_response_text`` attribute holds the raw text from the last model
    that *did* return a response (but whose JSON could not be validated), so
    callers can still attempt a best-effort text fallback.

    ``last_model`` and ``last_usage`` record the model name and token usage
    from the last attempt so callers can persist usage even on fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        last_response_text: Optional[str] = None,
        last_model: Optional[str] = None,
        last_usage: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.last_response_text = last_response_text
        self.last_model = last_model
        self.last_usage = last_usage or {}


from src.utils.data_processing import normalize_report_signal_attribution


def check_content_integrity(
    result: "AnalysisResult",
    *,
    require_phase_decision: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Check mandatory fields for report content integrity.
    Returns (pass, missing_fields). Module-level for use by pipeline (agent weak mode).

    Note:
    - Required fields: missing → pass=False, added to missing_fields
    - Optional fields (e.g., signal_attribution): missing → pass=True and are not added to missing_fields
    """
    missing: List[str] = []

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    if result.sentiment_score is None:
        missing.append("sentiment_score")
    advice = result.operation_advice
    if not advice or not isinstance(advice, str) or _is_blank_text(advice):
        missing.append("operation_advice")
    summary = result.analysis_summary
    if not summary or not isinstance(summary, str) or _is_blank_text(summary):
        missing.append("analysis_summary")
    dash = result.dashboard if isinstance(result.dashboard, dict) else {}
    core = dash.get("core_conclusion")
    core = core if isinstance(core, dict) else {}
    if _is_blank_text(core.get("one_sentence")):
        missing.append("dashboard.core_conclusion.one_sentence")
    intel = dash.get("intelligence")
    intel = intel if isinstance(intel, dict) else None
    if intel is None or _is_invalid_risk_alerts(intel.get("risk_alerts")):
        missing.append("dashboard.intelligence.risk_alerts")
    if result.decision_type in ("buy", "hold"):
        battle = dash.get("battle_plan")
        battle = battle if isinstance(battle, dict) else {}
        sp = battle.get("sniper_points")
        sp = sp if isinstance(sp, dict) else {}
        stop_loss = sp.get("stop_loss")
        if _is_invalid_stop_loss(stop_loss):
            missing.append("dashboard.battle_plan.sniper_points.stop_loss")
    if require_phase_decision:
        phase_decision = dash.get("phase_decision")
        phase_decision = phase_decision if isinstance(phase_decision, dict) else {}
        if not isinstance(phase_decision.get("phase_context"), dict):
            missing.append("dashboard.phase_decision.phase_context")
        if _is_blank_text(phase_decision.get("action_window")):
            missing.append("dashboard.phase_decision.action_window")
        if _is_blank_text(phase_decision.get("immediate_action")):
            missing.append("dashboard.phase_decision.immediate_action")
        if not isinstance(phase_decision.get("watch_conditions"), list):
            missing.append("dashboard.phase_decision.watch_conditions")
        if _is_blank_text(phase_decision.get("next_check_time")):
            missing.append("dashboard.phase_decision.next_check_time")
        if _is_blank_text(phase_decision.get("confidence_reason")):
            missing.append("dashboard.phase_decision.confidence_reason")
        if not isinstance(phase_decision.get("data_limitations"), list):
            missing.append("dashboard.phase_decision.data_limitations")
    return len(missing) == 0, missing


def apply_placeholder_fill(result: "AnalysisResult", missing_fields: List[str]) -> None:
    """Fill missing mandatory fields with placeholders (in-place). Module-level for pipeline."""

    def _is_blank_text(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return True

    def _is_invalid_risk_alerts(value: Any) -> bool:
        return not isinstance(value, list)

    def _is_invalid_stop_loss(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, (list, tuple, dict)):
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    report_language = normalize_report_language(getattr(result, "report_language", "zh"))
    placeholder = get_placeholder_text(report_language)
    phase_decision_placeholders = {
        "dashboard.phase_decision.action_window": _localized_text(
            report_language,
            en="Model did not provide a phase action window",
            zh="模型未提供阶段化行动窗口",
            ko="모델이 단계별 행동 구간을 제공하지 않았습니다",
        ),
        "dashboard.phase_decision.immediate_action": _localized_text(
            report_language,
            en="Model did not provide a phase-aware immediate action",
            zh="模型未提供阶段化即时动作",
            ko="모델이 단계 인식 즉시 동작을 제공하지 않았습니다",
        ),
        "dashboard.phase_decision.next_check_time": _localized_text(
            report_language,
            en="Model did not provide a next check point",
            zh="模型未提供下一次检查点",
            ko="모델이 다음 점검 시점을 제공하지 않았습니다",
        ),
        "dashboard.phase_decision.confidence_reason": _localized_text(
            report_language,
            en="Model did not provide a phase confidence rationale",
            zh="模型未提供阶段化置信度理由",
            ko="모델이 단계별 신뢰도 근거를 제공하지 않았습니다",
        ),
    }
    for field in missing_fields:
        if field == "sentiment_score":
            result.sentiment_score = 50
        elif field == "operation_advice":
            if _is_blank_text(result.operation_advice):
                result.operation_advice = placeholder
        elif field == "analysis_summary":
            if _is_blank_text(result.analysis_summary):
                result.analysis_summary = placeholder
        elif field == "dashboard.core_conclusion.one_sentence":
            if not result.dashboard:
                result.dashboard = {}
            core = result.dashboard.get("core_conclusion")
            if not isinstance(core, dict):
                core = {}
                result.dashboard["core_conclusion"] = core
            fallback_sentence = (
                result.analysis_summary
                or result.operation_advice
                or placeholder
            )
            if _is_blank_text(core.get("one_sentence")):
                result.dashboard["core_conclusion"]["one_sentence"] = fallback_sentence
        elif field == "dashboard.intelligence.risk_alerts":
            if not result.dashboard:
                result.dashboard = {}
            intelligence = result.dashboard.get("intelligence")
            if not isinstance(intelligence, dict):
                intelligence = {}
                result.dashboard["intelligence"] = intelligence
            if _is_invalid_risk_alerts(intelligence.get("risk_alerts")):
                risk_warning_values = _normalize_risk_warning_values(result.risk_warning)
                intelligence["risk_alerts"] = risk_warning_values
        elif field == "dashboard.battle_plan.sniper_points.stop_loss":
            if not result.dashboard:
                result.dashboard = {}
            battle_plan = result.dashboard.get("battle_plan")
            if not isinstance(battle_plan, dict):
                battle_plan = {}
                result.dashboard["battle_plan"] = battle_plan
            sniper_points = battle_plan.get("sniper_points")
            if not isinstance(sniper_points, dict):
                sniper_points = {}
                battle_plan["sniper_points"] = sniper_points
            if _is_invalid_stop_loss(sniper_points.get("stop_loss")):
                sniper_points["stop_loss"] = placeholder
        elif field.startswith("dashboard.phase_decision."):
            if not result.dashboard:
                result.dashboard = {}
            phase_decision = result.dashboard.get("phase_decision")
            if not isinstance(phase_decision, dict):
                phase_decision = {}
                result.dashboard["phase_decision"] = phase_decision
            if field == "dashboard.phase_decision.phase_context":
                if not isinstance(phase_decision.get("phase_context"), dict):
                    phase_decision["phase_context"] = {}
            elif field == "dashboard.phase_decision.watch_conditions":
                if not isinstance(phase_decision.get("watch_conditions"), list):
                    phase_decision["watch_conditions"] = []
            elif field == "dashboard.phase_decision.data_limitations":
                if not isinstance(phase_decision.get("data_limitations"), list):
                    phase_decision["data_limitations"] = []
            elif field in phase_decision_placeholders:
                if _is_blank_text(phase_decision.get(field.rsplit(".", 1)[-1])):
                    phase_decision[field.rsplit(".", 1)[-1]] = phase_decision_placeholders[field]


# ---------- chip_structure fallback (Issue #589) ----------

_CHIP_KEYS: tuple = ("profit_ratio", "avg_cost", "concentration", "chip_health")


def _is_value_placeholder(v: Any) -> bool:
    """True if value is empty or placeholder (N/A, 数据缺失, etc.)."""
    return is_chip_placeholder_value(v)


_RISK_WARNING_PLACEHOLDER_TEXTS = {
    "",
    "n/a",
    "na",
    "none",
    "null",
    "unknown",
    "tbd",
    "暂无",
    "待补充",
    "数据缺失",
    "未知",
    "无",
}

_STRUCTURAL_RISK_PHRASE_HINTS = (
    "重大利空",
    "重大风险",
    "关键风险",
    "减持",
    "高位减持",
    "退市",
    "退市风险",
    "停牌",
    "重大问询",
    "处罚",
    "限售",
    "违规",
    "违规风险",
    "诉讼",
    "问询",
    "监管",
    "财务",
    "审计",
    "爆雷",
    "暴雷",
    "违约",
    "违约风险",
    "流动性危机",
    "债务",
    "清算",
    "破产",
    "重大变脸",
    "major risk",
    "material adverse",
    "suspension",
    "delisting",
    "regulatory",
    "downgrade",
    "liquidity",
    "default",
)

_CAPITAL_FLOW_UNAVAILABLE_STATUS = {
    "not_supported",
    "not supported",
    "unsupported",
    "unavailable",
    "not_available",
    "not available",
    "none",
    "na",
    "n/a",
    "null",
    "missing",
}


def _is_meaningful_text(value: Any) -> bool:
    text = str(value).strip() if value is not None else ""
    if not text:
        return False
    lowered = text.strip().lower()
    return lowered not in _RISK_WARNING_PLACEHOLDER_TEXTS


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Safely convert to float; return default on failure. Private helper for chip fill."""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            return default if math.isnan(float(v)) else float(v)
        except (ValueError, TypeError):
            return default
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _coerce_chip_metric(v: Any) -> Optional[float]:
    """Convert chip metrics while preserving the distinction between missing and zero."""
    if v is None:
        return None
    try:
        numeric = float(v)
    except (TypeError, ValueError):
        try:
            numeric = float(str(v).strip())
        except (TypeError, ValueError):
            return None
    return None if math.isnan(numeric) else numeric


_BULLISH_TREND_HINTS: Tuple[str, ...] = (
    "多头排列",
    "持续上涨",
    "趋势向上",
    "上升趋势",
    "向上发散",
    "bullish",
    "uptrend",
)
_WEAK_BULLISH_TREND_HINTS: Tuple[str, ...] = ("弱势多头", "weak bullish")
_BEARISH_TREND_HINTS: Tuple[str, ...] = (
    "空头排列",
    "持续下跌",
    "趋势向下",
    "下降趋势",
    "向下发散",
    "bearish",
    "downtrend",
)
_WEAK_BEARISH_TREND_HINTS: Tuple[str, ...] = ("弱势空头", "weak bearish")
_NEGATION_TOKENS: Tuple[str, ...] = (
    "不是",
    "并非",
    "并未",
    "没有",
    "尚不",
    "尚未",
    "未",
    "无",
    "不属",
    "非",
    "not ",
    "no ",
)
_NEGATION_BREAK_CHARS: Tuple[str, ...] = (",", ".", ";", ":", "!", "?", "，", "。", "；", "：", "！", "？", "\n")
_NEGATION_LOOKBACK_CHARS = 16
_NEGATION_MAX_GAP_CHARS = 8
_NEGATION_SCOPE_BREAK_TOKENS: Tuple[str, ...] = (
    "而是",
    "但是",
    "但",
    "反而",
    "反倒",
    "转为",
    "转成",
    "改为",
    "改成",
    " but ",
    " instead ",
    " rather ",
)
_SINGLE_CHAR_NEGATION_GAP_PREFIXES: Tuple[str, ...] = (
    "形成",
    "出现",
    "进入",
    "转为",
    "转成",
    "构成",
    "呈现",
    "显示",
    "属于",
    "是",
    "有",
    "能",
    "见",
    "站",
    "守",
    "破",
)


def _normalize_prompt_reason_items(items: Any) -> List[str]:
    """Normalize prompt reason/risk items into a clean string list."""
    if not isinstance(items, list):
        return []
    normalized: List[str] = []
    for item in items:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _contains_trend_hint(text: str, hints: Tuple[str, ...]) -> bool:
    """Return True when text contains a non-negated strong trend hint."""
    lowered = text.strip().lower()

    def _has_negation_scope_break(gap: str) -> bool:
        normalized_gap = gap.lower()
        for token in _NEGATION_SCOPE_BREAK_TOKENS:
            token_index = normalized_gap.find(token)
            if token_index > 0:
                return True
        return False

    def _is_valid_negation_gap(token: str, gap: str) -> bool:
        if not gap:
            return True
        if token not in {"未", "无", "非"}:
            return True
        return any(gap.startswith(prefix) for prefix in _SINGLE_CHAR_NEGATION_GAP_PREFIXES)

    def _is_negated_match(index: int) -> bool:
        prefix = lowered[max(0, index - _NEGATION_LOOKBACK_CHARS):index]
        for token in _NEGATION_TOKENS:
            token_index = prefix.rfind(token)
            if token_index < 0:
                continue
            gap = prefix[token_index + len(token):]
            if any(char in gap for char in _NEGATION_BREAK_CHARS):
                continue
            stripped_gap = gap.strip()
            if len(stripped_gap) > _NEGATION_MAX_GAP_CHARS:
                continue
            if _has_negation_scope_break(stripped_gap):
                continue
            if not _is_valid_negation_gap(token, stripped_gap):
                continue
            return True
        return False

    for hint in hints:
        keyword = hint.lower()
        start = 0
        while True:
            index = lowered.find(keyword, start)
            if index < 0:
                break
            if not _is_negated_match(index):
                return True
            start = index + len(keyword)
    return False


def _infer_trend_direction(trend: Dict[str, Any]) -> str:
    """Infer the final trend direction from trend_status and ma_alignment."""
    combined = " ".join(
        str(trend.get(key, "")).strip()
        for key in ("trend_status", "ma_alignment")
        if str(trend.get(key, "")).strip()
    )
    if not combined:
        return "neutral"
    lowered = combined.lower()
    normalized = lowered.replace(" ", "")
    has_bullish = (
        _contains_trend_hint(combined, _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS)
        or "ma5>ma10>ma20" in normalized
        or (
            "ma5>ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≤ma20", "ma10<=ma20"))
        )
    )
    has_bearish = (
        _contains_trend_hint(combined, _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS)
        or "ma5<ma10<ma20" in normalized
        or (
            "ma5<ma10" in normalized
            and any(pattern in normalized for pattern in ("ma10≥ma20", "ma10>=ma20"))
        )
    )
    if has_bullish and not has_bearish:
        return "bullish"
    if has_bearish and not has_bullish:
        return "bearish"
    return "neutral"


def _filter_conflicting_trend_items(items: List[str], conflict_hints: Tuple[str, ...]) -> List[str]:
    """Drop reasons that directly conflict with the final trend direction."""
    return [item for item in items if not _contains_trend_hint(item, conflict_hints)]


# Display-only English labels for src.stock_analyzer's raw Chinese Enum.value strings
# (TrendStatus/VolumeStatus/BuySignal). These translate prompt TEXT only; the underlying
# enum values in src/stock_analyzer.py, and any downstream keyword matching against those
# exact Chinese literals, are left untouched on purpose.
_TREND_STATUS_PROMPT_EN = {
    "强势多头": "Strong bullish alignment",
    "多头排列": "Bullish alignment",
    "弱势多头": "Weak bullish",
    "盘整": "Consolidation",
    "弱势空头": "Weak bearish",
    "空头排列": "Bearish alignment",
    "强势空头": "Strong bearish alignment",
}
_VOLUME_STATUS_PROMPT_EN = {
    "放量上涨": "Rising volume, price up",
    "放量下跌": "Rising volume, price down",
    "缩量上涨": "Falling volume, price up",
    "缩量回调": "Falling volume, pullback",
    "量能正常": "Normal volume",
}
_BUY_SIGNAL_PROMPT_EN = {
    "强烈买入": "Strong Buy",
    "买入": "Buy",
    "持有": "Hold",
    "观望": "Watch",
    "卖出": "Sell",
    "强烈卖出": "Strong Sell",
}


def _localize_prompt_display_value(value: Any, table: Dict[str, str], report_language: str) -> Any:
    """Translate a raw Chinese indicator label for prompt display when report_language != zh."""
    if report_language == "zh":
        return value
    text = str(value) if value is not None else value
    if isinstance(text, str) and text in table:
        return table[text]
    return value


def _localize_prompt_reason_items(items: List[str], report_language: str) -> List[str]:
    """Replace embedded raw Chinese trend-status labels inside reason/risk sentences."""
    if report_language == "zh" or not items:
        return items
    localized = []
    for item in items:
        text = item
        for zh_label, en_label in _TREND_STATUS_PROMPT_EN.items():
            if zh_label in text:
                text = text.replace(zh_label, en_label)
        localized.append(text)
    return localized


def _sanitize_trend_analysis_for_prompt(
    trend: Any,
    *,
    volume_change_ratio: Any = None,
    report_language: str = "zh",
) -> Dict[str, Any]:
    """Clean prompt-only trend hints on a derived copy without touching runtime/provider config."""
    trend_dict = dict(trend) if isinstance(trend, dict) else {}
    signal_reasons = _normalize_prompt_reason_items(trend_dict.get("signal_reasons"))
    risk_factors = _normalize_prompt_reason_items(trend_dict.get("risk_factors"))
    signal_reasons = _localize_prompt_reason_items(signal_reasons, report_language)
    risk_factors = _localize_prompt_reason_items(risk_factors, report_language)
    prompt_notes: List[str] = []
    trend_direction = _infer_trend_direction(trend_dict)

    if trend_direction == "bearish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BULLISH_TREND_HINTS + _WEAK_BULLISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("The current technical structure is bearish; bullish structural reasons that directly conflict with the bearish primary verdict were removed.")
        signal_reasons = filtered_signal_reasons
        prompt_notes.append(
            "If news, earnings, or policy catalysts lean bullish, describe them only as \"event leads, technicals pending confirmation\" or \"fundamentals lean bullish, but technicals have not confirmed\"; never present them as a definitive buy point."
        )
    elif trend_direction == "bullish":
        filtered_signal_reasons = _filter_conflicting_trend_items(
            signal_reasons,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_signal_reasons) != len(signal_reasons):
            prompt_notes.append("The current technical structure is bullish; bearish structural reasons that directly conflict with the bullish primary verdict were removed.")
        signal_reasons = filtered_signal_reasons
        filtered_risk_factors = _filter_conflicting_trend_items(
            risk_factors,
            _BEARISH_TREND_HINTS + _WEAK_BEARISH_TREND_HINTS,
        )
        if len(filtered_risk_factors) != len(risk_factors):
            prompt_notes.append("The current technical structure is bullish; bearish structural risk statements that directly conflict with the bullish primary verdict were removed.")
        risk_factors = filtered_risk_factors

    parsed_volume_change = _safe_float(volume_change_ratio, default=math.nan)
    if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
        prompt_notes.append(
            f"Volume changed about {parsed_volume_change:.2f}x versus the previous day, possibly due to abnormal data or a one-off spike; the volume signal must be down-weighted and must not be treated mechanically as strong confirmation."
        )

    trend_dict["signal_reasons"] = signal_reasons
    trend_dict["risk_factors"] = risk_factors
    trend_dict["prompt_consistency_notes"] = prompt_notes
    trend_dict["prompt_trend_direction"] = trend_direction
    if "trend_status" in trend_dict:
        trend_dict["trend_status"] = _localize_prompt_display_value(
            trend_dict["trend_status"], _TREND_STATUS_PROMPT_EN, report_language
        )
    if "volume_status" in trend_dict:
        trend_dict["volume_status"] = _localize_prompt_display_value(
            trend_dict["volume_status"], _VOLUME_STATUS_PROMPT_EN, report_language
        )
    if "buy_signal" in trend_dict:
        trend_dict["buy_signal"] = _localize_prompt_display_value(
            trend_dict["buy_signal"], _BUY_SIGNAL_PROMPT_EN, report_language
        )
    return trend_dict


def _derive_chip_health(profit_ratio: float, concentration_90: float, language: str = "zh") -> str:
    """Derive chip_health from profit_ratio and concentration_90."""
    if profit_ratio >= 0.9:
        return localize_chip_health("caution", language)  # very high profit ratio
    if concentration_90 >= 0.25:
        return localize_chip_health("caution", language)  # dispersed chips
    if concentration_90 < 0.15 and 0.3 <= profit_ratio < 0.9:
        return localize_chip_health("healthy", language)  # concentrated with moderate profit ratio
    return localize_chip_health("average", language)


def _build_chip_structure_from_data(chip_data: Any, language: str = "zh") -> Dict[str, Any]:
    """Build chip_structure dict from ChipDistribution or dict."""
    if hasattr(chip_data, "profit_ratio"):
        pr = _safe_float(chip_data.profit_ratio)
        ac = chip_data.avg_cost
        c90 = _safe_float(chip_data.concentration_90)
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        pr = _safe_float(d.get("profit_ratio"))
        ac = d.get("avg_cost")
        c90 = _safe_float(d.get("concentration_90"))
    chip_health = _derive_chip_health(pr, c90, language=language)
    return {
        "profit_ratio": f"{pr:.1%}",
        "avg_cost": ac if (ac is not None and _safe_float(ac) != 0.0) else "N/A",
        "concentration": f"{c90:.2%}",
        "chip_health": chip_health,
    }


def _has_meaningful_chip_data(chip_data: Any) -> bool:
    """Return True when chip data has the core metrics required for reporting."""
    if not chip_data:
        return False
    if hasattr(chip_data, "avg_cost"):
        avg_cost = _coerce_chip_metric(getattr(chip_data, "avg_cost", None))
        concentration_90 = _coerce_chip_metric(getattr(chip_data, "concentration_90", None))
        concentration_70 = _coerce_chip_metric(getattr(chip_data, "concentration_70", None))
    else:
        d = chip_data if isinstance(chip_data, dict) else {}
        avg_cost = _coerce_chip_metric(d.get("avg_cost"))
        concentration_90_value = d.get("concentration_90")
        if concentration_90_value is None:
            concentration_90_value = d.get("concentration")
        concentration_90 = _coerce_chip_metric(concentration_90_value)
        concentration_70 = _coerce_chip_metric(d.get("concentration_70"))
    return (
        avg_cost is not None
        and avg_cost > 0
        and (
            (concentration_90 is not None and concentration_90 >= 0)
            or (concentration_70 is not None and concentration_70 >= 0)
        )
    )


def _mark_chip_structure_unavailable(result: "AnalysisResult", language: str) -> None:
    if not result or not isinstance(result.dashboard, dict):
        return
    data_perspective = result.dashboard.get("data_perspective")
    if not isinstance(data_perspective, dict):
        return
    data_perspective["chip_structure"] = {}
    data_perspective["chip_unavailable_reason"] = get_chip_unavailable_text(language)


def normalize_chip_structure_availability(result: "AnalysisResult", chip_data: Any) -> None:
    """Fill valid chip metrics or collapse placeholder-only chip fields to one fallback line."""
    if not result:
        return
    language = getattr(result, "report_language", "zh")
    if _has_meaningful_chip_data(chip_data):
        fill_chip_structure_if_needed(result, chip_data)
        return
    _mark_chip_structure_unavailable(result, language)


def fill_chip_structure_if_needed(result: "AnalysisResult", chip_data: Any) -> None:
    """When chip_data exists, fill chip_structure placeholder fields from chip_data (in-place)."""
    if not result or not _has_meaningful_chip_data(chip_data):
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        # Use `or {}` rather than setdefault so that an explicit `null` from LLM is also replaced
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        cs = dp.get("chip_structure") or {}
        filled = _build_chip_structure_from_data(
            chip_data,
            language=getattr(result, "report_language", "zh"),
        )
        # Start from a copy of cs to preserve any extra keys the LLM may have added
        merged = dict(cs)
        for k in _CHIP_KEYS:
            if _is_value_placeholder(merged.get(k)):
                merged[k] = filled[k]
        if merged != cs:
            dp["chip_structure"] = merged
            logger.info("[chip_structure] Filled placeholder chip fields from data source (Issue #589)")
    except Exception as e:
        logger.warning("[chip_structure] Fill failed, skipping: %s", e)


_PRICE_POS_KEYS = ("ma5", "ma10", "ma20", "bias_ma5", "bias_status", "current_price", "support_level", "resistance_level")


def fill_price_position_if_needed(
    result: "AnalysisResult",
    trend_result: Any = None,
    realtime_quote: Any = None,
) -> None:
    """Fill missing price_position fields from trend_result / realtime data (in-place)."""
    if not result:
        return
    try:
        if not result.dashboard:
            result.dashboard = {}
        dash = result.dashboard
        dp = dash.get("data_perspective") or {}
        dash["data_perspective"] = dp
        pp = dp.get("price_position") or {}

        computed: Dict[str, Any] = {}
        if trend_result:
            tr = trend_result if isinstance(trend_result, dict) else (
                trend_result.__dict__ if hasattr(trend_result, "__dict__") else {}
            )
            computed["ma5"] = tr.get("ma5")
            computed["ma10"] = tr.get("ma10")
            computed["ma20"] = tr.get("ma20")
            computed["bias_ma5"] = tr.get("bias_ma5")
            computed["current_price"] = tr.get("current_price")
            support_levels = tr.get("support_levels") or []
            resistance_levels = tr.get("resistance_levels") or []
            if support_levels:
                computed["support_level"] = support_levels[0]
            if resistance_levels:
                computed["resistance_level"] = resistance_levels[0]
        if realtime_quote:
            rq = realtime_quote if isinstance(realtime_quote, dict) else (
                realtime_quote.to_dict() if hasattr(realtime_quote, "to_dict") else {}
            )
            if _is_value_placeholder(computed.get("current_price")):
                computed["current_price"] = rq.get("price")

        filled = False
        for k in _PRICE_POS_KEYS:
            if _is_value_placeholder(pp.get(k)) and not _is_value_placeholder(computed.get(k)):
                pp[k] = computed[k]
                filled = True
        if filled:
            dp["price_position"] = pp
            logger.info("[price_position] Filled placeholder fields from computed data")
    except Exception as e:
        logger.warning("[price_position] Fill failed, skipping: %s", e)


def stabilize_decision_with_structure(
    result: "AnalysisResult",
    trend_result: Any = None,
    fundamental_context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Calibrate aggressive buy/sell advice with price levels and capital flow.

    The LLM can overreact to one-day price movement.  This guard keeps the
    public `decision_type` enum stable while allowing richer neutral wording
    such as 震荡/洗盘观察 when support, resistance, and fund flow do not confirm
    an immediate buy/sell action.
    """
    if not result:
        return

    try:
        language = normalize_report_language(getattr(result, "report_language", "zh"))
        dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
        data_perspective = dashboard.get("data_perspective") if isinstance(dashboard, dict) else {}
        if not isinstance(data_perspective, dict):
            data_perspective = {}
        price_position = data_perspective.get("price_position")
        if not isinstance(price_position, dict):
            price_position = {}

        trend_dict = _as_dict_for_decision_guard(trend_result)
        current_price = _first_numeric_value(
            getattr(result, "current_price", None),
            price_position.get("current_price"),
            trend_dict.get("current_price"),
        )
        support = _first_numeric_value(
            price_position.get("support_level"),
            _first_list_value(trend_dict.get("support_levels")),
        )
        resistance = _first_numeric_value(
            price_position.get("resistance_level"),
            _first_list_value(trend_dict.get("resistance_levels")),
        )
        decision_type = infer_decision_type_from_advice(
            getattr(result, "decision_type", ""),
            default=getattr(result, "decision_type", "hold") or "hold",
        )
        decision_type = decision_type if decision_type in {"buy", "hold", "sell"} else "hold"
        advice_decision_type = infer_decision_type_from_advice(
            getattr(result, "operation_advice", ""),
            default="",
        )

        flow_bias, flow_reason = _capital_flow_bias_with_status(fundamental_context)
        flow_unavailable = flow_bias == "unavailable"
        flow_market_unsupported = flow_unavailable and _is_capital_flow_market_unsupported(flow_reason)
        if flow_unavailable and isinstance(fundamental_context, dict) and "capital_flow" in fundamental_context:
            # 资金流缺失属于"证据缺失"而非"证据反对"：只记录元数据，不否决买卖结论。
            # 美股/港股/台股等市场资金流数据源结构性不支持时，必须允许正常输出 buy/sell。
            _set_decision_stability_unavailable(
                result,
                language,
                current_price=current_price,
                support=support,
                resistance=resistance,
                flow_status=flow_reason,
                market_unsupported=flow_market_unsupported,
            )

        if current_price is None:
            return

        broke_support = support is not None and current_price < support * 0.985
        near_support = support is not None and not broke_support and current_price <= support * 1.03
        breakout = resistance is not None and current_price > resistance * 1.01
        near_resistance = (
            resistance is not None
            and not breakout
            and current_price >= resistance * 0.97
        )
        mid_range = (
            support is not None
            and resistance is not None
            and support * 1.03 < current_price < resistance * 0.97
        )

        has_significant_risk = _has_structural_risk_alert(result)
        volume_confirmed = _volume_confirmation(result)

        if decision_type == "buy":
            if near_resistance and flow_bias in {"neutral", "outflow"}:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_near_resistance",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "outflow" and not breakout:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="buy_with_outflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "sell":
            if near_support and flow_bias in {"neutral", "inflow"} and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="sell_near_support",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif flow_bias == "inflow" and not broke_support and not has_significant_risk:
                _downgrade_to_structural_hold(
                    result,
                    language,
                    advice_key="hold",
                    reason_key="sell_with_inflow",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        elif decision_type == "hold":
            change_pct = _first_numeric_value(getattr(result, "change_pct", None))
            # 对称升级路径：结构确认时允许 hold→buy / hold→sell（与降级分支使用同一批结构信号）。
            promote_buy = (
                breakout
                and volume_confirmed
                and not has_significant_risk
                and (flow_bias == "inflow" or flow_market_unsupported)
                and advice_decision_type != "sell"
            )
            promote_sell = (
                broke_support
                and flow_bias != "inflow"
                and (flow_bias == "outflow" or volume_confirmed)
                and advice_decision_type != "buy"
            )
            if promote_buy:
                _promote_hold_to_structural_decision(
                    result,
                    language,
                    target="buy",
                    reason_key="hold_breakout_promotion",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif promote_sell:
                _promote_hold_to_structural_decision(
                    result,
                    language,
                    target="sell",
                    reason_key="hold_support_break_promotion",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif change_pct is not None and change_pct < 0 and near_support and flow_bias in {"neutral", "inflow"}:
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="shakeout",
                    reason_key="hold_shakeout",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
            elif mid_range and flow_bias == "neutral":
                _set_structural_hold_wording(
                    result,
                    language,
                    advice_key="range",
                    reason_key="hold_mid_range",
                    current_price=current_price,
                    support=support,
                    resistance=resistance,
                    flow_bias=flow_bias,
                )
        _sync_stability_dashboard_fields(result)
    except Exception as exc:
        logger.warning("[decision_stability] skipped: %s", exc)


def _has_structural_risk_alert(result: "AnalysisResult") -> bool:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}

    risk_text = getattr(result, "risk_warning", "")
    if _is_significant_structural_risk(risk_text):
        return True

    intelligence = dashboard.get("intelligence") if isinstance(dashboard, dict) else None
    if isinstance(intelligence, dict):
        risk_alerts = intelligence.get("risk_alerts")
        if isinstance(risk_alerts, str):
            if _is_significant_structural_risk(risk_alerts):
                return True
        elif isinstance(risk_alerts, (list, tuple, set)):
            if any(_is_significant_structural_risk(item) for item in risk_alerts):
                return True

    core_conclusion = dashboard.get("core_conclusion") if isinstance(dashboard, dict) else None
    if isinstance(core_conclusion, dict):
        signal_type = str(core_conclusion.get("signal_type", "")).strip()
        if _is_significant_structural_risk(signal_type):
            return True
    return False


def _is_significant_structural_risk(value: Any) -> bool:
    text = str(value or "").strip()
    if not _is_meaningful_text(text):
        return False

    normalized = text.lower()
    if any(keyword in normalized for keyword in _STRUCTURAL_RISK_PHRASE_HINTS):
        return True

    if "重大" in text and "风险" in normalized:
        return True
    return "major" in normalized and "risk" in normalized


def _sync_stability_dashboard_fields(result: "AnalysisResult") -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["sentiment_score"] = getattr(result, "sentiment_score", None)
    dashboard["operation_advice"] = getattr(result, "operation_advice", None)
    dashboard["decision_type"] = getattr(result, "decision_type", None)


def _as_dict_for_decision_guard(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            converted = value.to_dict()
            return converted if isinstance(converted, dict) else {}
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def _first_list_value(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _coerce_numeric_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _coerce_p_up(value: Any) -> Optional[int]:
    """把模型输出的 p_up 归一化为 0-100 的整数概率（容忍 0-1 小数写法）。"""
    numeric = _coerce_numeric_value(value)
    if numeric is None:
        return None
    if 0 < numeric < 1:
        numeric *= 100
    if numeric < 0 or numeric > 100:
        return None
    return int(round(numeric))


def _first_numeric_value(*values: Any) -> Optional[float]:
    for value in values:
        if isinstance(value, (list, tuple)):
            nested = _first_numeric_value(*value)
            if nested is not None:
                return nested
            continue
        numeric = _coerce_numeric_value(value)
        if numeric is not None:
            return numeric
    return None


def _capital_flow_bias(fundamental_context: Optional[Dict[str, Any]]) -> str:
    return _capital_flow_bias_with_status(fundamental_context)[0]


def _capital_flow_bias_with_status(
    fundamental_context: Optional[Dict[str, Any]],
) -> tuple[str, str]:
    if not isinstance(fundamental_context, dict):
        return "unavailable", "invalid_context"
    block = fundamental_context.get("capital_flow")
    if not isinstance(block, dict):
        return "unavailable", "capital_flow_block_missing"
    status = str(block.get("status") or "").strip().lower()
    normalized_status = status.replace("-", " ").replace("_", " ").strip()
    if normalized_status in _CAPITAL_FLOW_UNAVAILABLE_STATUS or "not supported" in normalized_status:
        return "unavailable", status or "not_supported"
    data = block.get("data") if isinstance(block.get("data"), dict) else block
    stock_flow = data.get("stock_flow") if isinstance(data, dict) else None
    if not isinstance(stock_flow, dict) or not stock_flow:
        return "unavailable", "empty_stock_flow"

    def _flow_direction(value: Optional[float]) -> Optional[str]:
        if value is None or value == 0:
            return None
        return "inflow" if value > 0 else "outflow"

    numeric_values = [
        _coerce_numeric_value(stock_flow.get("main_net_inflow")),
        _coerce_numeric_value(stock_flow.get("inflow_5d")),
        _coerce_numeric_value(stock_flow.get("inflow_10d")),
    ]
    if all(value is None for value in numeric_values):
        return "unavailable", "missing_or_na_flow_fields"

    ordered_signals = [
        _flow_direction(value) for value in numeric_values
    ]
    directions = {signal for signal in ordered_signals if signal is not None}
    if not directions or len(directions) > 1:
        return "neutral", "conflict_or_missing"
    for signal in ordered_signals:
        if signal is not None:
            return signal, "ok"
    return "neutral", "neutral"


def _capital_flow_status_for_stability(reason: str, language: str) -> str:
    normalized = str(reason or "").strip().lower()
    if "not_supported" in normalized or "unsupported" in normalized or "not available" in normalized:
        return "市场资金流服务暂不支持" if language == "zh" else "Capital flow source unsupported"
    if "empty_stock_flow" in normalized or "missing" in normalized:
        return "资金流数据缺失" if language == "zh" else "capital flow data unavailable"
    return "资金流数据不可用" if language == "zh" else "capital flow unavailable"


def _is_capital_flow_market_unsupported(reason: str) -> bool:
    """判断资金流缺失是否属于"该市场数据源结构性不支持"（而非临时抓取失败）。"""
    normalized = str(reason or "").strip().lower().replace("-", " ").replace("_", " ")
    return "not supported" in normalized or "unsupported" in normalized


def _volume_confirmation(result: "AnalysisResult") -> bool:
    """从 dashboard 量能数据判断是否存在放量确认（升级分支的保守门槛）。"""
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    data_perspective = dashboard.get("data_perspective")
    if not isinstance(data_perspective, dict):
        return False
    volume_block = data_perspective.get("volume_analysis")
    if not isinstance(volume_block, dict):
        return False
    ratio = _coerce_numeric_value(volume_block.get("volume_ratio"))
    if ratio is not None:
        return ratio >= 1.5
    status = str(volume_block.get("volume_status") or "")
    lowered = status.lower()
    return "放量" in status or "heavy" in lowered or "surge" in lowered or "high volume" in lowered


def _set_decision_stability_unavailable(
    result: "AnalysisResult",
    language: str,
    *,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_status: str,
    market_unsupported: bool = False,
) -> None:
    """资金流不可用时只做元数据标注：资金流缺失是"证据缺失"，不否决买卖结论。"""
    if market_unsupported:
        reason = (
            "该市场资金流数据源不支持，资金面确认缺失，结论仅基于量价结构"
            if language == "zh"
            else "Capital flow feed is not supported for this market; the verdict relies on price/volume structure without flow confirmation"
        )
    else:
        reason = (
            "资金流数据暂不可用，未使用资金流校准"
            if language == "zh"
            else "Capital flow data temporarily unavailable; stability calibration not applied"
        )
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    dashboard["decision_stability"] = {
        "applied": False,
        "reason": reason,
        "capital_flow_status": _capital_flow_status_for_stability(flow_status, language),
        "capital_flow_market_supported": not market_unsupported,
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": "unavailable",
    }
    _sync_stability_dashboard_fields(result)


def _record_decision_score_calibration(
    result: "AnalysisResult",
    *,
    raw_score: int,
    adjusted_score: int,
    final_action: str,
    guardrail_reason: Optional[str],
) -> None:
    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    calibration = score_band_metadata(raw_score)
    calibration.update(
        {
            "raw_score": raw_score,
            "adjusted_score": adjusted_score,
            "final_action": final_action,
        }
    )
    if guardrail_reason:
        calibration["guardrail_reason"] = guardrail_reason
    dashboard["decision_score_calibration"] = calibration


def _bound_hold_watch_sentiment_score(
    result: "AnalysisResult",
    *,
    reason: Optional[str] = None,
    final_action: str = "watch",
) -> None:
    """记录守门校准元数据，但保留模型原始评分。

    评分是模型观点的一部分：守门逻辑只调整措辞与元数据，不再把
    sentiment_score 压缩到 45-59 区间；守门分歧写入
    ``dashboard.decision_score_calibration``（adjusted_score 与 raw_score 保持一致）。
    """
    try:
        score = int(getattr(result, "sentiment_score", 50))
    except (TypeError, ValueError):
        score = 50
    _record_decision_score_calibration(
        result,
        raw_score=score,
        adjusted_score=score,
        final_action=final_action,
        guardrail_reason=reason,
    )


def _apply_hold_watch_dashboard(
    result: "AnalysisResult",
    language: str,
    *,
    advice: str,
    reason: str,
    current_price: Optional[float],
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
    no_position: str,
    has_position: str,
    capital_flow_status: Optional[str] = None,
) -> None:
    result.operation_advice = advice

    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    core["signal_type"] = "🟡持有观望" if language == "zh" else "🟡 Hold / Watch"
    core["one_sentence"] = f"{advice}：{reason}" if language == "zh" else f"{advice}: {reason}"

    position_advice = core.get("position_advice")
    if not isinstance(position_advice, dict):
        position_advice = {}
        core["position_advice"] = position_advice
    position_advice["no_position"] = no_position
    position_advice["has_position"] = has_position

    stability = {
        "applied": True,
        "reason": reason,
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": flow_bias,
    }
    if capital_flow_status is not None:
        stability["capital_flow_status"] = capital_flow_status
    score_calibration = dashboard.get("decision_score_calibration")
    if isinstance(score_calibration, dict):
        stability["raw_score"] = score_calibration.get("raw_score")
        stability["adjusted_score"] = score_calibration.get("adjusted_score")
        stability["final_action"] = score_calibration.get("final_action")
    dashboard["decision_stability"] = stability

    if reason and reason not in str(result.risk_warning or ""):
        sep = "；" if language == "zh" else "; "
        result.risk_warning = f"{result.risk_warning}{sep}{reason}" if result.risk_warning else reason
    result.buy_reason = reason or result.buy_reason


def _promote_hold_to_structural_decision(
    result: "AnalysisResult",
    language: str,
    *,
    target: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    """结构确认时的对称升级：hold→buy（放量有效突破）/ hold→sell（放量或流出的破位）。

    升级只改方向与措辞并记录 decision_stability 元数据，不改写评分/置信度。
    """
    reason_templates = {
        "zh": {
            "hold_breakout_promotion": "价格放量突破压力位且资金/量价确认，结构支持由观望升级为买入。",
            "hold_support_break_promotion": "价格跌破关键支撑且量能/资金流确认破位，结构支持由观望升级为卖出。",
        },
        "en": {
            "hold_breakout_promotion": "Price broke above resistance with volume (and flow where available) confirmation; structure upgrades the watch call to buy.",
            "hold_support_break_promotion": "Price broke key support with volume/outflow confirmation; structure upgrades the watch call to sell.",
        },
        "ko": {
            "hold_breakout_promotion": "가격이 거래량(및 자금 흐름) 확인과 함께 저항선을 돌파해 관망에서 매수로 승격합니다.",
            "hold_support_break_promotion": "가격이 거래량/자금 유출 확인과 함께 핵심 지지선을 이탈해 관망에서 매도로 승격합니다.",
        },
    }
    reason = reason_templates.get(language, reason_templates["en"]).get(reason_key, "")
    advice_key = "buy" if target == "buy" else "sell"
    result.decision_type = target
    result.operation_advice = localize_operation_advice(advice_key, language)

    dashboard = result.dashboard if isinstance(result.dashboard, dict) else {}
    result.dashboard = dashboard
    core = dashboard.get("core_conclusion")
    if not isinstance(core, dict):
        core = {}
        dashboard["core_conclusion"] = core
    if target == "buy":
        core["signal_type"] = "🟢买入信号" if language == "zh" else "🟢 Buy signal"
    else:
        core["signal_type"] = "🔴卖出信号" if language == "zh" else "🔴 Sell signal"

    dashboard["decision_stability"] = {
        "applied": True,
        "promotion": True,
        "reason": reason,
        "reason_key": reason_key,
        "current_price": current_price,
        "support": support,
        "resistance": resistance,
        "capital_flow_bias": flow_bias,
    }
    _sync_stability_dashboard_fields(result)
    logger.info("[decision_stability] Promoted hold to %s: %s", target, reason_key)


def _downgrade_to_structural_hold(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
) -> None:
    result.decision_type = "hold"
    _set_structural_hold_wording(
        result,
        language,
        advice_key=advice_key,
        reason_key=reason_key,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
        calibrate_score=True,
    )


def _set_structural_hold_wording(
    result: "AnalysisResult",
    language: str,
    *,
    advice_key: str,
    reason_key: str,
    current_price: float,
    support: Optional[float],
    resistance: Optional[float],
    flow_bias: str,
    calibrate_score: bool = False,
) -> None:
    advice_map = {
        "zh": {
            "range": "震荡观望",
            "shakeout": "洗盘观察",
            "hold": "持有观察",
        },
        "en": {
            "range": "Range-bound watch",
            "shakeout": "Shakeout watch",
            "hold": "Hold and watch",
        },
        "ko": {
            "range": "박스권 관망",
            "shakeout": "흔들기 관찰",
            "hold": "보유 관찰",
        },
    }
    advice_default = {"zh": "持有观察", "en": "Hold and watch", "ko": "보유 관찰"}.get(language, "Hold and watch")
    advice = advice_map.get(language, advice_map["en"]).get(advice_key, advice_default)
    reason_templates = {
        "zh": {
            "buy_near_resistance": "价格接近压力位且主力资金未确认流入，不宜仅因短线反弹追买。",
            "buy_with_outflow": "主力资金流出与买入结论冲突，买点需等待支撑确认或资金回流。",
            "sell_near_support": "价格贴近支撑且未见资金持续流出，不宜仅因单日下跌直接卖出。",
            "sell_with_inflow": "主力资金流入与卖出结论冲突，先按持有观察处理并跟踪支撑失效。",
            "hold_shakeout": "价格回落至支撑附近但资金未确认流出，更适合按洗盘观察处理。",
            "hold_mid_range": "价格处于支撑与压力之间且资金流不明确，维持震荡观望更可操作。",
        },
        "en": {
            "buy_near_resistance": "Price is near resistance without confirmed main-force inflow, so chasing the rebound is not actionable.",
            "buy_with_outflow": "Main-force outflow conflicts with a buy call; wait for support confirmation or capital inflow.",
            "sell_near_support": "Price is near support without sustained outflow, so a one-day drop is not enough to sell.",
            "sell_with_inflow": "Main-force inflow conflicts with a sell call; hold and watch for support failure.",
            "hold_shakeout": "Price pulled back near support without confirmed outflow, which is better treated as a shakeout watch.",
            "hold_mid_range": "Price is between support and resistance with neutral fund flow, so range-bound watch is more actionable.",
        },
        "ko": {
            "buy_near_resistance": "가격이 저항선에 근접했고 주력 자금 유입이 확인되지 않아 단기 반등만 보고 추격 매수하기 어렵습니다.",
            "buy_with_outflow": "주력 자금 유출이 매수 결론과 상충하므로 지지 확인이나 자금 재유입을 기다려야 합니다.",
            "sell_near_support": "가격이 지지선에 근접했고 지속적 유출이 없어 하루 하락만으로 매도하기 어렵습니다.",
            "sell_with_inflow": "주력 자금 유입이 매도 결론과 상충하므로 우선 보유 관찰하며 지지 이탈을 추적합니다.",
            "hold_shakeout": "가격이 지지선 부근까지 눌렸지만 유출이 확인되지 않아 흔들기 관찰로 처리하는 것이 적절합니다.",
            "hold_mid_range": "가격이 지지선과 저항선 사이이고 자금 흐름이 불명확해 박스권 관망이 더 실행 가능합니다.",
        },
    }
    reason = reason_templates.get(language, reason_templates["en"]).get(reason_key, "")
    if calibrate_score:
        final_action = "watch" if advice_key in {"range", "shakeout"} else "hold"
        _bound_hold_watch_sentiment_score(result, reason=reason, final_action=final_action)
    result.operation_advice = advice
    if advice_key == "range":
        if language == "zh" and "震荡" not in str(result.trend_prediction):
            result.trend_prediction = "震荡"
        elif language == "en":
            result.trend_prediction = "Sideways"
        elif language == "ko":
            result.trend_prediction = "횡보"

    if language == "zh":
        no_position = "空仓先不追涨杀跌，等待支撑确认、放量突破或资金回流后再行动。"
        has_position = "持仓以关键支撑为风控线，未跌破前以观察和分批控仓为主。"
    elif language == "ko":
        no_position = "현금 보유 시 추격·투매를 삼가고 지지 확인·대량 돌파·자금 재유입 후 행동하세요."
        has_position = "보유 시 핵심 지지선을 리스크 관리선으로 삼고, 이탈 전까지 관찰과 분할 관리 위주로 대응하세요."
    else:
        no_position = "Do not chase or panic; wait for support confirmation, breakout, or renewed inflow."
        has_position = "Use key support as the risk line and manage position size unless support fails."
    _apply_hold_watch_dashboard(
        result,
        language,
        advice=advice,
        reason=reason,
        current_price=current_price,
        support=support,
        resistance=resistance,
        flow_bias=flow_bias,
        no_position=no_position,
        has_position=has_position,
    )
    logger.info("[decision_stability] Applied structural hold calibration: %s", reason_key)


def get_stock_name_multi_source(
    stock_code: str,
    context: Optional[Dict] = None,
    data_manager = None
) -> str:
    """
    多来源获取股票中文名称

    获取策略（按优先级）：
    1. 从传入的 context 中获取（realtime 数据）
    2. 从静态映射表 STOCK_NAME_MAP 获取
    3. 从 DataFetcherManager 获取（各数据源）
    4. 返回默认名称（股票+代码）

    Args:
        stock_code: 股票代码
        context: 分析上下文（可选）
        data_manager: DataFetcherManager 实例（可选）

    Returns:
        股票中文名称
    """
    # 1. 从上下文获取（实时行情数据）
    if context:
        # 优先从 stock_name 字段获取
        if context.get('stock_name'):
            name = context['stock_name']
            if name and not name.startswith('股票') and not name.lower().startswith('stock '):
                return name

        # 其次从 realtime 数据获取
        if 'realtime' in context and context['realtime'].get('name'):
            return context['realtime']['name']

    # 2. 从静态映射表获取
    if stock_code in STOCK_NAME_MAP:
        return STOCK_NAME_MAP[stock_code]

    # 3. 从数据源获取
    if data_manager is None:
        try:
            from data_provider.base import DataFetcherManager
            data_manager = DataFetcherManager()
        except Exception as e:
            logger.debug(f"Failed to initialize DataFetcherManager: {e}")

    if data_manager:
        try:
            name = data_manager.get_stock_name(stock_code)
            if name:
                # 更新缓存
                STOCK_NAME_MAP[stock_code] = name
                return name
        except Exception as e:
            logger.debug(f"Failed to fetch stock name from data source: {e}")

    # 4. 返回默认名称
    return f'Stock {stock_code}'


@dataclass
class AnalysisResult:
    """
    AI 分析结果数据类 - 决策仪表盘版

    封装 Gemini 返回的分析结果，包含决策仪表盘和详细分析
    """
    code: str
    name: str

    # ========== 核心指标 ==========
    sentiment_score: int  # 综合评分 0-100 (>70强烈看多, >60看多, 40-60震荡, <40看空)
    trend_prediction: str  # 趋势预测：强烈看多/看多/震荡/看空/强烈看空
    operation_advice: str  # 操作建议：买入/加仓/持有/减仓/卖出/观望
    decision_type: str = "hold"  # 决策类型：buy/hold/sell（用于统计）
    confidence_level: str = "Medium"  # 置信度：高/中/低
    report_language: str = "zh"  # 报告输出语言：zh/en
    action: Optional[str] = None  # 建议动作 taxonomy：buy/add/hold/reduce/sell/watch/avoid/alert
    action_label: Optional[str] = None  # 本地化建议动作标签

    # ========== 决策仪表盘 (新增) ==========
    dashboard: Optional[Dict[str, Any]] = None  # 完整的决策仪表盘数据

    # ========== 走势分析 ==========
    trend_analysis: str = ""  # 走势形态分析（支撑位、压力位、趋势线等）
    short_term_outlook: str = ""  # 短期展望（1-3日）
    medium_term_outlook: str = ""  # 中期展望（1-2周）

    # ========== 技术面分析 ==========
    technical_analysis: str = ""  # 技术指标综合分析
    ma_analysis: str = ""  # 均线分析（多头/空头排列，金叉/死叉等）
    volume_analysis: str = ""  # 量能分析（放量/缩量，主力动向等）
    pattern_analysis: str = ""  # K线形态分析

    # ========== 基本面分析 ==========
    fundamental_analysis: str = ""  # 基本面综合分析
    sector_position: str = ""  # 板块地位和行业趋势
    company_highlights: str = ""  # 公司亮点/风险点

    # ========== 情绪面/消息面分析 ==========
    news_summary: str = ""  # 近期重要新闻/公告摘要
    market_sentiment: str = ""  # 市场情绪分析
    hot_topics: str = ""  # 相关热点话题

    # ========== 综合分析 ==========
    analysis_summary: str = ""  # 综合分析摘要
    key_points: str = ""  # 核心看点（3-5个要点）
    risk_warning: str = ""  # 风险提示
    buy_reason: str = ""  # 买入/卖出理由

    # ========== 元数据 ==========
    market_snapshot: Optional[Dict[str, Any]] = None  # 当日行情快照（展示用）
    raw_response: Optional[str] = None  # 原始响应（调试用）
    search_performed: bool = False  # 是否执行了联网搜索
    data_sources: str = ""  # 数据来源说明
    success: bool = True
    error_message: Optional[str] = None

    # ========== 价格数据（分析时快照）==========
    current_price: Optional[float] = None  # 分析时的股价
    change_pct: Optional[float] = None     # 分析时的涨跌幅(%)

    # ========== 模型标记（Issue #528）==========
    model_used: Optional[str] = None  # 分析使用的 LLM 模型（完整名，如 gemini/gemini-2.0-flash）

    # ========== 历史对比（Report Engine P0）==========
    query_id: Optional[str] = None  # 本次分析 query_id，用于历史对比时排除本次记录

    # ========== 基本面上下文（仅运行时，用于通知拼装；不持久化到 to_dict）==========
    fundamental_context: Optional[Dict[str, Any]] = None
    market_structure_context: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'sentiment_score': self.sentiment_score,
            'trend_prediction': self.trend_prediction,
            'operation_advice': self.operation_advice,
            'decision_type': self.decision_type,
            'confidence_level': self.confidence_level,
            'report_language': self.report_language,
            'action': self.action,
            'action_label': self.action_label,
            'dashboard': self.dashboard,  # 决策仪表盘数据
            'trend_analysis': self.trend_analysis,
            'short_term_outlook': self.short_term_outlook,
            'medium_term_outlook': self.medium_term_outlook,
            'technical_analysis': self.technical_analysis,
            'ma_analysis': self.ma_analysis,
            'volume_analysis': self.volume_analysis,
            'pattern_analysis': self.pattern_analysis,
            'fundamental_analysis': self.fundamental_analysis,
            'sector_position': self.sector_position,
            'company_highlights': self.company_highlights,
            'news_summary': self.news_summary,
            'market_sentiment': self.market_sentiment,
            'hot_topics': self.hot_topics,
            'analysis_summary': self.analysis_summary,
            'key_points': self.key_points,
            'risk_warning': self.risk_warning,
            'buy_reason': self.buy_reason,
            'market_snapshot': self.market_snapshot,
            'search_performed': self.search_performed,
            'success': self.success,
            'error_message': self.error_message,
            'current_price': self.current_price,
            'change_pct': self.change_pct,
            'model_used': self.model_used,
            'market_structure_context': self.market_structure_context,
        }

    def get_core_conclusion(self) -> str:
        """获取核心结论（一句话）"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            return self.dashboard['core_conclusion'].get('one_sentence', self.analysis_summary)
        return self.analysis_summary

    def get_position_advice(self, has_position: bool = False) -> str:
        """获取持仓建议"""
        if self.dashboard and 'core_conclusion' in self.dashboard:
            pos_advice = self.dashboard['core_conclusion'].get('position_advice', {})
            if has_position:
                return pos_advice.get('has_position', self.operation_advice)
            return pos_advice.get('no_position', self.operation_advice)
        return self.operation_advice

    def get_sniper_points(self) -> Dict[str, str]:
        """获取狙击点位"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('sniper_points', {})
        return {}

    def get_checklist(self) -> List[str]:
        """获取检查清单"""
        if self.dashboard and 'battle_plan' in self.dashboard:
            return self.dashboard['battle_plan'].get('action_checklist', [])
        return []

    def get_risk_alerts(self) -> List[str]:
        """获取风险警报"""
        if self.dashboard and 'intelligence' in self.dashboard:
            return self.dashboard['intelligence'].get('risk_alerts', [])
        return []

    def get_emoji(self) -> str:
        """根据操作建议返回对应 emoji"""
        _, emoji, _ = get_signal_level(
            self.operation_advice,
            self.sentiment_score,
            self.report_language,
        )
        return emoji

    def get_confidence_stars(self) -> str:
        """返回置信度星级"""
        star_map = {
            "高": "⭐⭐⭐",
            "high": "⭐⭐⭐",
            "中": "⭐⭐",
            "medium": "⭐⭐",
            "低": "⭐",
            "low": "⭐",
        }
        return star_map.get(str(self.confidence_level or "").strip().lower(), "⭐⭐")


def populate_decision_action_fields(
    result: AnalysisResult,
    *,
    explicit_action: Any = None,
    report_type: Any = None,
    use_existing_action: bool = True,
    align_with_score: bool = True,
) -> AnalysisResult:
    """Populate optional decision action fields without changing legacy advice."""

    action_source = explicit_action
    if action_source is None and use_existing_action:
        action_source = getattr(result, "action", None)

    fields = build_action_fields(
        operation_advice=getattr(result, "operation_advice", None),
        explicit_action=action_source,
        report_type=report_type,
        report_language=getattr(result, "report_language", "zh"),
        sentiment_score=getattr(result, "sentiment_score", None),
        guardrail_reason=getattr(result, "guardrail_reason", None),
        align_with_score=align_with_score,
    )
    result.action = fields["action"]
    result.action_label = fields["action_label"]
    return result


class GeminiAnalyzer:
    """
    Gemini AI 分析器

    职责：
    1. 调用 Google Gemini API 进行股票分析
    2. 结合预先搜索的新闻和技术面数据生成分析报告
    3. 解析 AI 返回的 JSON 格式结果

    使用方式：
        analyzer = GeminiAnalyzer()
        result = analyzer.analyze(context, news_context)
    """

    # ========================================
    # System prompt - Decision Dashboard v2.0
    # ========================================
    # Output format: upgraded from a simple signal to a full decision dashboard
    # Core modules: core conclusion + data perspective + intelligence + battle plan
    # ========================================

    _DASHBOARD_JSON_TEMPLATE_HEAD = """## Output format: Decision Dashboard JSON

Output strictly in the following JSON format. This is a complete **Decision Dashboard**:

```json
{
    "stock_name": "Company name",
    "sentiment_score": integer 0-100,
    "trend_prediction": "Strong Bullish/Bullish/Sideways/Bearish/Strong Bearish",
    "operation_advice": "Buy/Add/Hold/Reduce/Sell/Watch",
    "decision_type": "buy/hold/sell",
    "action": "buy/add/hold/reduce/sell/watch/avoid/alert",
    "guardrail_reason": "Fill in the downgrade/upgrade reason when the score band and the final action disagree; otherwise leave empty",
    "confidence_level": "High/Medium/Low",
    "p_up": integer 0-100 (multiple of 5; probability that price reaches the target before the stop),
    "ev_contract": {
        "entry": entry price number,
        "stop": stop-loss price number,
        "target": target price number,
        "r_multiple": reward/risk number ((target-entry)/(entry-stop)),
        "expected_value": expected value number (p*R-(1-p)),
        "flip_condition": "Required when watching: the specific price level or event that flips the conclusion to buy/sell",
        "short_plan": {
            "entry": short entry price number (near the current price),
            "stop": buy-stop price number ABOVE entry (invalidation level),
            "target": cover target number BELOW entry (next support),
            "r_multiple": reward/risk number ((entry-target)/(stop-entry)),
            "expected_value": expected value number (p_down*R-(1-p_down), p_down=1-p_up/100)
        } — optional; ONLY for a sell/avoid conclusion on a US stock that is a concrete downside trade (see EV contract rules); omit entirely otherwise,
    },

    "dashboard": {
        "core_conclusion": {
            "one_sentence": "One-sentence core conclusion (under 30 words; tell the user exactly what to do)",
            "signal_type": "🟢 Buy signal/🟡 Hold-Watch/🔴 Sell signal/⚠️ Risk warning",
            "time_sensitivity": "Act now/Within today/Within this week/No rush",
            "position_advice": {
                "no_position": "For those without a position: specific action guidance",
                "has_position": "For those holding a position: specific action guidance"
            }
        },

        "data_perspective": {
            "trend_status": {
                "ma_alignment": "Description of the moving-average alignment",
                "is_bullish": true/false,
                "trend_score": 0-100
            },
            "price_position": {
                "current_price": current price number,
                "ma5": MA5 number,
                "ma10": MA10 number,
                "ma20": MA20 number,
                "bias_ma5": bias percentage number,
                "bias_status": "Safe/Caution/Danger",
                "support_level": support price,
                "resistance_level": resistance price
            },
            "volume_analysis": {
                "volume_ratio": volume ratio number,
                "volume_status": "Expanding/Shrinking/Flat",
                "turnover_rate": turnover rate percentage,
                "volume_meaning": "Interpretation of volume (e.g. a shrinking-volume pullback suggests easing selling pressure)"
            },
            "chip_structure": {
                "profit_ratio": profit ratio,
                "avg_cost": average cost,
                "concentration": chip concentration,
                "chip_health": "Healthy/Average/Caution"
            }
        },

        "intelligence": {
            "latest_news": "[Latest news] summary of recent important news",
            "risk_alerts": ["Risk 1: specific description", "Risk 2: specific description"],
            "positive_catalysts": ["Catalyst 1: specific description", "Catalyst 2: specific description"],
            "earnings_outlook": "Earnings outlook analysis (based on guidance, preliminary results, etc.)",
            "sentiment_summary": "One-sentence sentiment summary"
        },

        "battle_plan": {
            "sniper_points": {
{sniper_points_placeholder}
            },
            "position_strategy": {
                "suggested_position": "Suggested position: X/10",
                "entry_plan": "Description of the staged entry plan",
                "risk_control": "Description of the risk-control plan"
            },
            "action_checklist": [
{checklist_placeholder}
            ]
        },

        "phase_decision": {
            "phase_context": {"phase": "premarket/intraday/lunch_break/closing_auction/postmarket/non_trading/unknown"},
            "action_window": "Pre-market plan/Intraday tracking/Midday confirmation/Pre-close risk control/Post-market review/Non-trading-day watch",
            "immediate_action": "Act now/Wait for confirmation/Observe/Stop-loss or take-profit alert/Do not chase/No intraday action",
            "watch_conditions": ["Watch condition 1", "Watch condition 2"],
            "next_check_time": "Next check point or market-local time",
            "confidence_reason": "Confidence rationale, including phase and data-quality limitations",
            "data_limitations": ["Phase or data-quality limitation 1", "Phase or data-quality limitation 2"]
        },

        "signal_attribution": {
            "technical_indicators": technical indicator contribution (0-100),
            "news_sentiment": news sentiment contribution (0-100),
            "fundamentals": fundamentals contribution (0-100),
            "market_conditions": market environment contribution (0-100),
            "strongest_bullish_signal": "Name of the strongest bullish signal",
            "strongest_bearish_signal": "Name of the strongest bearish signal"
        }
    },

    "analysis_summary": "Comprehensive analysis summary (about 100 words)",
    "key_points": "3-5 key points, comma separated",
    "risk_warning": "Risk warning",
{buy_reason_placeholder}

    "trend_analysis": "Price pattern analysis",
    "short_term_outlook": "Short-term (1-3 day) outlook",
    "medium_term_outlook": "Medium-term (1-2 week) outlook",
    "technical_analysis": "Comprehensive technical analysis",
    "ma_analysis": "Moving-average system analysis",
    "volume_analysis": "Volume analysis",
    "pattern_analysis": "Candlestick pattern analysis",
    "fundamental_analysis": "Fundamental analysis",
    "sector_position": "Sector and industry analysis",
    "company_highlights": "Company highlights/risks",
    "news_summary": "News summary",
    "market_sentiment": "Market sentiment",
    "hot_topics": "Related hot topics",

    "search_performed": true/false,
    "data_sources": "Description of data sources"
}
```
"""

    _EV_CONTRACT_RULES = """## Expected-value (EV) decision contract and stability constraints

- Every analysis must provide `p_up`: an integer from 0 to 100 (in steps of 5) representing the subjective probability that price touches the target (rather than the stop) within the stated horizon.
- Entry, stop, and target prices are mandatory; when the input provides **System-computed reference levels (ATR-based)**, anchor on them, otherwise prefer the computed support/resistance levels; reasoned adjustments are allowed but the rationale must be written out.
- Compute the reward/risk ratio R = (target - entry) / (entry - stop) and the expected value EV = p*R - (1-p) (where p = p_up/100).
- Output hold/watch only when EV < 0 or R < 1.5; a watch call must state in `ev_contract.flip_condition` the specific price level or event that would flip the conclusion to buy or sell.
- Medium-risk opportunities with positive EV and R >= 1.5 must be reported honestly as buy with an honest `p_up`; do not dilute them into watch for the sake of caution.
- US stocks can be sold short. When your honest conclusion is sell/avoid on a stock (not merely "already fully valued") because of a concrete negative catalyst or broken structure, evaluate the downside trade: R_short = (entry - target) / (stop - entry) with the stop ABOVE entry at the invalidation level and the target at the next support. If p_down = 1 - p_up/100 gives EV = p_down*R_short - (1-p_down) > 0 and R_short >= 1.5, provide `ev_contract.short_plan` with those levels. This does not change the action taxonomy — the action stays sell/avoid; the plan makes the bearish view executable. Omit `short_plan` when the bearish case is not a concrete trade (no clean invalidation level, event risk, or R_short < 1.5).
- For markets without capital-flow data (US/HK/TW etc.), rely on price/volume structure; never refuse a buy/sell conclusion merely because flow confirmation is missing.
- Do not flip violently between buy and sell merely because of a single day's move or a score crossing a band boundary; any change of conclusion must be tied to a specific price level or event.
- Operation advice must jointly consider price position (support/resistance), volume/chips, main-force capital flow (when available), and risk events.
- `dashboard.phase_decision` with all seven fields is mandatory; during intraday, lunch break, or near the close, give the current action, watch conditions, and the next check point.
- The optional display block `dashboard.signal_attribution` (six fields) is recommended: explain the composition of the recommendation, including the contribution of technical indicators, news sentiment, fundamentals, and market environment, plus the strongest bullish/bearish signals.
- Never fabricate today's intraday action during pre-market, non-trading, or unknown phases; when quote/daily_bars/technical data is stale, fallback, missing, fetch_failed, partial, or estimated, `confidence_level` must not be High."""

    _LEGACY_SNIPER_POINTS = """                "ideal_buy": "Ideal entry: XX (near MA5)",
                "secondary_buy": "Secondary entry: XX (near MA10)",
                "stop_loss": "Stop loss: XX (break below MA20 or X%)",
                "take_profit": "Target: XX (prior high / round number)\""""

    _LEGACY_CHECKLIST = """                "✅/⚠️/❌ Check 1: bullish MA alignment",
                "✅/⚠️/❌ Check 2: bias is reasonable (may be relaxed in strong trends)",
                "✅/⚠️/❌ Check 3: volume confirmation",
                "✅/⚠️/❌ Check 4: no major negative news",
                "✅/⚠️/❌ Check 5: healthy chip structure",
                "✅/⚠️/❌ Check 6: reasonable PE valuation\""""

    _SKILL_SNIPER_POINTS = """                "ideal_buy": "Ideal entry: XX (meets the primary skill trigger)",
                "secondary_buy": "Secondary entry: XX (more conservative or after confirmation)",
                "stop_loss": "Stop loss: XX (invalidation condition or X% risk)",
                "take_profit": "Target: XX (set by resistance / reward-risk ratio)\""""

    _SKILL_CHECKLIST = """                "✅/⚠️/❌ Check 1: current structure meets the activated skill conditions",
                "✅/⚠️/❌ Check 2: entry location and reward/risk are reasonable",
                "✅/⚠️/❌ Check 3: volume/volatility/chips support the verdict",
                "✅/⚠️/❌ Check 4: no major negative news",
                "✅/⚠️/❌ Check 5: position size and stop-loss plan are explicit",
                "✅/⚠️/❌ Check 6: valuation/earnings/catalysts match the conclusion\""""

    LEGACY_DEFAULT_SYSTEM_PROMPT = """You are a trend-trading-focused {market_placeholder} investment analyst responsible for producing a professional **Decision Dashboard** analysis report.

{guidelines_placeholder}

{skill_policy_placeholder}

""" + CANONICAL_DECISION_SCALE_PROMPT + """

""" + _DASHBOARD_JSON_TEMPLATE_HEAD.replace(
        "{sniper_points_placeholder}", _LEGACY_SNIPER_POINTS
    ).replace(
        "{checklist_placeholder}", _LEGACY_CHECKLIST
    ).replace(
        "{buy_reason_placeholder}", '    "buy_reason": "Rationale for the action, citing the trading philosophy",'
    ) + """
## Scoring criteria

### Strong Buy (80-100):
- ✅ Bullish alignment: MA5 > MA10 > MA20
- ✅ Low bias: <2%, the best entry point
- ✅ Shrinking-volume pullback or expanding-volume breakout
- ✅ Concentrated, healthy chip structure
- ✅ Positive news catalysts

### Buy (60-79):
- ✅ Bullish or weakly bullish alignment
- ✅ Bias <5%
- ✅ Normal volume
- ⚪ One secondary condition may be unmet

### Watch (40-59):
- ⚠️ Bias >5% (risk of chasing)
- ⚠️ Tangled moving averages, unclear trend
- ⚠️ Risk events present
- ⚠️ The score expresses conviction; staying at Watch requires EV<0 or R<1.5 plus an explicit flip condition (flip_condition)

### Reduce (20-39):
- ⚠️ Trend weakening or break below key moving averages
- ⚠️ Capital/volume turning weak; risk clearly exceeds reward
- ⚠️ Focus on lowering exposure and protecting gains

### Sell (0-19):
- ❌ Bearish alignment or materially deteriorating trend
- ❌ Break below key support / stop-loss level
- ❌ Expanding-volume decline or major negative news

## Core principles of the Decision Dashboard

1. **Conclusion first**: one sentence that says whether to buy or sell
2. **Position-aware advice**: different guidance for those without and with a position
3. **Precise sniper levels**: give specific prices; no vague language
4. **Visual checklist**: mark every check with ✅⚠️❌
5. **Risk priority**: highlight risk points found in the news

""" + _EV_CONTRACT_RULES

    SYSTEM_PROMPT = """You are a {market_placeholder} investment analyst responsible for producing a professional **Decision Dashboard** analysis report.

{guidelines_placeholder}

{default_skill_policy_section}
{skills_section}

""" + CANONICAL_DECISION_SCALE_PROMPT + """

""" + _DASHBOARD_JSON_TEMPLATE_HEAD.replace(
        "{sniper_points_placeholder}", _SKILL_SNIPER_POINTS
    ).replace(
        "{checklist_placeholder}", _SKILL_CHECKLIST
    ).replace(
        "{buy_reason_placeholder}", '    "buy_reason": "Rationale for the action, citing the activated skills or risk framework",'
    ) + """
## Scoring criteria

### Strong Buy (80-100):
- ✅ Multiple activated skills support a positive conclusion at the same time
- ✅ Upside, trigger conditions, and reward/risk are clear
- ✅ Key risks have been screened; position size and stop-loss plan are explicit
- ✅ Important data and intelligence conclusions agree with each other

### Buy (60-79):
- ✅ The primary signal is positive, but a few items still await confirmation
- ✅ Controllable risks or a secondary entry point are acceptable
- ✅ Watch conditions must be stated explicitly in the report

### Watch (40-59):
- ⚠️ Signals diverge materially, or confirmation is insufficient
- ⚠️ Risk and opportunity are roughly balanced
- ⚠️ Better to wait for the trigger condition or avoid the uncertainty
- ⚠️ The score expresses conviction; staying at Watch requires EV<0 or R<1.5 plus an explicit flip condition (flip_condition)

### Reduce (20-39):
- ⚠️ The primary conclusion is weakening; risk clearly exceeds reward
- ⚠️ Some invalidation conditions triggered; existing positions need lower exposure
- ⚠️ Better suited to protecting gains than attacking

### Sell (0-19):
- ❌ Stop-loss/invalidation conditions or major negative news triggered
- ❌ Trend or risk has deteriorated materially
- ❌ Existing positions should prioritize exiting

## Core principles of the Decision Dashboard

1. **Conclusion first**: one sentence that says whether to buy or sell
2. **Position-aware advice**: different guidance for those without and with a position
3. **Precise sniper levels**: give specific prices; no vague language
4. **Visual checklist**: mark every check with ✅⚠️❌
5. **Risk priority**: highlight risk points found in the news

""" + _EV_CONTRACT_RULES

    TEXT_SYSTEM_PROMPT = """You are a professional stock analysis assistant.

- Answers must be grounded in the data and context provided by the user
- If information is insufficient, state the uncertainty explicitly
- Do not fabricate prices, financial results, or news facts
"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        config: Optional[Config] = None,
        skills: Optional[List[str]] = None,
        skill_instructions: Optional[str] = None,
        default_skill_policy: Optional[str] = None,
        use_legacy_default_prompt: Optional[bool] = None,
    ):
        """Initialize LLM Analyzer via LiteLLM.

        Args:
            api_key: Ignored (kept for backward compatibility). Keys are loaded from config.
        """
        self._config_override = config
        self._requested_skills = list(skills) if skills is not None else None
        self._skill_instructions_override = skill_instructions
        self._default_skill_policy_override = default_skill_policy
        self._use_legacy_default_prompt_override = use_legacy_default_prompt
        self._resolved_prompt_state: Optional[Dict[str, Any]] = None
        self._router = None
        self._legacy_router_model_list: List[Dict[str, Any]] = []
        self._litellm_available = False
        self._init_litellm()
        if not self._litellm_available:
            try:
                backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
            except GenerationError:
                backend_id = ""
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                logger.info(
                    "Analyzer generation backend: %s configured; LiteLLM API keys are not "
                    "required for stock analysis generation",
                    backend_id,
                )
            else:
                logger.warning("No LLM configured (LITELLM_MODEL / API keys), AI analysis will be unavailable")

    def _get_runtime_config(self) -> Config:
        """Return the runtime config, honoring injected overrides for tests/pipeline."""
        return getattr(self, "_config_override", None) or get_config()

    def _get_skill_prompt_sections(self) -> tuple[str, str, bool]:
        """Resolve skill instructions + default baseline + prompt mode."""
        skill_instructions = getattr(self, "_skill_instructions_override", None)
        default_skill_policy = getattr(self, "_default_skill_policy_override", None)
        use_legacy_default_prompt = getattr(self, "_use_legacy_default_prompt_override", None)

        if skill_instructions is not None and default_skill_policy is not None:
            return (
                skill_instructions,
                default_skill_policy,
                bool(use_legacy_default_prompt) if use_legacy_default_prompt is not None else False,
            )

        resolved_state = getattr(self, "_resolved_prompt_state", None)
        if resolved_state is None:
            from src.agent.factory import resolve_skill_prompt_state

            prompt_state = resolve_skill_prompt_state(
                self._get_runtime_config(),
                skills=getattr(self, "_requested_skills", None),
            )
            resolved_state = {
                "skill_instructions": prompt_state.skill_instructions,
                "default_skill_policy": prompt_state.default_skill_policy,
                "use_legacy_default_prompt": bool(getattr(prompt_state, "use_legacy_default_prompt", False)),
            }
            self._resolved_prompt_state = resolved_state

        return (
            skill_instructions if skill_instructions is not None else resolved_state.get("skill_instructions", ""),
            default_skill_policy if default_skill_policy is not None else resolved_state.get("default_skill_policy", ""),
            (
                use_legacy_default_prompt
                if use_legacy_default_prompt is not None
                else bool(resolved_state.get("use_legacy_default_prompt", False))
            ),
        )

    def _get_analysis_system_prompt(self, report_language: str, stock_code: str = "") -> str:
        """Build the analyzer system prompt with output-language guidance."""
        lang = normalize_report_language(report_language)
        market_role = get_market_role(stock_code, lang)
        market_guidelines = get_market_guidelines(stock_code, lang)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = self._get_skill_prompt_sections()
        if use_legacy_default_prompt:
            skill_policy = get_default_trading_skill_policy(
                explicit_skill_selection=False, report_language=lang
            )
            base_prompt = self.LEGACY_DEFAULT_SYSTEM_PROMPT.replace(
                "{market_placeholder}", market_role
            ).replace(
                "{guidelines_placeholder}", market_guidelines
            ).replace(
                "{skill_policy_placeholder}", skill_policy
            )
        else:
            skills_section = ""
            if skill_instructions:
                skills_section = f"## Activated trading skills\n\n{skill_instructions}\n"
            default_skill_policy_section = ""
            if default_skill_policy:
                default_skill_policy_section = f"{default_skill_policy}\n"
            base_prompt = (
                self.SYSTEM_PROMPT.replace("{market_placeholder}", market_role)
                .replace("{guidelines_placeholder}", market_guidelines)
                .replace("{default_skill_policy_section}", default_skill_policy_section)
                .replace("{skills_section}", skills_section)
            )
        if lang == "zh":
            return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in Simplified Chinese (简体中文).
- The template above uses English example values (e.g. Strong Bullish/Bullish/Sideways, Buy/Hold/Sell/Watch, High/Medium/Low, Expanding/Flat/Shrinking, Safe/Caution/Danger, Within this week, Suggested position: X/10). Never copy them verbatim: write the Chinese equivalents (强烈看多/看多/震荡, 买入/持有/卖出/观望, 高/中/低, 放量/平量/缩量, 安全/警戒/危险, 本周内, 建议仓位：X成).
- Use the official Chinese listed company name for `stock_name`.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
        if lang == "ko":
            return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in Korean (한국어).
- Use the common Korean or original listed company name when confident; do not invent one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
"""
        return base_prompt + """

## Output Language (highest priority)

- Keep all JSON keys unchanged.
- `decision_type` must remain `buy|hold|sell`.
- All human-readable JSON values must be written in English.
- Use the common English company name when you are confident; otherwise keep the original listed company name instead of inventing one.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, nested dashboard text, checklist items, and all narrative summaries.
- No Chinese characters may appear anywhere in the output values.
"""

    def _has_channel_config(self, config: Config) -> bool:
        """Check if multi-channel config (channels / YAML / legacy model_list) is active."""
        return bool(config.llm_model_list) and not all(
            e.get('model_name', '').startswith('__legacy_') for e in config.llm_model_list
        )

    @staticmethod
    def _legacy_router_provider_alias(model: str) -> str:
        provider = model.split("/", 1)[0] if "/" in model else "openai"
        return f"__legacy_{provider}__"

    @staticmethod
    def _build_legacy_router_model_list_from_config(
        model: str,
        model_list: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build legacy-router candidates from configured legacy llm_model_list entries."""
        if not model:
            return []
        target_model = model
        target_legacy_alias = GeminiAnalyzer._legacy_router_provider_alias(model)
        legacy_entries: List[Dict[str, Any]] = []
        for entry in model_list or []:
            if not isinstance(entry, dict):
                continue
            model_name = str(entry.get("model_name") or "").strip()
            if model_name != target_legacy_alias:
                continue

            params = entry.get("litellm_params")
            if not isinstance(params, dict):
                continue

            api_key = str(params.get("api_key") or "").strip()
            if not api_key or len(api_key) < 8:
                continue

            deployed_params = dict(params)
            deployed_params["model"] = target_model
            deployed_params["api_key"] = api_key
            legacy_entries.append({
                "model_name": target_model,
                "litellm_params": deployed_params,
            })

        return legacy_entries

    def _init_litellm(self) -> None:
        """Initialize litellm Router from channels / YAML / legacy keys."""
        config = self._get_runtime_config()
        if self._get_hermes_config_error(config) is not None:
            logger.error("Analyzer LLM: Hermes channel configuration blocks legacy fallback")
            return
        litellm_model = config.litellm_model
        if not litellm_model:
            backend_id = ""
            try:
                backend_id = resolve_generation_backend_id(config)
            except GenerationError:
                pass
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                logger.info(
                    "Analyzer LiteLLM: LITELLM_MODEL not configured; using %s generation backend",
                    backend_id,
                )
            else:
                logger.warning("Analyzer LLM: LITELLM_MODEL not configured")
            return

        self._litellm_available = True

        # --- Channel / YAML path: build Router from pre-built model_list ---
        if self._has_channel_config(config):
            model_list = config.llm_model_list
            if self._get_mixed_hermes_route_error(config, litellm_model) is not None:
                self._litellm_available = False
                logger.error("Analyzer LLM: mixed Hermes/non-Hermes route requires deployment-level no-proxy support")
                return
            router_model_list = model_list
            if route_has_hermes(model_list, litellm_model):
                # Hermes-only routes are dispatched directly with a request-scoped
                # no-proxy OpenAI client. Keeping them out of Router prevents the
                # default proxy-aware transport from seeing the Hermes bearer key.
                router_model_list = filter_non_hermes_deployments(model_list)
                if not router_model_list:
                    self._litellm_available = True
                    logger.info("Analyzer LLM: Hermes-only route will use direct no-proxy completion")
                    return
            try:
                self._router = Router(
                    model_list=router_model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug("Analyzer LLM: Router constructor signature not compatible; fallback to direct mode")
                self._router = None
            else:
                unique_models = list(dict.fromkeys(
                    e['litellm_params']['model'] for e in model_list
                ))
                logger.info(
                    f"Analyzer LLM: Router initialized from channels/YAML — "
                    f"{len(router_model_list)} deployment(s), models: {unique_models}"
                )
                return

        # --- Legacy path: build Router for multi-key, or use single key ---
        keys = get_api_keys_for_model(litellm_model, config)
        legacy_model_list = self._build_legacy_router_model_list_from_config(
            litellm_model,
            config.llm_model_list,
        )
        if len(legacy_model_list) <= 1 and keys:
            extra_params = extra_litellm_params(litellm_model, config)
            configured_model_list = [
                {
                    "model_name": litellm_model,
                    "litellm_params": {
                        "model": litellm_model,
                        "api_key": k,
                        **extra_params,
                    },
                }
                for k in keys
            ]
            if not legacy_model_list:
                legacy_model_list = configured_model_list
            elif len(legacy_model_list) < len(configured_model_list):
                legacy_model_list = configured_model_list

        if len(legacy_model_list) > 1:
            self._legacy_router_model_list = legacy_model_list
            try:
                self._router = Router(
                    model_list=legacy_model_list,
                    routing_strategy="simple-shuffle",
                    num_retries=2,
                )
            except TypeError:
                logger.debug("Analyzer LLM: Legacy Router constructor signature not compatible; using legacy model_list fallback")
                self._router = None
            else:
                logger.info(
                    f"Analyzer LLM: Legacy Router initialized with {len(legacy_model_list)} keys "
                    f"for {litellm_model}"
                )
                return

        if keys:
            logger.info(f"Analyzer LLM: litellm initialized (model={litellm_model})")
        else:
            logger.info(
                f"Analyzer LLM: litellm initialized (model={litellm_model}, "
                f"API key from environment)"
            )

    def is_available(self) -> bool:
        """Check whether the configured generation backend is available."""
        backend_error = self.get_generation_backend_config_error()
        if backend_error is not None:
            return self._can_use_generation_fallback(backend_error)
        backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
        if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
            return True
        return self._litellm_runtime_available()

    def _litellm_runtime_available(self) -> bool:
        return self._router is not None or self._litellm_available

    def _can_use_generation_fallback(self, backend_error: GenerationError) -> bool:
        if not backend_error.fallbackable:
            return False
        try:
            _backend_id, fallback_backend_id = self._resolve_generation_backend_config()
        except GenerationError:
            return False
        return (
            fallback_backend_id == LITELLM_BACKEND_ID
            and self._litellm_runtime_available()
        )

    def _resolve_generation_backend_config(self) -> Tuple[str, Optional[str]]:
        """Resolve and validate generation backend ids."""
        config = self._get_runtime_config()
        backend_id = resolve_generation_backend_id(config)
        fallback_backend_id = resolve_generation_fallback_backend_id(config)
        return backend_id, fallback_backend_id

    def get_generation_backend_config_error(self) -> Optional[GenerationError]:
        """Return a structured backend config error, if the backend cannot run."""
        try:
            backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
            config = self._get_runtime_config()
            hermes_error = self._get_hermes_config_error(config)
            if hermes_error is not None:
                return hermes_error
            for model in [getattr(config, "litellm_model", "")] + list(getattr(config, "litellm_fallback_models", []) or []):
                mixed_error = self._get_mixed_hermes_route_error(config, model)
                if mixed_error is not None:
                    return mixed_error
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                backend = self._get_generation_backend(backend_id)
                get_config_error = getattr(backend, "get_config_error", None)
                if callable(get_config_error):
                    return get_config_error()
        except GenerationError as exc:
            return exc
        return None

    def _get_hermes_config_error(self, config: Config) -> Optional[GenerationError]:
        issues = list(getattr(config, "llm_channel_config_issues", []) or [])
        if not getattr(config, "llm_blocks_legacy_fallback", False) or not issues:
            return None
        blocked_routes = set(getattr(config, "llm_blocked_hermes_routes", []) or [])
        selected_models = [
            ("LITELLM_MODEL", getattr(config, "litellm_model", "") or ""),
            *[
                ("LITELLM_FALLBACK_MODELS", fallback_model)
                for fallback_model in list(getattr(config, "litellm_fallback_models", []) or [])
            ],
        ]
        selected_blocked_route = ""
        selected_field = ""
        for field_name, model in selected_models:
            raw_model = str(model or "").strip()
            if not raw_model:
                continue
            candidates = hermes_blocked_route_candidates(raw_model)
            candidates.add(raw_model)
            try:
                candidates.add(canonicalize_hermes_model_ref(raw_model).route_model)
            except (TypeError, ValueError) as exc:
                logger.debug("Failed to canonicalize selected Hermes route candidate %r: %s", raw_model, exc)
            matched = candidates & blocked_routes
            if matched:
                selected_blocked_route = sorted(matched)[0]
                selected_field = field_name
                break
        if blocked_routes and not selected_blocked_route and getattr(config, "llm_model_list", None):
            return None
        first = issues[0]
        code = (
            "explicit_hermes_route_invalid"
            if selected_blocked_route
            else first.get("code", "invalid_hermes_channel")
        )
        return GenerationError(
            error_code=GenerationErrorCode.UNSAFE_CONFIG,
            stage="configuration",
            retryable=False,
            fallbackable=False,
            backend=LITELLM_BACKEND_ID,
            provider=HERMES_CHANNEL_NAME,
            details={
                "field": selected_field or first.get("field", "LLM_HERMES_API_KEY"),
                "code": code,
                "reason": code,
                "message": first.get("message", "Hermes channel configuration is invalid"),
                "issues": issues,
                "route_name": selected_blocked_route or None,
            },
        )

    def _get_mixed_hermes_route_error(self, config: Config, model: str) -> Optional[GenerationError]:
        if not model:
            return None
        origins = route_deployment_origins(getattr(config, "llm_model_list", []) or [], model)
        if not origins.is_mixed:
            return None
        return GenerationError(
            error_code=GenerationErrorCode.UNSAFE_CONFIG,
            stage="configuration",
            retryable=False,
            fallbackable=False,
            backend=LITELLM_BACKEND_ID,
            provider=HERMES_CHANNEL_NAME,
            details={
                "field": "LLM_CHANNELS",
                "code": "mixed_hermes_route_unsupported",
                "reason": "router_deployment_no_proxy_unavailable",
                "route_name": model,
            },
        )

    def _hermes_redaction_values_for_model(self, config: Config, model: str = "") -> set[str]:
        redactions: set[str] = set()
        deployments = list(getattr(config, "llm_model_list", []) or [])
        selected_deployments = deployments
        if model:
            origins = route_deployment_origins(deployments, model)
            selected_deployments = list(origins.hermes_deployments or [])
            if not selected_deployments and not origins.has_hermes:
                return redactions
        for deployment in selected_deployments:
            if not isinstance(deployment, dict):
                continue
            if not route_has_hermes([deployment], str(deployment.get("model_name") or "")):
                continue
            params = deployment.get("litellm_params") or {}
            if isinstance(params, dict):
                redactions.update(build_hermes_redaction_values(params.get("api_key")))
        return redactions

    def _sanitize_hermes_exception_text(
        self,
        exc: Any,
        *,
        config: Optional[Config] = None,
        model: str = "",
    ) -> str:
        runtime_config = config or self._get_runtime_config()
        redactions = self._hermes_redaction_values_for_model(runtime_config, model)
        if not redactions:
            return str(exc)
        return sanitize_hermes_error_text(exc, redaction_values=redactions)

    def _litellm_redaction_values_for_model(self, config: Config, model: str = "") -> set[str]:
        redactions = self._hermes_redaction_values_for_model(config, model)
        try:
            redactions.update(build_hermes_redaction_values(*get_api_keys_for_model(model, config)))
        except Exception:
            pass
        origins = route_deployment_origins(getattr(config, "llm_model_list", []) or [], model)
        for deployment in (*origins.hermes_deployments, *origins.non_hermes_deployments):
            params = deployment.get("litellm_params") if isinstance(deployment, dict) else None
            if isinstance(params, dict):
                redactions.update(build_hermes_redaction_values(params.get("api_key")))
        return redactions

    def _sanitize_litellm_exception_text(
        self,
        exc: Any,
        *,
        config: Optional[Config] = None,
        model: str = "",
    ) -> str:
        runtime_config = config or self._get_runtime_config()
        redactions = self._litellm_redaction_values_for_model(runtime_config, model)
        sanitized = sanitize_hermes_error_text(exc, redaction_values=redactions)
        return redact_diagnostic_text(sanitized, limit=500)

    def _dispatch_litellm_completion(
        self,
        model: str,
        call_kwargs: Dict[str, Any],
        *,
        config: Config,
        use_channel_router: bool,
        router_model_names: set[str],
    ) -> Any:
        """Dispatch a LiteLLM completion through router or direct fallback."""
        origins = route_deployment_origins(config.llm_model_list, model)
        if origins.is_mixed:
            raise RuntimeError("Hermes/non-Hermes mixed generation route is not supported without deployment-level no-proxy client support")
        if origins.is_hermes_only:
            deployment = origins.hermes_deployments[0]
            params = dict(deployment.get("litellm_params") or {})
            api_key = str(params.get("api_key") or "").strip()
            base_url = str(params.get("api_base") or "").strip()
            if is_masked_secret_placeholder(api_key):
                raise RuntimeError("Hermes API key is a masked placeholder and cannot be used for generation")
            timeout = float(call_kwargs.get("timeout") or 30.0)
            hermes_kwargs = dict(call_kwargs)
            hermes_kwargs["model"] = str(params.get("model") or model)
            hermes_kwargs["stream"] = False
            hermes_kwargs.pop("api_key", None)
            hermes_kwargs.pop("api_base", None)
            with open_hermes_no_proxy_client(api_key=api_key, base_url=base_url, timeout=timeout) as client:
                hermes_kwargs["client"] = client
                return litellm.completion(**hermes_kwargs)

        wire_models = resolve_fallback_litellm_wire_models(model, config.llm_model_list)
        register_fallback_model_pricing(wire_models)
        effective_kwargs = dict(call_kwargs)
        if use_channel_router and self._router and model in router_model_names:
            return self._router.completion(**effective_kwargs)
        if self._router and model == config.litellm_model and not use_channel_router:
            return self._router.completion(**effective_kwargs)

        keys = get_api_keys_for_model(model, config)
        if keys:
            effective_kwargs["api_key"] = keys[0]
        effective_kwargs.update(extra_litellm_params(model, config))
        return litellm.completion(**effective_kwargs)

    def _normalize_usage(
        self,
        usage_obj: Any,
        *,
        model: str = "",
        provider: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Normalize usage objects from LiteLLM responses/chunks."""
        if not usage_obj:
            usage = attach_message_hmacs({}, messages) if messages is not None else {}
            return filter_prompt_cache_telemetry(usage, self._get_runtime_config())
        usage = normalize_litellm_usage(usage_obj, model=model, provider=provider)
        if messages is not None:
            usage = attach_message_hmacs(usage, messages)
        return filter_prompt_cache_telemetry(usage, self._get_runtime_config())

    @staticmethod
    def _get_response_field(obj: Any, key: str) -> Any:
        """Read a field from dict-like or object-like LiteLLM payloads."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _extract_text_blocks(self, blocks: Any, *, strip: bool = True) -> str:
        """Extract final-answer text from OpenAI-compatible content blocks.

        Some reasoning models (including MiniMax) expose thinking and final
        answer blocks in the same list.  Thinking blocks can also carry a
        ``text`` field, so concatenating every block corrupts structured output
        by prefixing the JSON answer with chain-of-thought text.
        """
        if not blocks:
            return ""

        parts: List[str] = []
        for block in blocks:
            if isinstance(block, str):
                parts.append(block)
                continue

            block_type = ""
            text = None
            if isinstance(block, dict):
                block_type = str(block.get("type") or "").strip().lower()
                text = block.get("text")
                if text is None:
                    text = block.get("content")
            else:
                block_type = str(getattr(block, "type", "") or "").strip().lower()
                text = getattr(block, "text", None)
                if text is None:
                    text = getattr(block, "content", None)

            # Keep untyped legacy blocks for compatibility, but typed blocks
            # must explicitly represent final output text.
            if block_type and block_type not in {"text", "output_text"}:
                continue
            if isinstance(text, str) and text:
                parts.append(text)

        result = "".join(parts)
        return result.strip() if strip else result

    def _extract_completion_text(self, response: Any) -> str:
        """Extract text from non-stream LiteLLM completion responses."""
        choices = self._get_response_field(response, "choices")
        if not choices:
            return ""

        choice = choices[0]
        message = self._get_response_field(choice, "message")

        content_blocks = self._get_response_field(choice, "content_blocks")
        if content_blocks is None and message is not None:
            content_blocks = self._get_response_field(message, "content_blocks")
        block_text = self._extract_text_blocks(content_blocks)
        if block_text:
            return strip_leading_think_wrapper(block_text)

        content = None
        if message is not None:
            content = self._get_response_field(message, "content")
        if content is None:
            content = self._get_response_field(choice, "content")

        if isinstance(content, list):
            return strip_leading_think_wrapper(self._extract_text_blocks(content))
        if isinstance(content, str):
            return strip_leading_think_wrapper(content)
        return str(content).strip() if content is not None else ""

    def _extract_stream_text(self, chunk: Any) -> str:
        """Extract provider-agnostic text delta from a LiteLLM streaming chunk."""
        choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
        if not choices:
            return ""

        choice = choices[0]
        delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
        message = choice.get("message") if isinstance(choice, dict) else getattr(choice, "message", None)

        content: Any = None
        if isinstance(delta, dict):
            content = delta.get("content")
        elif isinstance(delta, str):
            content = delta
        elif delta is not None:
            content = getattr(delta, "content", None)

        if content is None:
            if isinstance(message, dict):
                content = message.get("content")
            elif message is not None:
                content = getattr(message, "content", None)

        if isinstance(content, list):
            return self._extract_text_blocks(content, strip=False)

        return content if isinstance(content, str) else ""

    def _consume_litellm_stream(
        self,
        stream_response: Any,
        *,
        model: str,
        usage_model: Optional[str] = None,
        provider: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Consume a LiteLLM stream into a single text payload."""
        chunks: List[str] = []
        usage: Dict[str, Any] = {}
        chars_received = 0
        next_emit_at = 1

        try:
            for chunk in stream_response:
                chunk_usage = extract_usage_payload(chunk)
                normalized_usage = self._normalize_usage(
                    chunk_usage,
                    model=usage_model or model,
                    provider=provider,
                )
                if normalized_usage:
                    usage = normalized_usage

                delta_text = self._extract_stream_text(chunk)
                if not delta_text:
                    continue

                chunks.append(delta_text)
                chars_received += len(delta_text)
                if progress_callback and chars_received >= next_emit_at:
                    progress_callback(chars_received)
                    next_emit_at = chars_received + 160
        except Exception as exc:
            raise _LiteLLMStreamError(
                f"{model} stream interrupted: {exc}",
                partial_received=chars_received > 0,
            ) from exc

        response_text = strip_leading_think_wrapper("".join(chunks))
        if not response_text:
            raise _LiteLLMStreamError(
                f"{model} stream returned empty response",
                partial_received=False,
            )

        if progress_callback and chars_received > 0:
            progress_callback(chars_received)

        return response_text, usage

    def _get_generation_backend(self, backend_id: Optional[str] = None) -> GenerationBackend:
        """Return the configured generation backend."""
        config = self._get_runtime_config()
        resolved_backend_id = backend_id or self._resolve_generation_backend_config()[0]
        return create_generation_backend(
            resolved_backend_id,
            config=config,
            litellm_completion_callable=self._call_litellm_impl,
        )

    def _call_litellm(
        self,
        prompt: str,
        generation_config: dict,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Compatibility wrapper around the configured generation backend."""
        preflight_error = self.get_generation_backend_config_error()
        if preflight_error is not None and not self._can_use_generation_fallback(preflight_error):
            raise preflight_error
        backend_id, fallback_backend_id = self._resolve_generation_backend_config()
        try:
            result = self._get_generation_backend(backend_id).generate(
                prompt,
                generation_config,
                system_prompt=system_prompt,
                stream=stream,
                stream_progress_callback=stream_progress_callback,
                response_validator=response_validator,
                audit_context=audit_context,
            )
        except GenerationError as exc:
            if not exc.fallbackable or not fallback_backend_id:
                raise
            try:
                fallback_backend = self._get_generation_backend(fallback_backend_id)
            except GenerationError as fallback_exc:
                raise GenerationError(
                    error_code=fallback_exc.error_code,
                    stage="fallback",
                    retryable=False,
                    fallbackable=False,
                    backend=fallback_backend_id,
                    provider=fallback_exc.provider,
                    details={
                        "primary_error": {
                            "error_code": exc.error_code.value,
                            "backend": exc.backend,
                            "provider": exc.provider,
                            "stage": exc.stage,
                            "details": exc.details,
                        },
                        "fallback_error": fallback_exc.details,
                    },
                ) from fallback_exc
            try:
                result = fallback_backend.generate(
                    prompt,
                    generation_config,
                    system_prompt=system_prompt,
                    stream=stream,
                    stream_progress_callback=stream_progress_callback,
                    response_validator=response_validator,
                    audit_context=audit_context,
                )
            except _AllModelsFailedError:
                raise
            except GenerationError as fallback_exc:
                raise GenerationError(
                    error_code=fallback_exc.error_code,
                    stage="fallback",
                    retryable=False,
                    fallbackable=False,
                    backend=fallback_backend_id,
                    provider=fallback_exc.provider,
                    details={
                        "reason": "fallback_backend_failed",
                        "primary_error": {
                            "error_code": exc.error_code.value,
                            "backend": exc.backend,
                            "provider": exc.provider,
                            "stage": exc.stage,
                            "details": exc.details,
                        },
                        "fallback_error": {
                            "error_code": fallback_exc.error_code.value,
                            "backend": fallback_exc.backend,
                            "provider": fallback_exc.provider,
                            "stage": fallback_exc.stage,
                            "details": fallback_exc.details,
                        },
                    },
                ) from fallback_exc
            except Exception as fallback_exc:
                raise GenerationError(
                    error_code=GenerationErrorCode.UNKNOWN_BACKEND_ERROR,
                    stage="fallback",
                    retryable=False,
                    fallbackable=False,
                    backend=fallback_backend_id,
                    provider=fallback_backend_id,
                    details={
                        "reason": "fallback_backend_failed",
                        "primary_error": {
                            "error_code": exc.error_code.value,
                            "backend": exc.backend,
                            "provider": exc.provider,
                            "stage": exc.stage,
                            "details": exc.details,
                        },
                        "fallback_error": str(fallback_exc),
                    },
                ) from fallback_exc
        return result.text, result.model, result.usage

    def _call_litellm_impl(
        self,
        prompt: str,
        generation_config: dict,
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, Dict[str, Any]]:
        """Call LLM via litellm with fallback across configured models.

        When channels/YAML are configured, every model goes through the Router
        (which handles per-model key selection, load balancing, and retries).
        In legacy mode, the primary model may use the Router while fallback
        models fall back to direct litellm.completion().

        Args:
            prompt: User prompt text.
            generation_config: Dict with optional keys: temperature, max_output_tokens, max_tokens.
            response_validator: Optional callable that accepts the raw response text and raises
                an exception if the response is unacceptable (e.g. not valid JSON).  When it
                raises, the current model is treated as failed and the next fallback model is
                tried.  If all models fail validation, :class:`_AllModelsFailedError` is raised
                with ``last_response_text`` set to the last raw response received.

        Returns:
            Tuple of (response text, model_used, usage). On success model_used is the full model
            name and usage is a dict with prompt_tokens, completion_tokens, total_tokens.
        """
        config = self._get_runtime_config()
        max_tokens = (
            generation_config.get('max_output_tokens')
            or generation_config.get('max_tokens')
            or 8192
        )
        requested_temperature = generation_config.get('temperature', 0.7)
        requested_timeout = generation_config.get("timeout")

        models_to_try = [config.litellm_model] + (config.litellm_fallback_models or [])
        models_to_try = [m for m in models_to_try if m]

        use_channel_router = self._has_channel_config(config)

        last_error = None
        last_response_text: Optional[str] = None
        last_model: Optional[str] = None
        last_usage: Dict[str, Any] = {}
        effective_system_prompt = system_prompt or self.TEXT_SYSTEM_PROMPT
        router_model_names = set(get_configured_llm_models(config.llm_model_list))
        for model in models_to_try:
            origins = route_deployment_origins(config.llm_model_list, model)
            model_stream = bool(stream and not origins.has_hermes)
            recovery_model_list = config.llm_model_list
            legacy_router_model_list = getattr(self, "_legacy_router_model_list", None) or []
            if legacy_router_model_list and model == config.litellm_model and not use_channel_router:
                recovery_model_list = legacy_router_model_list
            usage_model, usage_provider = resolved_model_provider_identity(model, recovery_model_list)

            try:
                def _attach_usage_audit(
                    usage: Dict[str, Any],
                    messages: List[Dict[str, Any]],
                ) -> Dict[str, Any]:
                    if audit_context is None:
                        return filter_prompt_cache_telemetry(
                            attach_message_hmacs(usage, messages),
                            config,
                        )
                    effective_audit_context = dict(audit_context)
                    effective_audit_context["provider"] = usage_provider
                    effective_audit_context["transport"] = (
                        effective_audit_context.get("transport") or "litellm"
                    )
                    return filter_prompt_cache_telemetry(
                        attach_legacy_message_stability_audit(
                            usage,
                            messages,
                            effective_audit_context,
                        ),
                        config,
                    )

                model_short = model.split("/")[-1] if "/" in model else model
                extra = get_thinking_extra_body(model_short)
                call_kwargs: Dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": effective_system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": max_tokens,
                }
                if requested_timeout not in (None, ""):
                    call_kwargs["timeout"] = requested_timeout
                if extra:
                    call_kwargs["extra_body"] = extra
                uses_router = (
                    (use_channel_router and self._router and model in router_model_names)
                    or (self._router and model == config.litellm_model and not use_channel_router)
                )
                if not uses_router:
                    try:
                        keys = get_api_keys_for_model(model, config)
                    except AttributeError:
                        keys = []
                    if keys:
                        call_kwargs["api_key"] = keys[0]
                    try:
                        call_kwargs.update(extra_litellm_params(model, config))
                    except AttributeError:
                        pass
                call_kwargs = apply_litellm_generation_params(
                    call_kwargs,
                    model,
                    requested_temperature,
                    model_list=recovery_model_list,
                )
                route_context = build_provider_cache_route_context(
                    model=model,
                    provider=usage_provider,
                    call_kwargs=call_kwargs,
                    model_list=recovery_model_list,
                    call_type="analysis",
                )
                hint_result = apply_prompt_cache_hints(call_kwargs, route_context, config)
                call_kwargs = hint_result.call_kwargs
                if requested_timeout not in (None, ""):
                    call_kwargs["timeout"] = requested_timeout
                if hint_result.diagnostics:
                    logger.debug("[PromptCache] %s", hint_result.diagnostics)

                _stream_text: Optional[str] = None
                _stream_usage: Dict[str, Any] = {}

                if model_stream:
                    try:
                        stream_response = call_litellm_with_param_recovery(
                            lambda kwargs: self._dispatch_litellm_completion(
                                model,
                                kwargs,
                                config=config,
                                use_channel_router=use_channel_router,
                                router_model_names=router_model_names,
                            ),
                            model=model,
                            call_kwargs={**call_kwargs, "stream": True},
                            model_list=recovery_model_list,
                            cache_recovery=False,
                            logger=logger,
                        )
                        _stream_text, _stream_usage = self._consume_litellm_stream(
                            stream_response,
                            model=model,
                            usage_model=usage_model,
                            provider=usage_provider,
                            progress_callback=stream_progress_callback,
                        )
                    except _LiteLLMStreamError as exc:
                        safe_error = self._sanitize_litellm_exception_text(exc, config=config, model=model)
                        if exc.partial_received:
                            logger.warning(
                                "[LiteLLM] %s stream failed after partial output, retrying non-stream for same model: %s",
                                model,
                                safe_error,
                            )
                        else:
                            logger.warning(
                                "[LiteLLM] %s stream unavailable before first chunk, falling back to non-stream: %s",
                                model,
                                safe_error,
                            )
                        last_error = RuntimeError(f"{type(exc).__name__}: {safe_error}")
                    except Exception as exc:
                        safe_error = self._sanitize_litellm_exception_text(exc, config=config, model=model)
                        logger.warning(
                            "[LiteLLM] %s stream request failed before first chunk, falling back to non-stream: %s",
                            model,
                            safe_error,
                        )

                if _stream_text is not None:
                    last_response_text = _stream_text
                    last_model = model
                    _stream_usage = _attach_usage_audit(_stream_usage, call_kwargs["messages"])
                    last_usage = _stream_usage
                    if response_validator is not None:
                        response_validator(_stream_text)
                    return _stream_text, model, _stream_usage

                response = call_litellm_with_param_recovery(
                    lambda kwargs: self._dispatch_litellm_completion(
                        model,
                        kwargs,
                        config=config,
                        use_channel_router=use_channel_router,
                        router_model_names=router_model_names,
                    ),
                    model=model,
                    call_kwargs=call_kwargs,
                    model_list=recovery_model_list,
                    logger=logger,
                )

                content = self._extract_completion_text(response)
                if content:
                    usage_messages = None if audit_context is not None else call_kwargs["messages"]
                    usage = self._normalize_usage(
                        extract_usage_payload(response),
                        model=usage_model or model,
                        provider=usage_provider,
                        messages=usage_messages,
                    )
                    if audit_context is not None:
                        usage = _attach_usage_audit(usage, call_kwargs["messages"])
                    last_response_text = content
                    last_model = model
                    last_usage = usage
                    if response_validator is not None:
                        response_validator(content)
                    return (content, model, usage)
                raise ValueError("LLM returned empty response")

            except Exception as e:
                safe_error = self._sanitize_litellm_exception_text(e, config=config, model=model)
                logger.warning("[LiteLLM] %s failed: %s", model, safe_error)
                last_error = RuntimeError(f"{type(e).__name__}: {safe_error}")
                continue

        raise _AllModelsFailedError(
            f"All LLM models failed (tried {len(models_to_try)} model(s)). Last error: {last_error}",
            last_response_text=last_response_text,
            last_model=last_model,
            last_usage=last_usage,
        )

    def generate_text(
        self,
        prompt: str,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> Optional[str]:
        """Public entry point for free-form text generation.

        External callers (e.g. MarketAnalyzer) must use this method instead of
        calling _call_litellm() directly or accessing private attributes such as
        _litellm_available, _router, _model, _use_openai, or _use_anthropic.

        Args:
            prompt:      Text prompt to send to the LLM.
            max_tokens:  Maximum tokens in the response (default 2048).
            temperature: Sampling temperature (default 0.7).

        Returns:
            Response text, or None if the LLM call fails (error is logged).
        """
        try:
            result = self._call_litellm(
                prompt,
                generation_config={"max_tokens": max_tokens, "temperature": temperature},
            )
            if isinstance(result, tuple):
                text, model_used, usage = result
                if should_persist_usage_telemetry(usage):
                    persist_llm_usage(usage, model_used, call_type="market_review")
                return text
            return result
        except GenerationError:
            raise
        except Exception as exc:
            logger.error("[generate_text] LLM call failed: %s", exc)
            return None

    def analyze(
        self, 
        context: Dict[str, Any],
        news_context: Optional[str] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        analysis_context_pack_summary: Optional[str] = None,
    ) -> AnalysisResult:
        """
        分析单只股票
        
        流程：
        1. 格式化输入数据（技术面 + 新闻）
        2. 调用 Gemini API（带重试和模型切换）
        3. 解析 JSON 响应
        4. 返回结构化结果
        
        Args:
            context: 从 storage.get_analysis_context() 获取的上下文数据
            news_context: 预先搜索的新闻内容（可选）

        Returns:
            AnalysisResult 对象
        """
        def _emit_progress(progress: int, message: str) -> None:
            if progress_callback is None:
                return
            try:
                progress_callback(progress, message)
            except Exception as exc:
                logger.debug("[analyzer] progress callback skipped: %s", exc)

        code = context.get('code', 'Unknown')
        config = self._get_runtime_config()
        report_language = normalize_report_language(getattr(config, "report_language", "zh"))
        system_prompt = self._get_analysis_system_prompt(report_language, stock_code=code)
        skill_instructions, default_skill_policy, use_legacy_default_prompt = self._get_skill_prompt_sections()
        
        # 请求前增加延时（防止连续请求触发限流）
        request_delay = config.gemini_request_delay
        if request_delay > 0:
            logger.debug(f"[LLM] waiting {request_delay:.1f}s before request...")
            _emit_progress(
                65,
                f"{code}: 请求前等待 {request_delay:.1f}s" if report_language == "zh"
                else f"{code}: waiting {request_delay:.1f}s before LLM request",
            )
            time.sleep(request_delay)
        
        # 优先从上下文获取股票名称（由 main.py 传入）
        name = context.get('stock_name')
        placeholder_name = f'股票{code}' if report_language == "zh" else f'Stock {code}'
        if not name or name.startswith('股票') or name.lower().startswith('stock '):
            # 备选：从 realtime 中获取
            if 'realtime' in context and context['realtime'].get('name'):
                name = context['realtime']['name']
            else:
                # 最后从映射表获取
                name = STOCK_NAME_MAP.get(code, placeholder_name)

        backend_error = self.get_generation_backend_config_error()
        if backend_error is not None and not self._can_use_generation_fallback(backend_error):
            details = backend_error.details or {}
            field = str(details.get("field") or "GENERATION_BACKEND")
            requested_backend = str(details.get("requested_backend") or backend_error.backend)
            reason = str(details.get("reason") or backend_error.error_code.value)
            if report_language == "en":
                summary = (
                    "AI analysis is unavailable because the generation backend "
                    f"cannot start: {backend_error.error_code.value}."
                )
                risk_warning = (
                    f"Check {field}={requested_backend} ({reason}) or set a valid "
                    "backend/fallback before retrying."
                )
            elif report_language == "ko":
                summary = (
                    "생성 백엔드를 시작할 수 없어 AI 분석을 사용할 수 없습니다: "
                    f"{backend_error.error_code.value}."
                )
                risk_warning = (
                    f"{field}={requested_backend} ({reason})를 확인하거나 유효한 "
                    "백엔드/폴백을 설정한 뒤 다시 시도하세요."
                )
            else:
                summary = (
                    "AI 分析功能不可用：生成后端无法启动，"
                    f"{backend_error.error_code.value}。"
                )
                risk_warning = (
                    f"请检查 {field}={requested_backend}（{reason}），"
                    "或配置有效后端/回退后重试。"
                )
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction=localize_trend_prediction('sideways', report_language),
                operation_advice=localize_operation_advice('hold', report_language),
                confidence_level=localize_confidence_level('low', report_language),
                analysis_summary=summary,
                risk_warning=risk_warning,
                success=False,
                error_message=(
                    f"{backend_error.error_code.value}: {field}={requested_backend}"
                ),
                model_used=None,
                report_language=report_language,
            )

        # 如果模型不可用，返回默认结果
        if not self.is_available():
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction=localize_trend_prediction('sideways', report_language),
                operation_advice=localize_operation_advice('hold', report_language),
                confidence_level=localize_confidence_level('low', report_language),
                analysis_summary=_localized_text(
                    report_language,
                    en='AI analysis is unavailable because no API key is configured.',
                    zh='AI 分析功能未启用（未配置 API Key）',
                    ko='API 키가 설정되지 않아 AI 분석을 사용할 수 없습니다.',
                ),
                risk_warning=_localized_text(
                    report_language,
                    en='Configure an LLM API key (GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY) and retry.',
                    zh='请配置 LLM API Key（GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY）后重试',
                    ko='LLM API 키(GEMINI_API_KEY/ANTHROPIC_API_KEY/OPENAI_API_KEY)를 설정한 뒤 다시 시도하세요.',
                ),
                success=False,
                error_message=_localized_text(
                    report_language,
                    en='LLM API key is not configured',
                    zh='LLM API Key 未配置',
                    ko='LLM API 키가 설정되지 않았습니다',
                ),
                model_used=None,
                report_language=report_language,
            )
        
        try:
            # 格式化输入（包含技术面数据和新闻）
            prompt = self._format_prompt(
                context,
                name,
                news_context,
                report_language=report_language,
                analysis_context_pack_summary=analysis_context_pack_summary,
            )
            legacy_audit_context = {
                "language": report_language,
                "market_group": _legacy_market_group(code),
                "analysis_mode": "stock_analysis",
                "legacy_prompt_mode": "legacy_default" if use_legacy_default_prompt else "skill_aware",
                "skill_config": {
                    "skill_instructions": skill_instructions,
                    "default_skill_policy": default_skill_policy,
                    "use_legacy_default_prompt": use_legacy_default_prompt,
                },
                "transport": "litellm",
                "dynamic_markers": _legacy_audit_marker_specs(
                    context,
                    code=code,
                    stock_name=name,
                    report_language=report_language,
                    news_context=news_context,
                    analysis_context_pack_summary=analysis_context_pack_summary,
                ),
            }
            
            config = self._get_runtime_config()
            backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
            model_name = config.litellm_model or "unknown"
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                model_name = backend_id
                legacy_audit_context["transport"] = backend_id
            logger.info(f"========== AI analysis {name}({code}) ==========")
            logger.info(f"[LLM config] model: {model_name}")
            logger.info(f"[LLM config] prompt length: {len(prompt)} chars")
            logger.info(f"[LLM config] news included: {'yes' if news_context else 'no'}")

            # 本地 CLI backend 是进程执行能力，不记录完整 prompt。
            if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                prompt_preview = redact_diagnostic_text(prompt, limit=500)
            else:
                prompt_preview = prompt[:500] + "..." if len(prompt) > 500 else prompt
            logger.info(f"[LLM prompt preview]\n{prompt_preview}")
            if backend_id not in LOCAL_CLI_GENERATION_BACKEND_IDS:
                logger.debug(f"=== Full prompt ({len(prompt)} chars) ===\n{prompt}\n=== End Prompt ===")

            # 设置生成配置
            # 输出上限：思考型模型（adaptive thinking）的思考 token 计入输出预算，
            # 8192 会导致仪表盘 JSON 被截断并触发二次调用；默认 16384，可用 ANTHROPIC_MAX_TOKENS 覆盖。
            generation_config = {
                "temperature": config.llm_temperature,
                "max_output_tokens": max(
                    int(getattr(config, "anthropic_max_tokens", 8192) or 8192), 16384
                ),
            }

            logger.info(f"[LLM call] calling {model_name}...")
            _emit_progress(
                68,
                f"{name}: 请求已发送，等待响应" if report_language == "zh"
                else f"{name}: LLM request sent, waiting for response",
            )

            # 使用 litellm 调用（支持完整性校验重试）
            current_prompt = prompt
            retry_count = 0
            max_retries = config.report_integrity_retry if config.report_integrity_enabled else 0

            while True:
                start_time = time.time()
                try:
                    response_text, model_used, llm_usage = self._call_litellm(
                        current_prompt,
                        generation_config,
                        system_prompt=system_prompt,
                        stream=True,
                        stream_progress_callback=stream_progress_callback,
                        response_validator=self._validate_json_response,
                        audit_context=legacy_audit_context,
                    )
                except _AllModelsFailedError as exc:
                    if exc.last_response_text is not None:
                        logger.warning(
                            "[LLM JSON] %s(%s): all models returned invalid JSON, using text fallback",
                            name,
                            code,
                        )
                        response_text = exc.last_response_text
                        model_used = exc.last_model
                        llm_usage = exc.last_usage
                    else:
                        raise
                elapsed = time.time() - start_time

                # 记录响应信息
                logger.info(
                    f"[LLM response] {model_name} responded in {elapsed:.2f}s, response length {len(response_text)} chars"
                )
                if backend_id in LOCAL_CLI_GENERATION_BACKEND_IDS:
                    response_preview = redact_diagnostic_text(response_text, limit=300)
                else:
                    response_preview = response_text[:300] + "..." if len(response_text) > 300 else response_text
                logger.info(f"[LLM response preview]\n{response_preview}")
                if backend_id not in LOCAL_CLI_GENERATION_BACKEND_IDS:
                    logger.debug(
                        f"=== {model_name} full response ({len(response_text)} chars) ===\n{response_text}\n=== End Response ==="
                    )
                # Keep parser/retry progress monotonic so task progress/message never "goes backward".
                parse_progress = min(99, 93 + retry_count * 2)
                _emit_progress(
                    parse_progress,
                    f"{name}: 已收到 LLM 响应，正在解析 JSON" if report_language == "zh"
                    else f"{name}: LLM response received, parsing JSON",
                )

                # 解析响应
                result = self._parse_response(response_text, code, name)
                result.raw_response = response_text
                result.search_performed = bool(news_context)
                result.market_snapshot = self._build_market_snapshot(context)
                result.model_used = model_used
                result.report_language = report_language
                normalize_chip_structure_availability(result, context.get("chip"))

                # 内容完整性校验（可选）
                if not config.report_integrity_enabled:
                    break
                require_phase_decision = isinstance(context.get("market_phase_context"), dict)
                pass_integrity, missing_fields = self._check_content_integrity(
                    result,
                    require_phase_decision=require_phase_decision,
                )
                if pass_integrity:
                    break
                if retry_count < max_retries:
                    current_prompt = self._build_integrity_retry_prompt(
                        prompt,
                        response_text,
                        missing_fields,
                        report_language=report_language,
                    )
                    retry_count += 1
                    logger.info(
                        "[LLM integrity] missing mandatory fields %s, completion retry #%d",
                        missing_fields,
                        retry_count,
                    )
                    retry_progress = min(99, 92 + retry_count * 2)
                    _emit_progress(
                        retry_progress,
                        f"{name}: 报告字段不完整，正在补全重试 ({retry_count}/{max_retries})" if report_language == "zh"
                        else f"{name}: report fields incomplete, retrying completion ({retry_count}/{max_retries})",
                    )
                else:
                    self._apply_placeholder_fill(result, missing_fields)
                    logger.warning(
                        "[LLM integrity] missing mandatory fields %s filled with placeholders; pipeline continues",
                        missing_fields,
                    )
                    break

            if should_persist_usage_telemetry(llm_usage):
                persist_llm_usage(llm_usage, model_used, call_type="analysis", stock_code=code)

            logger.info(f"[LLM parse] {name}({code}) analysis complete: {result.trend_prediction}, score {result.sentiment_score}")

            return result
            
        except Exception as e:
            safe_error = self._sanitize_hermes_exception_text(e)
            logger.error("AI analysis %s(%s) failed: %s", name, code, safe_error)
            return AnalysisResult(
                code=code,
                name=name,
                sentiment_score=50,
                trend_prediction=localize_trend_prediction('sideways', report_language),
                operation_advice=localize_operation_advice('hold', report_language),
                confidence_level=localize_confidence_level('low', report_language),
                analysis_summary=_localized_text(
                    report_language,
                    en=f'Analysis failed: {safe_error[:100]}',
                    zh=f'分析过程出错: {safe_error[:100]}',
                    ko=f'분석 중 오류가 발생했습니다: {safe_error[:100]}',
                ),
                risk_warning=_localized_text(
                    report_language,
                    en='Analysis failed. Please retry later or review manually.',
                    zh='分析失败，请稍后重试或手动分析',
                    ko='분석에 실패했습니다. 잠시 후 다시 시도하거나 수동으로 검토하세요.',
                ),
                success=False,
                error_message=safe_error,
                model_used=None,
                report_language=report_language,
            )
    
    def _format_prompt(
        self, 
        context: Dict[str, Any], 
        name: str,
        news_context: Optional[str] = None,
        report_language: str = "zh",
        analysis_context_pack_summary: Optional[str] = None,
    ) -> str:
        """
        格式化分析提示词（决策仪表盘 v2.0）
        
        包含：技术指标、实时行情（量比/换手率）、筹码分布、趋势分析、新闻
        
        Args:
            context: 技术面数据上下文（包含增强数据）
            name: 股票名称（默认值，可能被上下文覆盖）
            news_context: 预先搜索的新闻内容
        """
        code = context.get('code', 'Unknown')
        report_language = normalize_report_language(report_language)
        _, _, use_legacy_default_prompt = self._get_skill_prompt_sections()
        
        # 优先使用上下文中的股票名称（从 realtime_quote 获取）
        stock_name = context.get('stock_name', name)
        placeholder_name = f'股票{code}' if report_language == "zh" else f'Stock {code}'
        if not stock_name or stock_name == f'股票{code}' or stock_name == f'Stock {code}':
            stock_name = STOCK_NAME_MAP.get(code, placeholder_name)
            
        today = context.get('today', {})
        unknown_text = get_unknown_text(report_language)
        no_data_text = get_no_data_text(report_language)
        quote_section_title, close_price_label = _phase_aware_quote_labels(context)
        hide_regular_session_ohlc = _should_hide_regular_session_ohlc(context)
        realtime_overlay_quote = hide_regular_session_ohlc and _today_has_realtime_overlay(today)
        pct_chg_label = "Real-time change %" if realtime_overlay_quote else "Change %"
        volume_label = "Real-time volume" if realtime_overlay_quote else "Volume"
        amount_label = "Real-time turnover" if realtime_overlay_quote else "Turnover"
        quote_rows = [
            f"| {close_price_label} | {today.get('close', 'N/A')} |",
        ]
        if not hide_regular_session_ohlc:
            quote_rows.extend(
                [
                    f"| Open | {today.get('open', 'N/A')} |",
                    f"| High | {today.get('high', 'N/A')} |",
                    f"| Low | {today.get('low', 'N/A')} |",
                ]
            )
        quote_rows.extend(
            [
                f"| {pct_chg_label} | {today.get('pct_chg', 'N/A')}% |",
                f"| {volume_label} | {self._format_volume(today.get('volume'))} |",
                f"| {amount_label} | {self._format_amount(today.get('amount'))} |",
            ]
        )
        quote_rows_text = "\n".join(quote_rows)

        # Next earnings date (US only, injected by the pipeline; row omitted when absent)
        earnings_calendar = context.get('earnings_calendar')
        earnings_row = ""
        earnings_note = ""
        if isinstance(earnings_calendar, dict):
            next_earnings_date = earnings_calendar.get('next_earnings_date')
            days_until = earnings_calendar.get('days_until')
            if next_earnings_date is not None and days_until is not None:
                earnings_row = f"\n| Next earnings | {next_earnings_date} (in {days_until} days) |"
                try:
                    if 0 <= int(days_until) <= 7:
                        earnings_note = (
                            f"\n> ⚠️ Earnings are imminent (in {days_until} days): earnings-event volatility risk must be "
                            "factored into `p_up`, the position advice, and `risk_alerts`.\n"
                        )
                except (TypeError, ValueError):
                    pass

        # ========== Build the decision-dashboard input ==========
        prompt = f"""# Decision Dashboard Analysis Request

## 📊 Stock Basics
| Item | Value |
|------|------|
| Stock code | **{code}** |
| Stock name | **{stock_name}** |
| Analysis date | {context.get('date', unknown_text)} |{earnings_row}
{earnings_note}
---
"""
        prompt += format_market_phase_prompt_section(
            context.get("market_phase_context"),
            report_language=report_language,
        )
        daily_market_context_section = format_daily_market_context_prompt_section(
            context.get("daily_market_context"),
            report_language=report_language,
        )
        if daily_market_context_section:
            prompt += daily_market_context_section
        market_structure_section = format_market_structure_prompt_section(
            context.get("market_structure_context"),
            report_language=report_language,
        )
        if market_structure_section:
            prompt += market_structure_section
        if isinstance(analysis_context_pack_summary, str) and analysis_context_pack_summary:
            prompt += analysis_context_pack_summary
        prompt += f"""

## 📈 Technical Data

### {quote_section_title}
| Metric | Value |
|------|------|
{quote_rows_text}

### Moving-average system (key judgment indicators)
| MA | Value | Note |
|------|------|------|
| MA5 | {today.get('ma5', 'N/A')} | Short-term trend line |
| MA10 | {today.get('ma10', 'N/A')} | Short/medium-term trend line |
| MA20 | {today.get('ma20', 'N/A')} | Medium-term trend line |
| MA pattern | {context.get('ma_status', unknown_text)} | Bullish/Bearish/Tangled |
"""

        # Real-time quote enrichment (volume ratio, turnover rate, etc.)
        if 'realtime' in context:
            rt = context['realtime']
            prompt += f"""
### Real-time quote enrichment
| Metric | Value | Interpretation |
|------|------|------|
| Current price | {rt.get('price', 'N/A')} | |
| **Volume ratio** | **{rt.get('volume_ratio', 'N/A')}** | {rt.get('volume_ratio_desc', '')} |
| **Turnover rate** | **{rt.get('turnover_rate', 'N/A')}%** | |
| PE (trailing/dynamic) | {rt.get('pe_ratio', 'N/A')} | |
| PB | {rt.get('pb_ratio', 'N/A')} | |
| Total market cap | {self._format_amount(rt.get('total_mv'))} | |
| Float market cap | {self._format_amount(rt.get('circ_mv'))} | |
| 60-day change | {rt.get('change_60d', 'N/A')}% | Medium-term performance |
"""

        # Financial report and dividends (value-investing perspective)
        fundamental_context = context.get("fundamental_context") if isinstance(context, dict) else None
        earnings_block = (
            fundamental_context.get("earnings", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        earnings_data = (
            earnings_block.get("data", {})
            if isinstance(earnings_block, dict)
            else {}
        )
        financial_report = (
            earnings_data.get("financial_report", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        dividend_metrics = (
            earnings_data.get("dividend", {})
            if isinstance(earnings_data, dict)
            else {}
        )
        if isinstance(financial_report, dict) or isinstance(dividend_metrics, dict):
            financial_report = financial_report if isinstance(financial_report, dict) else {}
            dividend_metrics = dividend_metrics if isinstance(dividend_metrics, dict) else {}
            ttm_yield = dividend_metrics.get("ttm_dividend_yield_pct", "N/A")
            ttm_cash = dividend_metrics.get("ttm_cash_dividend_per_share", "N/A")
            ttm_count = dividend_metrics.get("ttm_event_count", "N/A")
            report_date = financial_report.get("report_date", "N/A")
            prompt += f"""
### Financial report and dividends (value-investing perspective)
| Metric | Value | Note |
|------|------|------|
| Latest report period | {report_date} | From structured financial-report fields |
| Revenue | {financial_report.get('revenue', 'N/A')} | |
| Net profit (to parent) | {financial_report.get('net_profit_parent', 'N/A')} | |
| Operating cash flow | {financial_report.get('operating_cash_flow', 'N/A')} | |
| ROE | {financial_report.get('roe', 'N/A')} | |
| TTM cash dividend per share | {ttm_cash} | Cash dividends only, pre-tax |
| TTM dividend yield | {ttm_yield} | Formula: TTM cash dividend per share / current price x 100% |
| TTM dividend events | {ttm_count} | |

> If any field above is N/A or missing, state explicitly "data unavailable, cannot judge"; never fabricate.
"""

        capital_flow_block = (
            fundamental_context.get("capital_flow", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        capital_flow_data = (
            capital_flow_block.get("data", {})
            if isinstance(capital_flow_block, dict)
            else {}
        )
        stock_flow = (
            capital_flow_data.get("stock_flow", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        sector_flow = (
            capital_flow_data.get("sector_rankings", {})
            if isinstance(capital_flow_data, dict)
            else {}
        )
        has_capital_flow = (
            isinstance(stock_flow, dict)
            and any(v is not None for v in stock_flow.values())
        ) or (
            isinstance(sector_flow, dict)
            and (sector_flow.get("top") or sector_flow.get("bottom"))
        )
        if has_capital_flow:
            top_sectors = sector_flow.get("top", []) if isinstance(sector_flow, dict) else []
            bottom_sectors = sector_flow.get("bottom", []) if isinstance(sector_flow, dict) else []
            top_sector_text = ", ".join(
                str(item.get("name", "")).strip()
                for item in top_sectors[:3]
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ) or "N/A"
            bottom_sector_text = ", ".join(
                str(item.get("name", "")).strip()
                for item in bottom_sectors[:3]
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ) or "N/A"
            prompt += f"""
### Main-force capital flow (operation-advice filter)
| Metric | Value | Decision meaning |
|------|------|----------|
| Main-force net inflow | {stock_flow.get('main_net_inflow', 'N/A')} | Positive supports, negative suppresses |
| 5-day net inflow | {stock_flow.get('inflow_5d', 'N/A')} | Gauges capital persistence |
| 10-day net inflow | {stock_flow.get('inflow_10d', 'N/A')} | Gauges capital persistence |
| Top inflow sectors | {top_sector_text} | Sector capital resonance reference |
| Top outflow sectors | {bottom_sector_text} | Sector risk reference |

> Capital flow is weighted evidence, not a veto: when price is near resistance and main-force capital keeps flowing out, count it as significant negative evidence in `p_up` and expected value (EV) and state the confirmation needed before chasing; when price is near support without an expanding-volume breakdown, prefer hold-and-watch, sideways, or shakeout-watch.
"""

        # Institutional investor flows (TW chip filter) - tw-only; injected only when the
        # institution block has status='ok' with net values. Other markets report
        # status='not_supported' and are skipped; strictly additive.
        institution_block = (
            fundamental_context.get("institution", {})
            if isinstance(fundamental_context, dict)
            else {}
        )
        institution_data = (
            institution_block.get("data", {})
            if isinstance(institution_block, dict)
            else {}
        )
        if (
            isinstance(institution_block, dict)
            and institution_block.get("status") == "ok"
            and isinstance(institution_data, dict)
            and all(
                institution_data.get(key) is not None
                for key in ("foreign_net", "trust_net", "dealer_net", "total_net")
            )
        ):
            prompt += f"""
### Institutional investor flows (TW chip filter, net buy/sell, unit: shares)
| Institution | Net buy/sell | Decision meaning |
|------|------|----------|
| Foreign investors | {institution_data.get('foreign_net', 'N/A')} | Positive = net buying supports, negative = net selling suppresses |
| Investment trusts | {institution_data.get('trust_net', 'N/A')} | Sustained trust buying often accompanies medium-term longs |
| Dealers | {institution_data.get('dealer_net', 'N/A')} | Short-term hedging / proprietary direction reference |
| Total (three institutions) | {institution_data.get('total_net', 'N/A')} | The most watched chip signal in the TW market |
| Data date | {institution_data.get('date', 'N/A')} | Source: {institution_data.get('source', 'N/A')} |

> The three institutional investors are the TW market's chip filter (the counterpart of A-share main-force capital / dragon-tiger lists, but a different measure that must not be mixed): foreign investors and investment trusts buying together supports price; selling together suppresses it. Judge the TW chip structure from this data; do not write "chip structure: data unavailable" when this data is present.
"""

        # Chip distribution data
        if 'chip' in context:
            chip = context['chip']
            profit_ratio = chip.get('profit_ratio', 0)
            prompt += f"""
### Chip distribution data (efficiency indicators)
| Metric | Value | Health standard |
|------|------|----------|
| **Profit ratio** | **{profit_ratio:.1%}** | Caution at 70-90% |
| Average cost | {chip.get('avg_cost', 'N/A')} | Current price should be 5-15% above |
| 90% chip concentration | {chip.get('concentration_90', 0):.2%} | <15% is concentrated |
| 70% chip concentration | {chip.get('concentration_70', 0):.2%} | |
| Chip status | {chip.get('chip_status', unknown_text)} | |
"""
        else:
            chip_unavailable_text = get_chip_unavailable_text(report_language)
            chip_instruction = (
                "请勿编造获利比例、平均成本或集中度；报告中只说明一次筹码数据不可用，不要把“数据缺失，无法判断”逐字段重复写入 `chip_structure`。"
                if report_language == "zh"
                else "Do not fabricate profit ratio, average cost, or concentration. Mention chip data "
                "unavailability only once in the report; do not repeat per-field no-data text in `chip_structure`."
            )
            prompt += f"""
### Chip distribution data (efficiency indicators)
> {chip_unavailable_text}
> {chip_instruction}
"""

        # Trend analysis (only the implicit built-in bull_trend fallback keeps the legacy wording)
        if 'trend_analysis' in context:
            trend = _sanitize_trend_analysis_for_prompt(
                context['trend_analysis'],
                volume_change_ratio=context.get('volume_change_ratio'),
                report_language=report_language,
            )
            consistency_notes = trend.get('prompt_consistency_notes', [])
            if use_legacy_default_prompt:
                bias_warning = "🚨 Above 5%, do not chase!" if trend.get('bias_ma5', 0) > 5 else "✅ Safe range"
                prompt += f"""
### Trend pre-assessment (based on the trading philosophy)
| Metric | Value | Verdict |
|------|------|------|
| Trend status | {trend.get('trend_status', unknown_text)} | |
| MA alignment | {trend.get('ma_alignment', unknown_text)} | MA5>MA10>MA20 is bullish |
| Trend strength | {trend.get('trend_strength', 0)}/100 | |
| **Bias (MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| Bias (MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| Volume status | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| System signal | {trend.get('buy_signal', unknown_text)} | |
| System score | {trend.get('signal_score', 0)}/100 | |

#### System rationale
**Buy reasons**:
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['None'])) if trend.get('signal_reasons') else '- None'}

**Risk factors**:
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['None'])) if trend.get('risk_factors') else '- None'}
"""
                if consistency_notes:
                    prompt += f"""

**Consistency constraints**:
{chr(10).join('- ' + note for note in consistency_notes)}
"""
            else:
                bias_warning = (
                    "🚨 Large deviation; assess the risk of chasing carefully"
                    if trend.get('bias_ma5', 0) > 5
                    else "✅ Position relatively controllable"
                )
                prompt += f"""
### Technical and structural analysis (reference for activated skills)
| Metric | Value | Note |
|------|------|------|
| Trend status | {trend.get('trend_status', unknown_text)} | |
| MA alignment | {trend.get('ma_alignment', unknown_text)} | Judge structural strength together with the activated skills |
| Trend strength | {trend.get('trend_strength', 0)}/100 | |
| **Price position (MA5)** | **{trend.get('bias_ma5', 0):+.2f}%** | {bias_warning} |
| Price position (MA10) | {trend.get('bias_ma10', 0):+.2f}% | |
| Volume status | {trend.get('volume_status', unknown_text)} | {trend.get('volume_trend', '')} |
| System signal | {trend.get('buy_signal', unknown_text)} | |
| System score | {trend.get('signal_score', 0)}/100 | |

#### System rationale
**Supporting factors**:
{chr(10).join('- ' + r for r in trend.get('signal_reasons', ['None'])) if trend.get('signal_reasons') else '- None'}

**Risk factors**:
{chr(10).join('- ' + r for r in trend.get('risk_factors', ['None'])) if trend.get('risk_factors') else '- None'}
"""
                if consistency_notes:
                    prompt += f"""

**Consistency constraints**:
{chr(10).join('- ' + note for note in consistency_notes)}
"""

        # System-computed reference levels (ATR-based, injected by the pipeline via compute_trade_levels;
        # the whole block is omitted when data is insufficient or the geometry is invalid -
        # annotate-not-veto, only an anchor for the model)
        computed_levels = context.get('computed_trade_levels')
        if isinstance(computed_levels, dict):
            lv_entry = computed_levels.get('entry')
            lv_stop = computed_levels.get('stop')
            lv_target = computed_levels.get('target')
            lv_r = computed_levels.get('r_multiple')
            lv_quality = computed_levels.get('quality')
            if (
                lv_quality not in (None, 'insufficient_data', 'invalid')
                and lv_entry is not None
                and lv_stop is not None
                and lv_target is not None
            ):
                lv_atr = computed_levels.get('atr')
                lv_notes = computed_levels.get('notes')
                notes_text = ""
                if isinstance(lv_notes, list) and lv_notes:
                    notes_text = "\n" + "\n".join(f"- {note}" for note in lv_notes if note)
                prompt += f"""
### System-computed reference levels (ATR-based)
| Level | Value | Basis |
|------|------|------|
| Reference entry | {lv_entry} | Current price |
| Reference stop | {lv_stop} | Higher of structural swing low / entry-1.5xATR |
| Reference target | {lv_target} | Lower of resistance / entry+3xATR |
| Reward/risk R | {lv_r if lv_r is not None else 'N/A'} | ATR14={lv_atr if lv_atr is not None else 'N/A'}, quality: {lv_quality} |
{notes_text}
> Anchor the sniper points (sniper_points) and the `ev_contract` entry/stop/target on the system-computed levels above;
> adjustments are allowed with an explicit reason (e.g. a better structural level, an event driver), but the rationale must be written out;
> R and EV in `ev_contract` must be recomputed from the levels you finally give, not copied from this table.
"""

        # Track record (read-only backtest summary injected by the pipeline; omitted on first run without data)
        track_record = context.get('track_record')
        if isinstance(track_record, dict) and (
            track_record.get('overall') or track_record.get('stock')
        ):
            tr_lines = []
            overall_tr = track_record.get('overall')
            if isinstance(overall_tr, dict):
                strict = overall_tr.get('strict_accuracy_pct')
                scored = overall_tr.get('scored_count')
                if strict is not None:
                    sample_text = f" (sample: {scored})" if scored else ""
                    tr_lines.append(f"- Overall strict accuracy: {strict}%{sample_text}")
                brier = overall_tr.get('brier_score')
                if brier is not None:
                    tr_lines.append(f"- p_up calibration Brier score: {brier} (lower is better)")
                advice_breakdown = overall_tr.get('advice_breakdown')
                if isinstance(advice_breakdown, dict) and advice_breakdown:
                    parts = [
                        f"{advice} {bucket.get('strict_accuracy_pct')}% ({bucket.get('total')} calls)"
                        for advice, bucket in advice_breakdown.items()
                        if isinstance(bucket, dict) and bucket.get('strict_accuracy_pct') is not None
                    ]
                    if parts:
                        tr_lines.append(f"- Hit rate by advice: {', '.join(parts)}")
            stock_tr = track_record.get('stock')
            if isinstance(stock_tr, dict) and stock_tr.get('strict_accuracy_pct') is not None:
                stock_scored = stock_tr.get('scored_count')
                stock_sample_text = f" (sample: {stock_scored})" if stock_scored else ""
                tr_lines.append(
                    f"- Strict accuracy on this stock: {stock_tr.get('strict_accuracy_pct')}%{stock_sample_text}"
                )
            calibration_hint = track_record.get('calibration_hint')
            if calibration_hint:
                tr_lines.append(f"- Calibration hint: {calibration_hint}")
            if tr_lines:
                prompt += f"""
### 📜 Track record (backtest review, for calibration only)
{chr(10).join(tr_lines)}

> The above is the backtested performance of your (this system's) past conclusions. Use it to calibrate this `p_up` and confidence level:
> do not change the buy/sell direction because of the track record, but probability labels should converge toward the real hit rate.
"""

        # Lessons from the system's own closed paper trades (post-mortem loop).
        # Annotate-not-veto: calibration input, never a direction override.
        trade_lessons = context.get('trade_lessons')
        if isinstance(trade_lessons, dict) and trade_lessons.get('lessons'):
            lesson_lines = []
            for item in trade_lessons['lessons'][:8]:
                if not isinstance(item, dict) or not item.get('lesson'):
                    continue
                r_txt = f", {item['r_realized']:+.1f}R" if isinstance(item.get('r_realized'), (int, float)) else ""
                tag = f"{item.get('symbol', '?')} {item.get('direction', '')} {item.get('when', '')}{r_txt}".strip()
                lesson_lines.append(f"- [{item.get('category', 'lesson')}] ({tag}) {item['lesson']}")
            if lesson_lines:
                prompt += f"""
### 🧠 Lessons from this system's own closed trades (post-mortem review)
{chr(10).join(lesson_lines)}

> These are process lessons distilled from this system's OWN executed paper trades. Apply the ones relevant to this setup — especially to entry placement, stop placement, and target realism — and say so in the rationale when one changes your levels.
> They are calibration, not vetoes: do not become blanket-timid, and do not change an otherwise sound conclusion's direction because of them.
"""

        # Day-over-day comparison
        if 'yesterday' in context:
            volume_change = context.get('volume_change_ratio', 'N/A')
            prompt += f"""
### Volume and price change
- Volume vs. previous day: {volume_change}x
- Price vs. previous day: {context.get('price_change_ratio', 'N/A')}%
"""
            parsed_volume_change = _safe_float(volume_change, default=math.nan)
            if math.isfinite(parsed_volume_change) and parsed_volume_change > 10:
                prompt += """
- ⚠️ Abnormal volume alert: volume is more than 10x the previous day, possibly due to abnormal data or a one-off spike; it must be down-weighted and must not be treated mechanically as strong confirmation
"""

        # News search results (key section)
        news_window_days: Optional[int] = None
        context_window = context.get("news_window_days")
        try:
            if context_window is not None:
                parsed_window = int(context_window)
                if parsed_window > 0:
                    news_window_days = parsed_window
        except (TypeError, ValueError):
            news_window_days = None

        if news_window_days is None:
            prompt_config = self._get_runtime_config()
            news_window_days = resolve_news_window_days(
                news_max_age_days=getattr(prompt_config, "news_max_age_days", 3),
                news_strategy_profile=getattr(prompt_config, "news_strategy_profile", "short"),
            )
        prompt += """
---

## 📰 News Intelligence
"""
        if news_context:
            prompt += f"""
Below are the news search results for **{stock_name}({code})** from the past {news_window_days} days. Focus on extracting:
1. 🚨 **Risk alerts**: insider selling, penalties, negative news
2. 🎯 **Positive catalysts**: earnings, contracts, policy
3. 📊 **Earnings outlook**: guidance, preliminary results
4. 🕒 **Time rules (mandatory)**:
   - Every item written to `risk_alerts` / `positive_catalysts` / `latest_news` must carry a specific date (YYYY-MM-DD)
   - Ignore any news outside the {news_window_days}-day window
   - Ignore any news whose publication date is unknown or cannot be determined

```
{news_context}
```
"""
        else:
            prompt += """
No recent news was found for this stock. Base the analysis primarily on the technical data.
"""

        # Missing-data warning
        if context.get('data_missing'):
            prompt += """
⚠️ **Missing data warning**
Due to API limitations, complete real-time quotes and technical indicators are currently unavailable.
**Ignore the N/A values in the tables above** and focus on the news in **[📰 News Intelligence]** for fundamental and sentiment analysis.
When answering technical questions (e.g. moving averages, bias), state "data unavailable, cannot judge" directly. **Never fabricate data.**
"""

        # Explicit output requirements
        prompt += f"""
---

## ✅ Analysis Task

Produce the **Decision Dashboard** for **{stock_name}({code})**, strictly in the JSON format.
"""
        if context.get('is_index_etf'):
            prompt += """
> ⚠️ **Index/ETF analysis constraints**: this instrument is an index-tracking ETF or a market index.
> - Risk analysis should only cover: **index trend, tracking error, market liquidity**
> - Never include the fund company's litigation, reputation, or executive changes in risk alerts
> - Earnings outlook is based on the **overall performance of the index constituents**, not the fund company's financials
> - `risk_alerts` must not contain business risks of the fund manager

"""
        prompt += f"""
### ⚠️ Important: output the stock name in the correct format
The correct format is "Stock name (stock code)", e.g. "Kweichow Moutai (600519)".
If the stock name shown above is "股票{code}"/"Stock {code}" or incorrect, **state the stock's correct full name explicitly** at the start of the analysis.
"""
        if use_legacy_default_prompt:
            prompt += f"""

### Key questions (must be answered explicitly):
1. ❓ Is the MA5>MA10>MA20 bullish alignment satisfied?
2. ❓ Is the current bias within the safe range (<5%)? -- above 5% must be flagged "do not chase"
3. ❓ Does volume confirm (shrinking-volume pullback / expanding-volume breakout)?
4. ❓ Is the chip structure healthy?
5. ❓ Any major negative news? (insider selling, penalties, earnings reversals, etc.)
"""
        else:
            prompt += f"""

### Key questions (must be answered explicitly):
1. ❓ Does the current structure meet the key trigger conditions of the activated skills?
2. ❓ Are the current entry location and reward/risk reasonable? If the deviation is too large, state the waiting condition explicitly
3. ❓ Do volume, volatility, and chip structure support the current conclusion?
4. ❓ Any major negative news or information that conflicts with the skill conclusion?
5. ❓ If the conclusion holds, what exactly are the trigger condition, stop-loss level, and watch points?
"""
        prompt += f"""

### Decision Dashboard requirements:
- **Stock name**: output the correct full company name (e.g. "Kweichow Moutai", not "Stock 600519")
- **Core conclusion**: one sentence that says buy / sell / wait
- **Position-aware advice**: what to do without a position vs. with a position
- **Specific sniper levels**: entry price, stop-loss price, target price (to two decimals)
- **Checklist**: mark every item with ✅/⚠️/❌
- **News time compliance**: `latest_news`, `risk_alerts`, `positive_catalysts` must not contain information older than {news_window_days} days or with unknown dates
- **Technical consistency**: never treat mutually exclusive conclusions such as "bearish alignment" and "bullish alignment" as valid evidence at the same time; if fundamentals/events conflict with technicals, state explicitly "event leads, technicals pending confirmation" or "fundamentals lean bullish, but technicals have not confirmed"

Output the complete Decision Dashboard in JSON format."""

        if report_language == "zh":
            prompt += f"""

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in Simplified Chinese (简体中文).
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- The template uses English example values (e.g. Within this week, Safe, Expanding/Flat/Shrinking, Intraday tracking, Observe, Suggested position: X/10). Never copy them verbatim: write the Chinese equivalents (e.g. "本周内", "安全", "放量/平量/缩量", "盘中跟踪", "观察", "建议仓位：X成").
- When data is missing, state it in Chinese as "{no_data_text}，无法判断".
"""
        elif report_language == "ko":
            prompt += """

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in Korean (한국어).
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common Korean or original listed company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, explain it in Korean.
"""
        else:
            prompt += f"""

### Output language requirements (highest priority)
- Keep every JSON key exactly as defined above; do not translate keys.
- `decision_type` must remain `buy`, `hold`, or `sell`.
- All human-readable JSON values must be in English.
- This includes `stock_name`, `trend_prediction`, `operation_advice`, `confidence_level`, all nested dashboard text, checklist items, and every summary field.
- Use the common English company name when you are confident. If not, keep the listed company name rather than inventing one.
- When data is missing, state it in English as "{no_data_text}, cannot judge".
- No Chinese characters may appear anywhere in the output values.
"""

        return prompt
    
    def _format_volume(self, volume: Optional[float]) -> str:
        """格式化成交量显示"""
        if volume is None:
            return 'N/A'
        if volume >= 1e9:
            return f"{volume / 1e9:.2f}B shares"
        elif volume >= 1e6:
            return f"{volume / 1e6:.2f}M shares"
        elif volume >= 1e3:
            return f"{volume / 1e3:.2f}K shares"
        else:
            return f"{volume:.0f} shares"
    
    def _format_amount(self, amount: Optional[float]) -> str:
        """格式化成交额显示"""
        if amount is None:
            return 'N/A'
        if amount >= 1e9:
            return f"{amount / 1e9:.2f}B"
        elif amount >= 1e6:
            return f"{amount / 1e6:.2f}M"
        elif amount >= 1e3:
            return f"{amount / 1e3:.2f}K"
        else:
            return f"{amount:.0f}"

    def _format_percent(self, value: Optional[float]) -> str:
        """格式化百分比显示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return 'N/A'

    def _format_price(self, value: Optional[float]) -> str:
        """格式化价格显示"""
        if value is None:
            return 'N/A'
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return 'N/A'

    def _build_market_snapshot(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """构建当日行情快照（展示用）"""
        today = context.get('today', {}) or {}
        realtime = context.get('realtime', {}) or {}
        yesterday = context.get('yesterday', {}) or {}

        prev_close = yesterday.get('close')
        close = today.get('close')
        high = today.get('high')
        low = today.get('low')

        amplitude = None
        change_amount = None
        if prev_close not in (None, 0) and high is not None and low is not None:
            try:
                amplitude = (float(high) - float(low)) / float(prev_close) * 100
            except (TypeError, ValueError, ZeroDivisionError):
                amplitude = None
        if prev_close is not None and close is not None:
            try:
                change_amount = float(close) - float(prev_close)
            except (TypeError, ValueError):
                change_amount = None

        snapshot = {
            "date": context.get('date', 'Unknown'),
            "close": self._format_price(close),
            "open": self._format_price(today.get('open')),
            "high": self._format_price(high),
            "low": self._format_price(low),
            "prev_close": self._format_price(prev_close),
            "pct_chg": self._format_percent(today.get('pct_chg')),
            "change_amount": self._format_price(change_amount),
            "amplitude": self._format_percent(amplitude),
            "volume": self._format_volume(today.get('volume')),
            "amount": self._format_amount(today.get('amount')),
        }

        if realtime:
            snapshot.update({
                "price": self._format_price(realtime.get('price')),
                "volume_ratio": realtime.get('volume_ratio', 'N/A'),
                "turnover_rate": self._format_percent(realtime.get('turnover_rate')),
                "source": getattr(realtime.get('source'), 'value', realtime.get('source', 'N/A')),
            })

        return snapshot

    def _check_content_integrity(
        self,
        result: AnalysisResult,
        *,
        require_phase_decision: bool = False,
    ) -> Tuple[bool, List[str]]:
        """Delegate to module-level check_content_integrity."""
        return check_content_integrity(result, require_phase_decision=require_phase_decision)

    def _build_integrity_complement_prompt(self, missing_fields: List[str], report_language: str = "zh") -> str:
        """Build complement instruction for missing mandatory fields."""
        report_language = normalize_report_language(report_language)
        if report_language == "zh":
            lines = ["### 补全要求：请在上方分析基础上补充以下必填内容，并输出完整 JSON："]
            for f in missing_fields:
                if f == "sentiment_score":
                    lines.append("- sentiment_score: 0-100 综合评分")
                elif f == "operation_advice":
                    lines.append("- operation_advice: 买入/加仓/持有/减仓/卖出/观望")
                elif f == "analysis_summary":
                    lines.append("- analysis_summary: 综合分析摘要")
                elif f == "dashboard.core_conclusion.one_sentence":
                    lines.append("- dashboard.core_conclusion.one_sentence: 一句话决策")
                elif f == "dashboard.intelligence.risk_alerts":
                    lines.append("- dashboard.intelligence.risk_alerts: 风险警报列表（可为空数组）")
                elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                    lines.append("- dashboard.battle_plan.sniper_points.stop_loss: 止损价")
                elif f == "dashboard.phase_decision.phase_context":
                    lines.append("- dashboard.phase_decision.phase_context: 公开低敏市场阶段摘要子集")
                elif f == "dashboard.phase_decision.action_window":
                    lines.append("- dashboard.phase_decision.action_window: 阶段化行动窗口")
                elif f == "dashboard.phase_decision.immediate_action":
                    lines.append("- dashboard.phase_decision.immediate_action: 立即行动/等待确认/观察/无盘中动作")
                elif f == "dashboard.phase_decision.watch_conditions":
                    lines.append("- dashboard.phase_decision.watch_conditions: 观察条件数组")
                elif f == "dashboard.phase_decision.next_check_time":
                    lines.append("- dashboard.phase_decision.next_check_time: 下一次检查点或市场本地时间")
                elif f == "dashboard.phase_decision.confidence_reason":
                    lines.append("- dashboard.phase_decision.confidence_reason: 置信度理由与数据限制")
                elif f == "dashboard.phase_decision.data_limitations":
                    lines.append("- dashboard.phase_decision.data_limitations: 阶段/数据质量限制数组")
            return "\n".join(lines)

        lines = ["### Completion requirements: fill the missing mandatory fields below and output the full JSON again:"]
        for f in missing_fields:
            if f == "sentiment_score":
                lines.append("- sentiment_score: integer score from 0 to 100")
            elif f == "operation_advice":
                lines.append("- operation_advice: localized action advice")
            elif f == "analysis_summary":
                lines.append("- analysis_summary: concise analysis summary")
            elif f == "dashboard.core_conclusion.one_sentence":
                lines.append("- dashboard.core_conclusion.one_sentence: one-line decision")
            elif f == "dashboard.intelligence.risk_alerts":
                lines.append("- dashboard.intelligence.risk_alerts: risk alert list (can be empty)")
            elif f == "dashboard.battle_plan.sniper_points.stop_loss":
                lines.append("- dashboard.battle_plan.sniper_points.stop_loss: stop-loss level")
            elif f == "dashboard.phase_decision.phase_context":
                lines.append("- dashboard.phase_decision.phase_context: public market phase summary subset")
            elif f == "dashboard.phase_decision.action_window":
                lines.append("- dashboard.phase_decision.action_window: phase-aware action window")
            elif f == "dashboard.phase_decision.immediate_action":
                lines.append("- dashboard.phase_decision.immediate_action: act now / wait / watch / no intraday action")
            elif f == "dashboard.phase_decision.watch_conditions":
                lines.append("- dashboard.phase_decision.watch_conditions: list of watch conditions")
            elif f == "dashboard.phase_decision.next_check_time":
                lines.append("- dashboard.phase_decision.next_check_time: next check point or market-local time")
            elif f == "dashboard.phase_decision.confidence_reason":
                lines.append("- dashboard.phase_decision.confidence_reason: confidence rationale and data limits")
            elif f == "dashboard.phase_decision.data_limitations":
                lines.append("- dashboard.phase_decision.data_limitations: list of phase/data quality limitations")
        return "\n".join(lines)

    def _build_integrity_retry_prompt(
        self,
        base_prompt: str,
        previous_response: str,
        missing_fields: List[str],
        report_language: str = "zh",
    ) -> str:
        """Build retry prompt using the previous response as the complement baseline."""
        complement = self._build_integrity_complement_prompt(missing_fields, report_language=report_language)
        previous_output = previous_response.strip()
        if normalize_report_language(report_language) == "zh":
            prefix = "### 上一次输出如下，请在该输出基础上补齐缺失字段，并重新输出完整 JSON。不要省略已有字段："
        else:
            prefix = "### The previous output is below. Complete the missing fields based on that output and return the full JSON again. Do not omit existing fields:"
        return "\n\n".join([
            base_prompt,
            prefix,
            previous_output,
            complement,
        ])

    def _apply_placeholder_fill(self, result: AnalysisResult, missing_fields: List[str]) -> None:
        """Delegate to module-level apply_placeholder_fill."""
        apply_placeholder_fill(result, missing_fields)

    def _extract_analysis_json_object(self, response_text: str) -> Tuple[str, Dict[str, Any]]:
        """Extract the single allowed JSON object from an LLM response."""

        text = response_text or ""
        stripped = text.strip()
        if not stripped:
            raise ValueError("empty_response")

        fence_pattern = re.compile(
            r"```[ \t]*(?P<lang>[A-Za-z0-9_-]*)[ \t]*\n?(?P<body>.*?)```",
            flags=re.DOTALL,
        )
        fenced_matches = list(fence_pattern.finditer(text))
        if len(fenced_matches) > 1:
            raise ValueError("ambiguous_json")
        if len(fenced_matches) == 1:
            match = fenced_matches[0]
            outside = (text[:match.start()] + text[match.end():]).strip()
            if outside:
                raise ValueError("ambiguous_json")
            fence_lang = (match.group("lang") or "").strip().lower()
            if fence_lang not in {"", "json"}:
                raise ValueError("ambiguous_json")
            json_str = match.group("body").strip()
            data = self._load_analysis_json_candidate(json_str)
            return json_str, data
        if "```" in text:
            raise ValueError("ambiguous_json")

        try:
            data = self._load_analysis_json_candidate(stripped)
        except json.JSONDecodeError as exc:
            if self._contains_embedded_json_object(text):
                raise ValueError("ambiguous_json") from exc
            raise
        return stripped, data

    def _load_analysis_json_candidate(self, json_str: str) -> Dict[str, Any]:
        """Parse one already-selected JSON candidate, repairing common LLM JSON drift."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            stripped = (json_str or "").strip()
            try:
                _obj, end = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                pass
            else:
                if stripped[end:].strip():
                    raise
            if not (stripped.startswith("{") and stripped.endswith("}")):
                raise
            repaired = self._fix_json_string(stripped)
            data = json.loads(repaired)
        if not isinstance(data, dict):
            raise TypeError("json_root_not_object")
        return data

    @staticmethod
    def _contains_embedded_json_object(text: str) -> bool:
        decoder = json.JSONDecoder()
        count = 0
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                _obj, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            count += 1
            before = text[:index].strip()
            after = text[index + end:].strip()
            if count > 1 or before or after:
                return True
        return False

    def _validate_analysis_minimal_contract(self, data: Dict[str, Any]) -> None:
        try:
            AnalysisReportSchema.model_validate(data)
        except Exception as exc:
            logger.warning(
                "AnalysisReportSchema validation failed; continuing with raw parser contract: %s",
                str(exc)[:200],
            )
        minimal_keys = {
            "sentiment_score",
            "trend_prediction",
            "operation_advice",
            "analysis_summary",
            "dashboard",
        }
        if not any(key in data for key in minimal_keys):
            raise self._generation_validation_error(
                GenerationErrorCode.SCHEMA_VALIDATION_FAILED,
                reason="minimal_contract_failed",
                message="analysis JSON does not contain any minimal parser field",
            )
        if "sentiment_score" in data:
            try:
                int(data.get("sentiment_score", 50))
            except (TypeError, ValueError) as exc:
                raise self._generation_validation_error(
                    GenerationErrorCode.SCHEMA_VALIDATION_FAILED,
                    reason="parser_contract_failed",
                    message="sentiment_score must be integer-compatible",
                ) from exc

    def _generation_validation_error(
        self,
        error_code: GenerationErrorCode,
        *,
        reason: str,
        message: str,
    ) -> GenerationError:
        try:
            backend_id, _fallback_backend_id = self._resolve_generation_backend_config()
        except GenerationError:
            backend_id = "generation_backend"
        return GenerationError(
            error_code=error_code,
            stage="validation",
            retryable=True,
            fallbackable=True,
            backend=backend_id,
            provider=backend_id,
            details={
                "reason": reason,
                "message": message,
            },
        )

    def _parse_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """
        解析 Gemini 响应（决策仪表盘版）
        
        尝试从响应中提取 JSON 格式的分析结果，包含 dashboard 字段
        如果解析失败，尝试智能提取或返回默认结果
        """
        try:
            report_language = normalize_report_language(
                getattr(self._get_runtime_config(), "report_language", "zh")
            )
            try:
                _json_str, data = self._extract_analysis_json_object(response_text)
                self._validate_analysis_minimal_contract(data)
            except Exception as exc:
                logger.warning("Could not extract a unique valid JSON object from the response; marking as parse failure: %s", exc)
                return self._parse_text_response(response_text, code, name)

            # 提取 dashboard 数据
            dashboard = data.get('dashboard', None)
            guardrail_reason = data.get("guardrail_reason") or data.get("downgrade_reason")
            if guardrail_reason and isinstance(dashboard, dict):
                score_calibration = dashboard.get("decision_score_calibration")
                if not isinstance(score_calibration, dict):
                    score_calibration = {}
                    dashboard["decision_score_calibration"] = score_calibration
                score_calibration.setdefault("guardrail_reason", str(guardrail_reason).strip())
            # 归一化 signal_attribution（LLM 可能返回字符串/负数/总和≠100）
            normalize_report_signal_attribution(dashboard)

            # EV 决策契约字段（p_up / ev_contract）：存入 dashboard 元数据，
            # 顶层 schema 允许 extra 字段，不改动 report_schema。
            if isinstance(dashboard, dict):
                p_up_value = _coerce_p_up(data.get('p_up'))
                if p_up_value is not None:
                    dashboard['p_up'] = p_up_value
                ev_contract = data.get('ev_contract')
                if isinstance(ev_contract, dict) and ev_contract:
                    dashboard['ev_contract'] = ev_contract

            # 优先使用 AI 返回的股票名称（如果原名称无效或包含代码）
            ai_stock_name = data.get('stock_name')
            if ai_stock_name and (
                name.startswith('股票')
                or name.lower().startswith('stock ')
                or name == code
                or 'Unknown' in name
            ):
                name = ai_stock_name

            # 解析所有字段，使用默认值防止缺失
            # 解析 decision_type，如果没有则根据 operation_advice 推断
            decision_type = data.get('decision_type', '')
            if not decision_type:
                op = data.get('operation_advice', localize_operation_advice('hold', report_language))
                decision_type = infer_decision_type_from_advice(op, default='hold')

            explicit_action = data.get("action")
            if explicit_action is None and isinstance(dashboard, dict):
                explicit_action = dashboard.get("action")

            result = AnalysisResult(
                code=code,
                name=name,
                # 核心指标
                sentiment_score=int(data.get('sentiment_score', 50)),
                trend_prediction=data.get('trend_prediction', localize_trend_prediction('sideways', report_language)),
                operation_advice=data.get('operation_advice', localize_operation_advice('hold', report_language)),
                decision_type=decision_type,
                confidence_level=localize_confidence_level(
                    data.get('confidence_level', localize_confidence_level('medium', report_language)),
                    report_language,
                ),
                report_language=report_language,
                # 决策仪表盘
                dashboard=dashboard,
                # 走势分析
                trend_analysis=data.get('trend_analysis', ''),
                short_term_outlook=data.get('short_term_outlook', ''),
                medium_term_outlook=data.get('medium_term_outlook', ''),
                # 技术面
                technical_analysis=data.get('technical_analysis', ''),
                ma_analysis=data.get('ma_analysis', ''),
                volume_analysis=data.get('volume_analysis', ''),
                pattern_analysis=data.get('pattern_analysis', ''),
                # 基本面
                fundamental_analysis=data.get('fundamental_analysis', ''),
                sector_position=data.get('sector_position', ''),
                company_highlights=data.get('company_highlights', ''),
                # 情绪面/消息面
                news_summary=data.get('news_summary', ''),
                market_sentiment=data.get('market_sentiment', ''),
                hot_topics=data.get('hot_topics', ''),
                # 综合
                analysis_summary=data.get('analysis_summary', _localized_text(
                    report_language, en='Analysis completed', zh='分析完成', ko='분석 완료')),
                key_points=data.get('key_points', ''),
                risk_warning=data.get('risk_warning', ''),
                buy_reason=data.get('buy_reason', ''),
                # 元数据
                search_performed=data.get('search_performed', False),
                data_sources=data.get('data_sources', _localized_text(
                    report_language, en='Technical data', zh='技术面数据', ko='기술적 데이터')),
                success=True,
            )
            return populate_decision_action_fields(
                result,
                explicit_action=explicit_action,
                align_with_score=False,
            )
                
        except json.JSONDecodeError as e:
            logger.warning(f"JSON parsing failed: {e}; marking as parse failure")
            return self._parse_text_response(response_text, code, name)
    
    def _fix_json_string(self, json_str: str) -> str:
        """修复常见的 JSON 格式问题"""
        import re
        
        # 移除注释
        json_str = re.sub(r'//.*?\n', '\n', json_str)
        json_str = re.sub(r'/\*.*?\*/', '', json_str, flags=re.DOTALL)
        
        # 修复尾随逗号
        json_str = re.sub(r',\s*}', '}', json_str)
        json_str = re.sub(r',\s*]', ']', json_str)
        
        # 确保布尔值是小写
        json_str = json_str.replace('True', 'true').replace('False', 'false')
        
        # fix by json-repair
        json_str = repair_json(json_str)
        
        return json_str

    def _validate_json_response(self, text: str) -> None:
        """Validate that *text* contains one parser-compatible JSON object.

        Used as the ``response_validator`` argument to :meth:`_call_litellm` so
        that a JSON-less or unparseable reply from the primary model is treated
        as a model failure and triggers fallback to the next configured model.

        Raises:
            GenerationError: if the response has no unique parser-compatible
                JSON object, the selected JSON candidate cannot be parsed, or
                the parsed object cannot satisfy the minimal parser contract.
        """
        try:
            _json_str, data = self._extract_analysis_json_object(text)
        except ValueError as exc:
            reason = str(exc) or "invalid_json"
            if reason == "ambiguous_json":
                message = "JSON source is ambiguous"
            else:
                message = "No unique JSON object found in LLM response"
            raise self._generation_validation_error(
                GenerationErrorCode.INVALID_JSON,
                reason=reason,
                message=message,
            ) from exc
        except json.JSONDecodeError as exc:
            raise self._generation_validation_error(
                GenerationErrorCode.INVALID_JSON,
                reason="invalid_json",
                message=str(exc)[:200],
            ) from exc
        except Exception as exc:
            raise self._generation_validation_error(
                GenerationErrorCode.INVALID_JSON,
                reason="invalid_json",
                message=str(exc)[:200],
            ) from exc

        self._validate_analysis_minimal_contract(data)
    
    def _parse_text_response(
        self, 
        response_text: str, 
        code: str, 
        name: str
    ) -> AnalysisResult:
        """从纯文本响应中尽可能提取分析信息"""
        report_language = normalize_report_language(
            getattr(self._get_runtime_config(), "report_language", "zh")
        )
        # 尝试识别关键词来判断情绪
        sentiment_score = 50
        trend = localize_trend_prediction('sideways', report_language)
        advice = localize_operation_advice('hold', report_language)
        
        text_lower = response_text.lower()
        
        # 简单的情绪识别
        positive_keywords = [
            '看多', '买入', '上涨', '突破', '强势', '利好', '加仓',
            'bullish', 'buy', 'uptrend', 'breakout', 'catalyst', 'accumulate', 'add position',
        ]
        negative_keywords = [
            '看空', '卖出', '下跌', '跌破', '弱势', '利空', '减仓',
            'bearish', 'sell', 'downtrend', 'breakdown', 'negative news', 'reduce', 'trim',
        ]
        
        positive_count = sum(1 for kw in positive_keywords if kw in text_lower)
        negative_count = sum(1 for kw in negative_keywords if kw in text_lower)
        
        if positive_count > negative_count + 1:
            sentiment_score = 65
            trend = localize_trend_prediction('bullish', report_language)
            advice = localize_operation_advice('buy', report_language)
            decision_type = 'buy'
        elif negative_count > positive_count + 1:
            sentiment_score = 35
            trend = localize_trend_prediction('bearish', report_language)
            advice = localize_operation_advice('sell', report_language)
            decision_type = 'sell'
        else:
            decision_type = 'hold'
        
        # 截取前500字符作为摘要
        summary = response_text[:500] if response_text else _localized_text(
            report_language, en='No analysis result', zh='无分析结果', ko='분석 결과 없음')
        
        result = AnalysisResult(
            code=code,
            name=name,
            sentiment_score=sentiment_score,
            trend_prediction=trend,
            operation_advice=advice,
            decision_type=decision_type,
            confidence_level=localize_confidence_level('low', report_language),
            analysis_summary=summary,
            key_points=_localized_text(
                report_language,
                en='JSON parsing failed; treat this as best-effort output.',
                zh='JSON解析失败，仅供参考',
                ko='JSON 파싱에 실패했습니다. 참고용으로만 사용하세요.',
            ),
            risk_warning=_localized_text(
                report_language,
                en='The result may be inaccurate. Cross-check with other information.',
                zh='分析结果可能不准确，建议结合其他信息判断',
                ko='결과가 부정확할 수 있습니다. 다른 정보와 교차 확인하세요.',
            ),
            raw_response=response_text,
            success=False,
            error_message='LLM response is not valid JSON; analysis result will not be persisted',
            report_language=report_language,
        )
        return populate_decision_action_fields(result, align_with_score=False)
    
    def batch_analyze(
        self, 
        contexts: List[Dict[str, Any]],
        delay_between: float = 2.0
    ) -> List[AnalysisResult]:
        """
        批量分析多只股票
        
        注意：为避免 API 速率限制，每次分析之间会有延迟
        
        Args:
            contexts: 上下文数据列表
            delay_between: 每次分析之间的延迟（秒）
            
        Returns:
            AnalysisResult 列表
        """
        results = []
        
        for i, context in enumerate(contexts):
            if i > 0:
                logger.debug(f"Waiting {delay_between}s before continuing...")
                time.sleep(delay_between)
            
            result = self.analyze(context)
            results.append(result)
        
        return results


# 便捷函数
def get_analyzer() -> GeminiAnalyzer:
    """获取 LLM 分析器实例"""
    return GeminiAnalyzer()


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    # 模拟上下文数据
    test_context = {
        'code': '600519',
        'date': '2026-01-09',
        'today': {
            'open': 1800.0,
            'high': 1850.0,
            'low': 1780.0,
            'close': 1820.0,
            'volume': 10000000,
            'amount': 18200000000,
            'pct_chg': 1.5,
            'ma5': 1810.0,
            'ma10': 1800.0,
            'ma20': 1790.0,
            'volume_ratio': 1.2,
        },
        'ma_status': 'Bullish alignment 📈',
        'volume_change_ratio': 1.3,
        'price_change_ratio': 1.5,
    }
    
    analyzer = GeminiAnalyzer()
    
    if analyzer.is_available():
        print("=== AI analysis test ===")
        result = analyzer.analyze(test_context)
        print(f"Analysis result: {result.to_dict()}")
    else:
        print("LLM API not configured; skipping test")
