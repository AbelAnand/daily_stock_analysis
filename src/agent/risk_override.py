# -*- coding: utf-8 -*-
"""Shared risk override planning for the multi-agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from src.agent.protocols import AgentContext, normalize_decision_signal


_DOWNGRADE_STEPS = {
    "downgrade_one": 1,
    "downgrade_two": 2,
}

# 真正"取消资格"的风险类别：只有这些类别（财务造假 / 退市 / 停牌）才允许
# 触发买入否决。普通的 high/medium 严重度（如大额减持、业绩预亏）不再翻转
# 方向，而是转为强制收紧止损 + 压缩建议仓位。
SEVERE_RISK_CATEGORIES = frozenset({"fraud", "delisting", "halt"})

# 建议仓位系数：风险不改方向，只改仓位大小。severe 一律 0.0。
POSITION_SIZE_FACTORS = {
    "none": 1.0,
    "low": 0.9,
    "medium": 0.6,
    "high": 0.3,
}


def is_severe_risk_category(category: Any) -> bool:
    """Whether one risk-flag category belongs to the disqualifying severe set."""
    return str(category or "").strip().lower() in SEVERE_RISK_CATEGORIES


def position_size_factor_for_risk(risk_level: Any, *, severe: bool = False) -> float:
    """Map a risk level to a suggested position-size multiplier.

    未知的 risk_level 保守地按 medium（0.6）处理；severe 发现直接归零。
    """
    if severe:
        return 0.0
    return POSITION_SIZE_FACTORS.get(
        str(risk_level or "").strip().lower(),
        POSITION_SIZE_FACTORS["medium"],
    )


class DashboardDecisionSignal(str, Enum):
    """Canonical signals used while applying Agent risk controls."""

    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class RiskTrigger(str, Enum):
    """Normalized trigger selected for one risk-control evaluation."""

    NONE = "none"
    RISK_VETO = "risk_veto"
    RISK_DOWNGRADE = "risk_downgrade"


class RiskApplicationReason(str, Enum):
    """Exhaustive internal outcomes of evaluating a risk override."""

    NO_RISK_EVIDENCE = "no_risk_evidence"
    NO_OVERRIDE_TRIGGER = "no_override_trigger"
    OVERRIDE_DISABLED = "override_disabled"
    POST_RISK_SIGNAL_ALREADY_WITHIN_RISK_LIMIT = (
        "post_risk_signal_already_within_risk_limit"
    )
    RISK_VETO_APPLIED = "risk_veto_applied"
    RISK_DOWNGRADE_APPLIED = "risk_downgrade_applied"


_APPLIED_REASONS = frozenset({
    RiskApplicationReason.RISK_VETO_APPLIED,
    RiskApplicationReason.RISK_DOWNGRADE_APPLIED,
})
_VALID_DOWNGRADE_TRANSITIONS = frozenset({
    (DashboardDecisionSignal.BUY, DashboardDecisionSignal.HOLD),
    (DashboardDecisionSignal.BUY, DashboardDecisionSignal.SELL),
    (DashboardDecisionSignal.HOLD, DashboardDecisionSignal.SELL),
})


def classify_risk_application_reason(
    *,
    evidence_present: bool,
    trigger: RiskTrigger,
    override_enabled: bool,
    applied: bool,
) -> RiskApplicationReason:
    """Classify one application from normalized runtime facts."""
    trigger = RiskTrigger(trigger)
    if not evidence_present:
        return RiskApplicationReason.NO_RISK_EVIDENCE
    if trigger == RiskTrigger.NONE:
        return RiskApplicationReason.NO_OVERRIDE_TRIGGER
    if not override_enabled:
        return RiskApplicationReason.OVERRIDE_DISABLED
    if not applied:
        return RiskApplicationReason.POST_RISK_SIGNAL_ALREADY_WITHIN_RISK_LIMIT
    if trigger == RiskTrigger.RISK_VETO:
        return RiskApplicationReason.RISK_VETO_APPLIED
    return RiskApplicationReason.RISK_DOWNGRADE_APPLIED


def validate_risk_application_transition(
    *,
    applied: bool,
    reason: RiskApplicationReason,
    post_risk_signal: DashboardDecisionSignal,
    from_signal: Optional[DashboardDecisionSignal],
    to_signal: Optional[DashboardDecisionSignal],
) -> None:
    """Reject internally contradictory application records."""
    reason = RiskApplicationReason(reason)
    post_risk_signal = DashboardDecisionSignal(post_risk_signal)
    from_signal = DashboardDecisionSignal(from_signal) if from_signal is not None else None
    to_signal = DashboardDecisionSignal(to_signal) if to_signal is not None else None

    if not applied:
        if from_signal is not None or to_signal is not None:
            raise ValueError("non-applied risk override cannot carry a signal transition")
        if reason in _APPLIED_REASONS:
            raise ValueError("applied reason requires applied=True")
        return

    if from_signal is None or to_signal is None:
        raise ValueError("applied risk override requires from_signal and to_signal")
    if from_signal == to_signal:
        raise ValueError("applied risk override must change the signal")
    if to_signal != post_risk_signal:
        raise ValueError("to_signal must match post_risk_signal")
    if reason == RiskApplicationReason.RISK_VETO_APPLIED:
        # 否决仅保留给 severe 类别（造假/退市/停牌）触发的买入拦截：唯一合法
        # 转移是 buy -> hold。medium/high 但非 severe 的风险不再走否决路径，
        # 而是通过 plan.stop_tightening_required 附加强制收紧止损说明。
        if (from_signal, to_signal) != (
            DashboardDecisionSignal.BUY,
            DashboardDecisionSignal.HOLD,
        ):
            raise ValueError(
                "severe risk veto application must change buy to hold"
            )
    elif reason == RiskApplicationReason.RISK_DOWNGRADE_APPLIED:
        if (from_signal, to_signal) not in _VALID_DOWNGRADE_TRANSITIONS:
            raise ValueError("risk downgrade must move to a more conservative signal")
    else:
        raise ValueError("applied risk override requires an applied reason")


@dataclass(frozen=True)
class RiskOverridePlan:
    """Configuration-aware risk override decision shared by summary and executor."""

    evidence_present: bool
    override_enabled: bool
    override_trigger_present: bool
    veto_buy: bool
    adjustment: str
    has_high_flag: bool
    risk_level_high: bool
    current_signal: Optional[str]
    target_signal: Optional[str]
    will_apply: Optional[bool]
    reason: str
    # severe 类别（造假/退市/停牌）风险标记是否存在——否决的唯一标记来源。
    has_severe_flag: bool = False
    # medium 及以上但未构成否决的风险：强制收紧止损，不翻转方向。
    stop_tightening_required: bool = False
    stop_tightening_note: str = ""

    @property
    def trigger(self) -> RiskTrigger:
        """Return the effective trigger using execution precedence."""
        if self.veto_buy and self.current_signal == DashboardDecisionSignal.BUY:
            return RiskTrigger.RISK_VETO
        if self.adjustment in _DOWNGRADE_STEPS:
            return RiskTrigger.RISK_DOWNGRADE
        if self.veto_buy:
            return RiskTrigger.RISK_VETO
        return RiskTrigger.NONE

    def to_low_sensitivity_dict(self) -> Dict[str, Any]:
        """Return a prompt-safe view that does not expose raw risk payloads."""
        return {
            "evidence_present": self.evidence_present,
            "override_enabled": self.override_enabled,
            "override_trigger_present": self.override_trigger_present,
            "veto_buy": self.veto_buy,
            "stop_tightening_required": self.stop_tightening_required,
            "will_apply": self.will_apply,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RiskOverrideApplication:
    """Validated low-sensitivity result of a risk-control evaluation."""

    evidence_present: bool
    override_enabled: bool
    trigger: RiskTrigger
    applied: bool
    reason: RiskApplicationReason
    post_risk_signal: DashboardDecisionSignal
    from_signal: Optional[DashboardDecisionSignal] = None
    to_signal: Optional[DashboardDecisionSignal] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger", RiskTrigger(self.trigger))
        object.__setattr__(self, "reason", RiskApplicationReason(self.reason))
        object.__setattr__(
            self,
            "post_risk_signal",
            DashboardDecisionSignal(self.post_risk_signal),
        )
        if self.from_signal is not None:
            object.__setattr__(self, "from_signal", DashboardDecisionSignal(self.from_signal))
        if self.to_signal is not None:
            object.__setattr__(self, "to_signal", DashboardDecisionSignal(self.to_signal))

        if not self.evidence_present and self.trigger != RiskTrigger.NONE:
            raise ValueError("risk trigger requires risk evidence")
        expected_reason = classify_risk_application_reason(
            evidence_present=self.evidence_present,
            trigger=self.trigger,
            override_enabled=self.override_enabled,
            applied=self.applied,
        )
        if self.reason != expected_reason:
            raise ValueError(
                f"risk application reason must be {expected_reason.value} for the supplied facts"
            )
        validate_risk_application_transition(
            applied=self.applied,
            reason=self.reason,
            post_risk_signal=self.post_risk_signal,
            from_signal=self.from_signal,
            to_signal=self.to_signal,
        )


def build_risk_override_application(plan: RiskOverridePlan) -> RiskOverrideApplication:
    """Build the actual outcome for a plan evaluated against a dashboard signal."""
    if plan.current_signal is None or plan.target_signal is None or plan.will_apply is None:
        raise ValueError("risk override application requires an evaluated current signal")

    current_signal = DashboardDecisionSignal(plan.current_signal)
    target_signal = DashboardDecisionSignal(plan.target_signal)
    reason = classify_risk_application_reason(
        evidence_present=plan.evidence_present,
        trigger=plan.trigger,
        override_enabled=plan.override_enabled,
        applied=plan.will_apply,
    )
    if plan.will_apply:
        return RiskOverrideApplication(
            evidence_present=plan.evidence_present,
            override_enabled=plan.override_enabled,
            trigger=plan.trigger,
            applied=True,
            reason=reason,
            post_risk_signal=target_signal,
            from_signal=current_signal,
            to_signal=target_signal,
        )
    return RiskOverrideApplication(
        evidence_present=plan.evidence_present,
        override_enabled=plan.override_enabled,
        trigger=plan.trigger,
        applied=False,
        reason=reason,
        post_risk_signal=current_signal,
    )


def build_risk_override_plan(
    ctx: AgentContext,
    *,
    current_signal: Any = None,
    override_enabled: bool = True,
) -> RiskOverridePlan:
    """Build the single source of truth for risk override decisions.

    ``risk_level=high`` is risk evidence, but it is not by itself an override
    trigger. Actual execution also depends on ``override_enabled`` and on the
    dashboard signal observed before applying the risk rule.

    否决（veto）只保留给 severe 类别（造假/退市/停牌）或明确的 ``veto_buy`` /
    ``signal_adjustment=veto``。普通 high/medium 严重度不再触发否决，而是置位
    ``stop_tightening_required`` 并附上强制收紧止损说明。
    """
    risk_raw = _latest_risk_raw(ctx)
    adjustment = str(risk_raw.get("signal_adjustment") or "").strip().lower()
    valid_flags = [flag for flag in ctx.risk_flags if isinstance(flag, dict)]
    has_severe_flag = any(
        is_severe_risk_category(flag.get("category"))
        for flag in valid_flags
    )
    has_high_flag = any(
        str(flag.get("severity", "")).strip().lower() == "high"
        for flag in valid_flags
    )
    has_medium_flag = any(
        str(flag.get("severity", "")).strip().lower() == "medium"
        for flag in valid_flags
    )
    risk_level = str(risk_raw.get("risk_level") or "").strip().lower()
    risk_level_high = risk_level == "high"
    veto_buy = (
        bool(risk_raw.get("veto_buy"))
        or adjustment == "veto"
        or has_severe_flag
    )
    has_downgrade = adjustment in _DOWNGRADE_STEPS
    override_trigger_present = veto_buy or has_downgrade
    stop_tightening_required = (
        not veto_buy
        and (
            has_high_flag
            or has_medium_flag
            or risk_level in {"medium", "high"}
        )
    )
    stop_tightening_note = (
        "Risk level is medium or higher without a veto: the stop-loss must be tightened "
        "and the position reduced by the suggested position factor."
        if stop_tightening_required
        else ""
    )
    evidence_present = (
        override_trigger_present
        or risk_level_high
        or has_high_flag
        or stop_tightening_required
    )

    normalized_current = (
        normalize_decision_signal(current_signal)
        if isinstance(current_signal, str)
        else None
    )
    target_signal = normalized_current
    will_apply: Optional[bool]

    if normalized_current is None:
        will_apply = None
    elif not override_enabled or not override_trigger_present:
        will_apply = False
    else:
        if veto_buy and normalized_current == "buy":
            target_signal = "hold"
        elif has_downgrade:
            target_signal = _downgrade_signal(
                normalized_current,
                steps=_DOWNGRADE_STEPS[adjustment],
            )
        will_apply = target_signal != normalized_current

    return RiskOverridePlan(
        evidence_present=evidence_present,
        override_enabled=bool(override_enabled),
        override_trigger_present=override_trigger_present,
        veto_buy=veto_buy,
        adjustment=adjustment,
        has_high_flag=has_high_flag,
        risk_level_high=risk_level_high,
        current_signal=normalized_current,
        target_signal=target_signal,
        will_apply=will_apply,
        reason=_risk_override_reason(
            veto_buy=veto_buy,
            adjustment=adjustment,
            has_severe_flag=has_severe_flag,
            has_high_flag=has_high_flag,
            risk_level_high=risk_level_high,
            stop_tightening_required=stop_tightening_required,
        ),
        has_severe_flag=has_severe_flag,
        stop_tightening_required=stop_tightening_required,
        stop_tightening_note=stop_tightening_note,
    )


def _latest_risk_raw(ctx: AgentContext) -> Dict[str, Any]:
    risk_opinion = next((op for op in reversed(ctx.opinions) if op.agent_name == "risk"), None)
    if risk_opinion and isinstance(risk_opinion.raw_data, dict):
        return risk_opinion.raw_data
    return {}


def _risk_override_reason(
    *,
    veto_buy: bool,
    adjustment: str,
    has_severe_flag: bool,
    has_high_flag: bool,
    risk_level_high: bool,
    stop_tightening_required: bool,
) -> str:
    if has_severe_flag:
        return "severe_risk_flag"
    if veto_buy:
        return "risk_veto"
    if adjustment in _DOWNGRADE_STEPS:
        return adjustment
    if has_high_flag:
        return "high_severity_flag"
    if risk_level_high:
        return "high_risk_evidence"
    if stop_tightening_required:
        return "stop_tightening_advisory"
    return "none"


def _downgrade_signal(signal: str, steps: int = 1) -> str:
    order = ["buy", "hold", "sell"]
    try:
        index = order.index(signal)
    except ValueError:
        return signal
    return order[min(len(order) - 1, index + max(0, steps))]


__all__ = [
    "DashboardDecisionSignal",
    "POSITION_SIZE_FACTORS",
    "RiskApplicationReason",
    "RiskOverrideApplication",
    "RiskOverridePlan",
    "RiskTrigger",
    "SEVERE_RISK_CATEGORIES",
    "build_risk_override_application",
    "build_risk_override_plan",
    "classify_risk_application_reason",
    "is_severe_risk_category",
    "position_size_factor_for_risk",
    "validate_risk_application_transition",
]
