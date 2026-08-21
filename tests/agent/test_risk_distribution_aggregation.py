# -*- coding: utf-8 -*-
"""Behavior tests for the non-voting RiskAgent and the distribution-aware
SkillAggregator decision rule.

Covers the contract:
- RiskAgent no longer emits directional votes (buy/sell/strong_sell); its
  output is a severe-set veto flag plus a position-size modifier and notes.
- The aggregator's final signal comes from the vote distribution, not from
  the confidence-weighted mean band; hold is reserved for genuine
  disagreement / neutral consensus / insufficient evidence (with reason).
- The risk veto only fires for the severe categories (fraud/delisting/halt);
  medium/high non-severe risk yields a mandatory stop-tightening note.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.agent.agents.risk_agent import RiskAgent
from src.agent.protocols import AgentContext, AgentOpinion, RISK_ASSESSMENT_SIGNAL
from src.agent.risk_override import (
    RiskApplicationReason,
    RiskTrigger,
    build_risk_override_application,
    build_risk_override_plan,
    position_size_factor_for_risk,
)
from src.agent.skills.aggregator import SkillAggregator


DIRECTIONAL_SIGNALS = {"strong_buy", "buy", "sell", "strong_sell"}


def _risk_agent() -> RiskAgent:
    return RiskAgent(tool_registry=MagicMock(), llm_adapter=MagicMock())


def _risk_json(**overrides) -> str:
    payload = {
        "risk_level": "medium",
        "risk_score": 55,
        "flags": [
            {
                "category": "earnings",
                "severity": "medium",
                "description": "业绩预亏",
                "source": "news",
            }
        ],
        "veto_buy": False,
        "reasoning": "medium risk",
        "signal_adjustment": "none",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ------------------------------------------------------------------
# RiskAgent — no directional vote, position sizing instead
# ------------------------------------------------------------------

def test_risk_agent_medium_risk_does_not_vote_a_direction():
    ctx = AgentContext(stock_code="600519")
    opinion = _risk_agent().post_process(ctx, _risk_json())

    assert opinion is not None
    assert opinion.signal == RISK_ASSESSMENT_SIGNAL
    assert opinion.signal not in DIRECTIONAL_SIGNALS
    assert opinion.raw_data["directional_vote"] is False
    # medium risk shrinks suggested size, it does not veto or vote sell
    assert opinion.raw_data["veto_buy"] is False
    assert opinion.raw_data["position_size_factor"] == pytest.approx(0.6)
    assert opinion.raw_data["risk_notes"] == ["业绩预亏"]
    # flags still propagate to context for downstream risk planning
    assert ctx.risk_flags and ctx.risk_flags[0]["category"] == "earnings"


def test_risk_agent_vetoes_only_for_severe_categories():
    ctx = AgentContext(stock_code="600519")
    opinion = _risk_agent().post_process(
        ctx,
        _risk_json(
            risk_level="high",
            risk_score=90,
            flags=[
                {
                    "category": "fraud",
                    "severity": "high",
                    "description": "财务造假立案",
                    "source": "announcement",
                }
            ],
            veto_buy=True,
            signal_adjustment="veto",
        ),
    )

    assert opinion is not None
    assert opinion.signal == RISK_ASSESSMENT_SIGNAL
    assert opinion.raw_data["veto_buy"] is True
    assert opinion.raw_data["position_size_factor"] == pytest.approx(0.0)
    assert opinion.raw_data["severe_flags"][0]["category"] == "fraud"


def test_risk_agent_demotes_model_veto_without_severe_finding():
    """A model 'veto' backed only by ordinary risk becomes a two-step
    downgrade, not a veto."""
    ctx = AgentContext(stock_code="600519")
    opinion = _risk_agent().post_process(
        ctx,
        _risk_json(
            risk_level="high",
            risk_score=80,
            flags=[
                {
                    "category": "insider",
                    "severity": "high",
                    "description": "大股东减持",
                    "source": "news",
                }
            ],
            veto_buy=True,
            signal_adjustment="veto",
        ),
    )

    assert opinion is not None
    assert opinion.raw_data["veto_buy"] is False
    assert opinion.raw_data["signal_adjustment"] == "downgrade_two"
    assert opinion.raw_data["model_veto_buy"] is True
    assert opinion.raw_data["model_signal_adjustment"] == "veto"
    assert opinion.raw_data["position_size_factor"] == pytest.approx(0.3)


# ------------------------------------------------------------------
# SkillAggregator — distribution rule
# ------------------------------------------------------------------

def _skill_ctx(signals, confidence=0.8):
    ctx = AgentContext(stock_code="600519")
    for index, signal in enumerate(signals):
        ctx.add_opinion(
            AgentOpinion(
                agent_name=f"skill_s{index}",
                signal=signal,
                confidence=confidence,
            )
        )
    return ctx


def test_majority_agreement_outputs_direction_not_mean_band():
    """4-of-6 buy agreement must output buy even though the weighted mean
    (3.33) sits inside the old hold band."""
    ctx = _skill_ctx(["buy", "buy", "buy", "buy", "sell", "sell"])
    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.signal == "buy"
    assert consensus.raw_data["decision_rule"] == "majority_direction"
    assert consensus.raw_data["direction_counts"] == {
        "bullish": 4,
        "bearish": 2,
        "neutral": 0,
    }
    assert consensus.raw_data["signal_distribution"] == {"buy": 4, "sell": 2}
    # the old mean band would have said hold — kept only as diagnostics
    assert 2.5 <= consensus.raw_data["weighted_score"] < 3.5


def test_hold_is_reserved_for_genuine_disagreement():
    ctx = _skill_ctx(["buy", "buy", "buy", "sell", "sell", "sell"])
    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.signal == "hold"
    assert consensus.raw_data["decision_rule"] == "divergence_hold"
    disagreement = consensus.raw_data["disagreement_skills"]
    assert set(disagreement["bullish"]) == {"s0", "s1", "s2"}
    assert set(disagreement["bearish"]) == {"s3", "s4", "s5"}
    # the rationale names the disagreeing skills
    assert "disagreement" in consensus.raw_data["decision_reason"]
    for name in ("s0", "s3"):
        assert name in consensus.raw_data["decision_reason"]
    assert consensus.raw_data["hold_reason"] == consensus.raw_data["decision_reason"]


def test_unanimous_hold_is_neutral_consensus_not_disagreement():
    ctx = _skill_ctx(["hold", "hold", "hold"])
    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.signal == "hold"
    assert consensus.raw_data["decision_rule"] == "neutral_consensus"
    assert consensus.raw_data["hold_reason"]


def test_insufficient_evidence_hold_carries_explicit_reason():
    ctx = _skill_ctx(["buy", "hold"], confidence=0.0)
    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.signal == "hold"
    assert consensus.raw_data["decision_rule"] == "insufficient_evidence"
    assert "insufficient evidence" in consensus.raw_data["hold_reason"]


def test_strong_majority_can_output_strong_signal():
    ctx = _skill_ctx(["strong_buy", "strong_buy", "strong_buy"])
    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.signal == "strong_buy"
    assert consensus.raw_data["decision_rule"] == "majority_direction"


def test_low_confidence_majority_holds_with_reason():
    ctx = _skill_ctx(["buy", "buy", "buy"], confidence=0.1)
    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.signal == "hold"
    assert consensus.raw_data["decision_rule"] == "low_confidence_hold"
    assert consensus.raw_data["hold_reason"]


def test_risk_opinion_never_enters_the_directional_distribution():
    """A medium-risk assessment must not drag direction: it attaches as
    non-voting risk context (position-size modifier + notes) instead."""
    ctx = _skill_ctx(["buy", "buy", "buy", "buy", "sell", "sell"])
    ctx.add_opinion(
        AgentOpinion(
            agent_name="risk",
            signal=RISK_ASSESSMENT_SIGNAL,
            confidence=0.55,
            reasoning="medium risk",
            raw_data={
                "risk_level": "medium",
                "veto_buy": False,
                "position_size_factor": 0.6,
                "risk_notes": ["业绩预亏"],
                "directional_vote": False,
            },
        )
    )

    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    # direction unchanged by risk; only skills are counted
    assert consensus.signal == "buy"
    assert consensus.raw_data["skill_count"] == 6
    assert "risk" not in consensus.raw_data["individual_signals"]
    risk_context = consensus.raw_data["risk_context"]
    assert risk_context["risk_level"] == "medium"
    assert risk_context["severe_veto"] is False
    assert risk_context["position_size_factor"] == pytest.approx(0.6)
    assert risk_context["risk_notes"] == ["业绩预亏"]
    assert "position_size_factor" in consensus.reasoning


def test_legacy_risk_payload_still_yields_position_size_factor():
    ctx = _skill_ctx(["buy", "buy"])
    ctx.add_opinion(
        AgentOpinion(
            agent_name="risk",
            signal="hold",
            confidence=0.5,
            raw_data={"risk_level": "medium"},
        )
    )

    consensus = SkillAggregator().aggregate(ctx)

    assert consensus is not None
    assert consensus.raw_data["risk_context"]["position_size_factor"] == pytest.approx(0.6)


# ------------------------------------------------------------------
# Risk override — severe-set veto and stop tightening
# ------------------------------------------------------------------

def test_severe_flag_vetoes_buy_to_hold():
    ctx = AgentContext(stock_code="600519")
    ctx.add_risk_flag(category="delisting", description="退市风险警示", severity="high")

    plan = build_risk_override_plan(ctx, current_signal="buy")
    application = build_risk_override_application(plan)

    assert plan.has_severe_flag is True
    assert plan.veto_buy is True
    assert plan.reason == "severe_risk_flag"
    assert application.applied is True
    assert application.reason == RiskApplicationReason.RISK_VETO_APPLIED
    assert application.from_signal.value == "buy"
    assert application.to_signal.value == "hold"


def test_high_severity_non_severe_flag_tightens_stops_instead_of_veto():
    ctx = AgentContext(stock_code="600519")
    ctx.add_risk_flag(category="insider", description="大额减持", severity="high")

    plan = build_risk_override_plan(ctx, current_signal="buy")
    application = build_risk_override_application(plan)

    assert plan.veto_buy is False
    assert plan.trigger == RiskTrigger.NONE
    assert plan.stop_tightening_required is True
    assert plan.stop_tightening_note
    assert plan.to_low_sensitivity_dict()["stop_tightening_required"] is True
    assert application.applied is False
    assert application.reason == RiskApplicationReason.NO_OVERRIDE_TRIGGER
    assert application.post_risk_signal.value == "buy"


def test_medium_risk_level_yields_stop_tightening_evidence():
    ctx = AgentContext(stock_code="600519")
    ctx.add_opinion(
        AgentOpinion(
            agent_name="risk",
            signal=RISK_ASSESSMENT_SIGNAL,
            confidence=0.55,
            raw_data={"risk_level": "medium", "veto_buy": False},
        )
    )

    plan = build_risk_override_plan(ctx, current_signal="buy")

    assert plan.veto_buy is False
    assert plan.stop_tightening_required is True
    assert plan.evidence_present is True
    assert plan.reason == "stop_tightening_advisory"


def test_explicit_veto_payload_still_vetoes_for_compat():
    ctx = AgentContext(stock_code="600519")
    ctx.add_opinion(
        AgentOpinion(
            agent_name="risk",
            signal=RISK_ASSESSMENT_SIGNAL,
            confidence=0.9,
            raw_data={"veto_buy": True},
        )
    )

    plan = build_risk_override_plan(ctx, current_signal="buy")

    assert plan.veto_buy is True
    assert plan.reason == "risk_veto"
    assert plan.target_signal == "hold"


def test_position_size_factor_mapping():
    assert position_size_factor_for_risk("none") == pytest.approx(1.0)
    assert position_size_factor_for_risk("low") == pytest.approx(0.9)
    assert position_size_factor_for_risk("medium") == pytest.approx(0.6)
    assert position_size_factor_for_risk("high") == pytest.approx(0.3)
    # unknown level treated conservatively; severe always zero
    assert position_size_factor_for_risk("weird") == pytest.approx(0.6)
    assert position_size_factor_for_risk("none", severe=True) == pytest.approx(0.0)
