# -*- coding: utf-8 -*-
"""
SkillAggregator — distribution-aware aggregation of skill opinions.

最终信号来自**票型分布规则**而不是置信度加权均值落带：

- 有效方向性多数（≥50% 且多于对立阵营、阵营平均置信度达标）→ 输出该方向；
- 多空对立且无明确多数（真实分歧 / 高离散度）→ hold，并在 rationale 中列出
  分歧双方的 skill；
- 全体中性 / 无多数偏中性 → hold（真实中性共识，不是"均值塌缩"）；
- 证据不足 → hold，且必须携带显式 reason 字段。

置信度加权均值（weighted_score）仍然计算并保留在 raw_data 中，但仅作为
诊断元数据（向后兼容），不再决定最终信号。RiskAgent 的输出是非方向性的
（signal=risk_assessment），永远不会进入方向性分布；它以 risk_context
（veto 标志 / 建议仓位系数 / 风险提示）的形式附着在共识 opinion 上。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent.protocols import AgentContext, AgentOpinion, StrategyConflict, StrategyOpinion
from src.agent.skills.defaults import (
    SKILL_CONSENSUS_AGENT_NAME,
    extract_skill_id,
    is_skill_agent_name,
)
from src.agent.skills.synthesis import (
    ConflictDetector,
    StrategySynthesizer,
    strategy_opinion_from_agent_opinion,
    strategy_signal_score,
)
from src.services.skill_opinion_weight_service import (
    MAX_SKILL_OPINION_WEIGHT_FACTOR,
    MIN_SKILL_OPINION_WEIGHT_FACTOR,
    SkillOpinionWeightService,
)

logger = logging.getLogger(__name__)

_MIN_BACKTEST_SAMPLES = 30

# 仅用于诊断展示的旧分带映射（weighted_score 已退化为元数据）。
_SCORE_TO_SIGNAL = [
    (4.5, "strong_buy"),
    (3.5, "buy"),
    (2.5, "hold"),
    (1.5, "sell"),
    (0.0, "strong_sell"),
]

# 分布决策规则参数：
# - 方向阵营需要 ≥50% 的有效票且多于对立阵营才算"明确多数"；
# - 对立阵营 ≥1/3 时视为真实分歧（divergence），保留 hold；
# - 胜出阵营平均置信度低于该阈值时不输出方向（low_confidence_hold）。
_MAJORITY_RATIO = 0.5
_DIVERGENCE_MINORITY_RATIO = 1.0 / 3.0
_MIN_DIRECTION_CONFIDENCE = 0.35


@dataclass
class AggregationData:
    skill_opinions: List[AgentOpinion] = field(default_factory=list)
    weights: List[float] = field(default_factory=list)
    skill_names: List[str] = field(default_factory=list)
    strategy_opinions: List[StrategyOpinion] = field(default_factory=list)
    weighted_score: float = 3.0
    weighted_confidence: float = 0.0
    insufficient_evidence: bool = False
    conflicts: List[StrategyConflict] = field(default_factory=list)
    final_signal: str = "hold"
    individual_signals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    total_adjustment: float = 0.0
    # --- distribution-aware decision metadata ---
    signal_distribution: Dict[str, int] = field(default_factory=dict)
    direction_counts: Dict[str, int] = field(default_factory=dict)
    score_dispersion: float = 0.0
    decision_rule: str = ""
    decision_reason: str = ""
    disagreement_skills: Dict[str, List[str]] = field(default_factory=dict)


class SkillAggregator:
    """Aggregate multiple skill-agent opinions into one consensus."""

    def __init__(self, *, weight_service: Optional[Any] = None):
        self._weight_service = weight_service

    def aggregate(
        self,
        ctx: AgentContext,
        min_samples: int = _MIN_BACKTEST_SAMPLES,
    ) -> Optional[AgentOpinion]:
        aggregation = self.calculate(ctx.opinions, min_samples=min_samples)
        if aggregation is None:
            return None

        invalid_count = sum(1 for opinion in aggregation.strategy_opinions if opinion.invalid_signal)
        synthesis = StrategySynthesizer().synthesize(
            aggregation.strategy_opinions,
            weighted_score=aggregation.weighted_score,
            final_signal=aggregation.final_signal,
            weighted_confidence=aggregation.weighted_confidence,
            conflicts=aggregation.conflicts,
            insufficient_evidence=aggregation.insufficient_evidence,
            invalid_count=invalid_count,
        )
        consensus = self.build_consensus_opinion(aggregation, synthesis)

        # RiskAgent 输出以非方向性 risk_context 附着在共识上：veto 标志、
        # 建议仓位系数与风险提示。它不进入方向性分布，"medium risk" 只会
        # 压缩建议仓位，不会拖拽方向。
        risk_context = self._extract_risk_context(ctx.opinions)
        if risk_context is not None:
            consensus.raw_data["risk_context"] = risk_context
            consensus.reasoning += (
                f"\n  risk (non-voting): level={risk_context['risk_level']}, "
                f"position_size_factor={risk_context['position_size_factor']:.2f}, "
                f"severe_veto={risk_context['severe_veto']}"
            )
        return consensus

    @staticmethod
    def _extract_risk_context(opinions: List[AgentOpinion]) -> Optional[Dict[str, Any]]:
        """Read the latest RiskAgent opinion as non-directional risk context."""
        from src.agent.risk_override import position_size_factor_for_risk

        risk_opinion = next(
            (op for op in reversed(opinions) if op.agent_name == "risk"),
            None,
        )
        if risk_opinion is None or not isinstance(risk_opinion.raw_data, dict):
            return None
        raw = risk_opinion.raw_data
        risk_level = str(raw.get("risk_level") or "").strip().lower() or "unknown"
        severe_veto = bool(raw.get("veto_buy"))
        factor = raw.get("position_size_factor")
        if (
            not isinstance(factor, (int, float))
            or isinstance(factor, bool)
            or not math.isfinite(float(factor))
            or not 0.0 <= float(factor) <= 1.0
        ):
            # 兼容旧版风险载荷：按 risk_level 推导建议仓位系数。
            factor = position_size_factor_for_risk(risk_level, severe=severe_veto)
        notes = raw.get("risk_notes")
        if not isinstance(notes, list):
            notes = []
        return {
            "risk_level": risk_level,
            "severe_veto": severe_veto,
            "position_size_factor": float(factor),
            "risk_notes": [str(note) for note in notes[:5]],
        }

    def calculate(
        self,
        opinions: List[AgentOpinion],
        min_samples: int = _MIN_BACKTEST_SAMPLES,
    ) -> Optional[AggregationData]:
        skill_opinions = [op for op in opinions if is_skill_agent_name(op.agent_name)]
        if not skill_opinions:
            return None

        skill_ids = [extract_skill_id(op.agent_name) or op.agent_name for op in skill_opinions]
        perf_weights = self._performance_weights(skill_ids)

        weights: List[float] = []
        for op in skill_opinions:
            skill_id = extract_skill_id(op.agent_name) or op.agent_name
            weight = self._compute_weight(
                op,
                min_samples,
                perf_weight=perf_weights.get(skill_id),
            )
            weights.append(weight)

        strategy_opinions = [
            strategy_opinion_from_agent_opinion(op)
            for op in skill_opinions
        ]

        valid_opinions_with_weights = [
            (op, strategy, weight)
            for op, strategy, weight in zip(skill_opinions, strategy_opinions, weights)
            if not strategy.invalid_signal
        ]
        valid_weight_sum = sum(weight for _, _, weight in valid_opinions_with_weights)
        insufficient_evidence = (
            not valid_opinions_with_weights or valid_weight_sum <= 0
        )
        if not insufficient_evidence:
            weighted_score = sum(
                strategy_signal_score(strategy.signal) * weight
                for _, strategy, weight in valid_opinions_with_weights
            ) / valid_weight_sum
            weighted_confidence = sum(
                op.confidence * weight
                for op, _, weight in valid_opinions_with_weights
            ) / valid_weight_sum
        else:
            weighted_score = 3.0
            weighted_confidence = 0.0
        total_adjustment = sum(
            op.raw_data.get("score_adjustment", 0)
            for op, strategy, weight in valid_opinions_with_weights
            if isinstance(op.raw_data.get("score_adjustment"), (int, float))
        )

        if insufficient_evidence:
            # hold 必须携带显式 reason：这里是证据不足，不是"均值落带"。
            final_signal = "hold"
            decision_rule = "insufficient_evidence"
            decision_reason = (
                "insufficient evidence: no valid skill opinion carries "
                "positive weight"
            )
            signal_distribution: Dict[str, int] = {}
            direction_counts = {"bullish": 0, "bearish": 0, "neutral": 0}
            score_dispersion = 0.0
            disagreement_skills: Dict[str, List[str]] = {}
        else:
            (
                final_signal,
                decision_rule,
                decision_reason,
                signal_distribution,
                direction_counts,
                score_dispersion,
                disagreement_skills,
            ) = self._distribution_decision(valid_opinions_with_weights)

        conflicts = ConflictDetector().detect(strategy_opinions, final_signal=final_signal)
        individual_signals = {
            op.agent_name: {
                "signal": strategy.signal,
                "confidence": op.confidence,
                "original_signal": strategy.original_signal,
                "invalid_signal": strategy.invalid_signal,
            }
            for op, strategy in zip(skill_opinions, strategy_opinions)
        }
        return AggregationData(
            skill_opinions=skill_opinions,
            weights=weights,
            skill_names=skill_ids,
            strategy_opinions=strategy_opinions,
            weighted_score=weighted_score,
            weighted_confidence=weighted_confidence,
            insufficient_evidence=insufficient_evidence,
            conflicts=conflicts,
            final_signal=final_signal,
            individual_signals=individual_signals,
            total_adjustment=total_adjustment,
            signal_distribution=signal_distribution,
            direction_counts=direction_counts,
            score_dispersion=score_dispersion,
            decision_rule=decision_rule,
            decision_reason=decision_reason,
            disagreement_skills=disagreement_skills,
        )

    @staticmethod
    def _distribution_decision(
        valid_opinions_with_weights: List[Any],
    ) -> tuple[str, str, str, Dict[str, int], Dict[str, int], float, Dict[str, List[str]]]:
        """Distribution rule: majority direction wins; hold is reserved for
        genuine disagreement / neutral consensus, never for mean collapse.

        Returns (final_signal, decision_rule, decision_reason,
        signal_distribution, direction_counts, score_dispersion,
        disagreement_skills).
        """
        entries = [
            (
                extract_skill_id(op.agent_name) or op.agent_name,
                op.confidence,
                weight,
                strategy_signal_score(strategy.signal),
                strategy.signal,
            )
            for op, strategy, weight in valid_opinions_with_weights
        ]
        total = len(entries)

        signal_distribution: Dict[str, int] = {}
        for _, _, _, _, signal in entries:
            signal_distribution[signal] = signal_distribution.get(signal, 0) + 1

        bullish = [entry for entry in entries if entry[3] >= 4.0]
        bearish = [entry for entry in entries if entry[3] <= 2.0]
        neutral = [entry for entry in entries if 2.0 < entry[3] < 4.0]
        direction_counts = {
            "bullish": len(bullish),
            "bearish": len(bearish),
            "neutral": len(neutral),
        }

        weight_sum = sum(weight for _, _, weight, _, _ in entries)
        if weight_sum > 0:
            mean_score = sum(
                score * weight for _, _, weight, score, _ in entries
            ) / weight_sum
            score_dispersion = math.sqrt(
                sum(
                    weight * (score - mean_score) ** 2
                    for _, _, weight, score, _ in entries
                ) / weight_sum
            )
        else:
            score_dispersion = 0.0

        disagreement_skills = {
            "bullish": [name for name, _, _, _, _ in bullish],
            "bearish": [name for name, _, _, _, _ in bearish],
        }

        base = (signal_distribution, direction_counts, score_dispersion)

        # 1. 无方向性票：真实的中性共识。
        if not bullish and not bearish:
            return (
                "hold",
                "neutral_consensus",
                f"all {total} valid skills voted hold",
                *base,
                {},
            )

        # 2. 多空票数持平：真实分歧，保留 hold 并点名分歧双方。
        if len(bullish) == len(bearish):
            return (
                "hold",
                "divergence_hold",
                "hold due to disagreement: "
                f"bullish={disagreement_skills['bullish']} vs "
                f"bearish={disagreement_skills['bearish']}",
                *base,
                disagreement_skills,
            )

        is_bullish = len(bullish) > len(bearish)
        leading = bullish if is_bullish else bearish
        opposing = bearish if is_bullish else bullish
        direction = "bullish" if is_bullish else "bearish"
        leading_ratio = len(leading) / total

        # 3. 明确多数：≥50% 的有效票同向且多于对立阵营。
        if leading_ratio >= _MAJORITY_RATIO:
            camp_confidence = sum(conf for _, conf, _, _, _ in leading) / len(leading)
            if camp_confidence < _MIN_DIRECTION_CONFIDENCE:
                return (
                    "hold",
                    "low_confidence_hold",
                    f"{len(leading)}/{total} skills agree on {direction} "
                    f"direction but mean camp confidence "
                    f"{camp_confidence:.2f} < {_MIN_DIRECTION_CONFIDENCE}",
                    *base,
                    {},
                )
            # 方向由票数决定；强弱档位由胜出阵营+中性票的加权均值保守判定。
            pool = leading + neutral
            pool_weight = sum(weight for _, _, weight, _, _ in pool)
            if pool_weight > 0:
                pool_score = sum(
                    score * weight for _, _, weight, score, _ in pool
                ) / pool_weight
            else:
                pool_score = sum(score for _, _, _, score, _ in pool) / len(pool)
            if is_bullish:
                final_signal = "strong_buy" if pool_score >= 4.5 else "buy"
            else:
                final_signal = "strong_sell" if pool_score <= 1.5 else "sell"
            return (
                final_signal,
                "majority_direction",
                f"{len(leading)}/{total} skills agree on {direction} direction "
                f"(camp confidence {camp_confidence:.2f}, "
                f"opposing {len(opposing)})",
                *base,
                {},
            )

        # 4. 无多数且对立阵营占比 ≥1/3：高离散度，真实分歧。
        if opposing and len(opposing) / total >= _DIVERGENCE_MINORITY_RATIO:
            return (
                "hold",
                "divergence_hold",
                "hold due to disagreement: "
                f"bullish={disagreement_skills['bullish']} vs "
                f"bearish={disagreement_skills['bearish']}",
                *base,
                disagreement_skills,
            )

        # 5. 其余：中性票占主导，无方向性多数。
        return (
            "hold",
            "no_majority_hold",
            f"no directional majority: {direction_counts}",
            *base,
            {},
        )

    @staticmethod
    def build_consensus_opinion(
        aggregation: AggregationData,
        synthesis: Dict[str, Any],
    ) -> AgentOpinion:
        reasoning_parts = [
            f"Skill consensus from {len(aggregation.skill_opinions)} skills "
            f"({', '.join(aggregation.skill_names)}): weighted score {aggregation.weighted_score:.2f}/5.0 (diagnostic), "
            f"consensus={synthesis['consensus_level']}, conflicts={synthesis['conflict_severity']}({synthesis['conflict_count']})",
            f"  decision_rule={aggregation.decision_rule}: {aggregation.decision_reason}",
        ]
        for opinion, weight in zip(aggregation.skill_opinions, aggregation.weights):
            name = extract_skill_id(opinion.agent_name) or opinion.agent_name
            reasoning_parts.append(f"  - {name}: {opinion.signal} ({opinion.confidence:.0%}) weight={weight:.2f}")

        raw_data = {
            # weighted_score 仅作诊断元数据保留（向后兼容），最终信号来自
            # decision_rule 的分布规则。
            "weighted_score": round(aggregation.weighted_score, 2),
            "total_adjustment": aggregation.total_adjustment,
            "skill_count": len(aggregation.skill_opinions),
            "individual_signals": aggregation.individual_signals,
            "strategy_synthesis": synthesis,
            "conflicts": synthesis["conflicts"],
            "conflict_count": synthesis["conflict_count"],
            "conflict_severity": synthesis["conflict_severity"],
            "consensus_level": synthesis["consensus_level"],
            "signal_distribution": aggregation.signal_distribution,
            "direction_counts": aggregation.direction_counts,
            "score_dispersion": round(aggregation.score_dispersion, 4),
            "decision_rule": aggregation.decision_rule,
            "decision_reason": aggregation.decision_reason,
        }
        if aggregation.disagreement_skills:
            raw_data["disagreement_skills"] = aggregation.disagreement_skills
        if aggregation.final_signal == "hold":
            # hold 必须能解释自己：显式 reason 字段（insufficient / divergence /
            # neutral 等），供渲染层与 DecisionAgent 直接引用。
            raw_data["hold_reason"] = aggregation.decision_reason

        return AgentOpinion(
            agent_name=SKILL_CONSENSUS_AGENT_NAME,
            signal=aggregation.final_signal,
            confidence=synthesis["confidence"],
            reasoning="\n".join(reasoning_parts),
            raw_data=raw_data,
        )

    def _compute_weight(
        self,
        opinion: AgentOpinion,
        min_samples: int,
        perf_weight: Optional[float] = None,
    ) -> float:
        del min_samples  # Retained for compatibility with existing callers.
        base_weight = opinion.confidence
        if (
            isinstance(perf_weight, (int, float))
            and not isinstance(perf_weight, bool)
            and math.isfinite(perf_weight)
            and perf_weight > 0
        ):
            return base_weight * perf_weight
        return base_weight

    def _performance_weights(
        self,
        skill_ids: List[str],
    ) -> Dict[str, float]:
        neutral = {skill_id: 1.0 for skill_id in skill_ids}
        if not self._use_outcome_autoweight():
            return neutral

        try:
            if self._weight_service is None:
                self._weight_service = SkillOpinionWeightService()
            computed = self._weight_service.compute_weights(skill_ids)
            if not isinstance(computed, dict):
                return neutral
            for skill_id in neutral:
                factor = computed.get(skill_id)
                if (
                    isinstance(factor, (int, float))
                    and not isinstance(factor, bool)
                    and math.isfinite(factor)
                    and MIN_SKILL_OPINION_WEIGHT_FACTOR
                    <= factor
                    <= MAX_SKILL_OPINION_WEIGHT_FACTOR
                ):
                    neutral[skill_id] = float(factor)
        except Exception:
            logger.debug(
                "Failed to compute Skill Opinion outcome weights",
                exc_info=True,
            )
        return neutral

    @staticmethod
    def _use_outcome_autoweight() -> bool:
        try:
            from src.config import get_config

            config = get_config()
            return getattr(config, "agent_skill_autoweight", True)
        except Exception:
            logger.debug(
                "Failed to get Outcome autoweight config, defaulting to True",
                exc_info=True,
            )
            return True


StrategyAggregator = SkillAggregator
