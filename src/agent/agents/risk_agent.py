# -*- coding: utf-8 -*-
"""
RiskAgent — dedicated risk screening specialist.

Responsible for:
- Scanning for insider sell-downs, earnings warnings, regulatory actions
- Checking valuation anomalies (PE/PB extremes)
- Evaluating lock-up expiration risks
- Producing risk flags that can override or downgrade signals from other agents

风险输出是**非方向性**的：RiskAgent 不再产出 buy/sell 等方向性投票（也就
不会以 confidence 为权重拉动共识均值）。它的输出只有两种作用方式：

1. **veto**：仅当发现真正"取消资格"的 severe 类别风险（财务造假 / 退市 /
   停牌，见 ``SEVERE_RISK_CATEGORIES``）时置位 ``veto_buy``；
2. **position sizing + 风险提示**：其余风险（包括 medium/high 严重度）只
   压缩建议仓位系数（``position_size_factor``）并附带风险说明，不改方向。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion, RISK_ASSESSMENT_SIGNAL
from src.agent.risk_override import (
    is_severe_risk_category,
    position_size_factor_for_risk,
)
from src.agent.runner import try_parse_json

logger = logging.getLogger(__name__)


class RiskAgent(BaseAgent):
    agent_name = "risk"
    max_steps = 4
    tool_names = [
        "search_stock_news",
        "get_realtime_quote",
        "get_stock_info",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return """\
You are a **Risk Screening Agent** focused exclusively on identifying \
risks and red flags for the given stock.

Your task: search for and evaluate ALL potential risk factors, then \
output a structured JSON risk assessment.

## Mandatory Risk Checks
1. **Insider / Major Shareholder Activity** — sell-downs (减持), pledges
2. **Earnings Warnings** — pre-loss, downward revisions (业绩预亏, 业绩变脸)
3. **Regulatory** — penalties, investigations, violations (监管处罚, 立案调查)
4. **Industry Policy** — headwinds, sector crackdowns
5. **Lock-up Expirations** — large block unlocks within 30 days (解禁)
6. **Valuation Extremes** — PE > 100 or negative, PB > 10 (flag as anomaly)
7. **Technical Warning Signs** — death crosses, breaking key supports

## Severity Levels
- "high": existential or material risk (lawsuits, fraud, massive insider selling)
- "medium": significant concern (earnings miss, lock-up, sector headwind)
- "low": minor or informational (analyst downgrade, minor insider sale)

## Output Format
Return **only** a JSON object:
{
  "risk_level": "high|medium|low|none",
  "risk_score": 0-100,
  "flags": [
    {
      "category": "fraud|delisting|halt|insider|earnings|regulatory|industry|lockup|valuation|technical",
      "severity": "high|medium|low",
      "description": "Clear description of the risk",
      "source": "Where this information came from"
    }
  ],
  "veto_buy": true|false,
  "reasoning": "2-3 sentence overall risk assessment",
  "signal_adjustment": "none|downgrade_one|downgrade_two|veto"
}

## Category & Veto Rules
- Use "fraud" for confirmed/alleged financial fraud (财务造假), "delisting" \
for delisting risk (退市风险, *ST), "halt" for trading halts/suspensions \
(停牌). These are the ONLY disqualifying categories.
- Set "veto_buy": true ONLY when a fraud/delisting/halt finding is present. \
All other risks — even severe insider selling or earnings warnings — must be \
expressed via risk_level / flags / signal_adjustment, and will translate into \
a smaller suggested position size plus mandatory stop tightening, not a \
directional call.
- You are a risk screener, not a trader: do NOT output buy/sell opinions.

Important: be thorough but factual. Only flag risks backed by evidence \
from your search results. Do NOT invent risks.
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        parts = [f"Screen stock **{ctx.stock_code}**"]
        if ctx.stock_name:
            parts[0] += f" ({ctx.stock_name})"
        parts.append("for ALL risk factors listed in your instructions.")
        parts.append("Search for latest news if you haven't received intel data yet.")

        # Feed any existing intel data so the risk agent doesn't redo searches
        if ctx.get_data("intel_opinion"):
            parts.append(f"\n[Existing intel data]\n{json.dumps(ctx.get_data('intel_opinion'), ensure_ascii=False, default=str)}")

        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        parsed = try_parse_json(raw_text)
        if parsed is None:
            logger.warning("[RiskAgent] failed to parse risk JSON")
            return None

        flags: List[Dict[str, Any]] = [
            flag for flag in parsed.get("flags", []) if isinstance(flag, dict)
        ]
        # Propagate structured risk flags to context
        for flag in flags:
            ctx.add_risk_flag(
                category=flag.get("category", "unknown"),
                description=flag.get("description", ""),
                severity=flag.get("severity", "medium"),
            )

        risk_level = str(parsed.get("risk_level") or "none").strip().lower()
        adjustment = str(parsed.get("signal_adjustment") or "none").strip().lower()
        severe_flags = [
            flag for flag in flags if is_severe_risk_category(flag.get("category"))
        ]
        # veto 只保留给 severe 类别（造假/退市/停牌）；模型对普通风险给出的
        # veto 会被降级为 downgrade_two + 强制收紧止损，而不是直接否决。
        severe_veto = bool(severe_flags)

        raw_payload: Dict[str, Any] = dict(parsed)
        raw_payload["model_veto_buy"] = bool(parsed.get("veto_buy"))
        raw_payload["model_signal_adjustment"] = adjustment
        raw_payload["veto_buy"] = severe_veto
        if adjustment == "veto" and not severe_veto:
            raw_payload["signal_adjustment"] = "downgrade_two"
        raw_payload["severe_flags"] = severe_flags
        raw_payload["position_size_factor"] = position_size_factor_for_risk(
            risk_level, severe=severe_veto
        )
        raw_payload["risk_notes"] = [
            str(flag.get("description"))
            for flag in flags
            if flag.get("description")
        ]
        # 显式标记：该 opinion 不是方向性投票，聚合器不得将其计入共识均值。
        raw_payload["directional_vote"] = False

        try:
            confidence = float(parsed.get("risk_score", 50)) / 100.0
        except (TypeError, ValueError):
            confidence = 0.5

        return AgentOpinion(
            agent_name=self.agent_name,
            signal=RISK_ASSESSMENT_SIGNAL,
            confidence=confidence,
            reasoning=parsed.get("reasoning", ""),
            raw_data=raw_payload,
        )

