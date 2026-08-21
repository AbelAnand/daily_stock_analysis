# -*- coding: utf-8 -*-
"""
DecisionAgent — final synthesis and decision-making specialist.

Responsible for:
- Aggregating opinions from technical + intel + risk + skill agents
- Producing the final Decision Dashboard JSON
- Generating actionable buy/hold/sell recommendations with price levels
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from src.agent.agents.base_agent import BaseAgent
from src.agent.protocols import AgentContext, AgentOpinion, normalize_decision_signal
from src.report_language import normalize_report_language

logger = logging.getLogger(__name__)


class DecisionAgent(BaseAgent):
    """Synthesise prior agent opinions into the final dashboard."""

    agent_name = "decision"
    max_steps = 3  # pure synthesis, should not need many tool calls
    tool_names: Optional[List[str]] = []  # no tool access — works from context only

    @staticmethod
    def _is_chat_mode(ctx: AgentContext) -> bool:
        return ctx.meta.get("response_mode") == "chat"

    @staticmethod
    def _collect_risk_context(ctx: AgentContext) -> Optional[dict]:
        """读取 RiskAgent 的非投票 risk_context（复用聚合器的唯一实现）。"""
        try:
            from src.agent.skills.aggregator import SkillAggregator

            return SkillAggregator._extract_risk_context(ctx.opinions)
        except Exception:
            logger.debug("[DecisionAgent] failed to extract risk context", exc_info=True)
            return None

    def system_prompt(self, ctx: AgentContext) -> str:
        report_language = normalize_report_language(ctx.meta.get("report_language", "zh"))
        if self._is_chat_mode(ctx):
            prompt = """\
You are a **Decision Synthesis Agent** replying directly to the user's latest
stock-analysis question.

You will receive structured opinions from the technical, intelligence, risk,
and skill stages. Synthesize them into a concise, natural-language answer.

Requirements:
- Answer the user's actual question directly
- Use Markdown when helpful
- Keep the response practical and specific
- Highlight the main signal, key reasoning, and major risks
- Do NOT output JSON or code fences unless the user explicitly asks for them
"""
            if report_language == "en":
                return prompt + "\nAlways answer in English.\n"
            if report_language == "ko":
                return prompt + "\n항상 한국어로 답변하세요.\n"
            return prompt + "\n默认使用中文回答。\n"

        skills = ""
        if self.skill_instructions:
            skills = f"\n## Active Trading Skills\n\n{self.skill_instructions}\n"

        prompt = f"""\
You are a **Decision Synthesis Agent** that produces the final investment \
Decision Dashboard.

You will receive:
1. Structured opinions from a Technical Agent and an Intel Agent
2. Any risk flags raised by a Risk Agent
        3. Skill evaluation results (if applicable)

Your task: synthesise all inputs into a single, actionable Decision Dashboard.
{skills}
## Core Principles
1. **Core conclusion first** — one sentence, ≤30 chars
2. **Split advice** — different for no-position vs has-position
3. **Precise sniper levels** — concrete price numbers; commit to the expected-value verdict, no hedging
4. **Checklist visual** — ✅⚠️❌ for each checkpoint
5. **Risk priority** — risk alerts must be prominent. High-severity risk must be \
   annotated in risk_warning and reflected as a smaller suggested position size \
   and a tighter/wider-documented stop; it does not by itself force the signal to hold.

## Signal Weighting Guidelines
- Technical opinion weight: ~40%
- Intel / sentiment weight: ~30%
- Risk assessment weight: ~30% — the Risk Agent is NON-VOTING \
  (signal=risk_assessment) and never sets direction. Consume its outputs: \
  scale the suggested position by ``position_size_factor``, surface \
  ``risk_notes`` prominently in risk_warning, and tighten the stop when risk \
  is medium/high (stop tightening, not a veto). Only severe findings \
  (fraud / delisting / halt, ``veto_buy=true``) may block a buy.
- If a skill opinion is present, blend it at 20% weight (reducing others proportionally)

## Scoring
- 80-100: buy (all conditions met, high conviction)
- 60-79: buy (mostly positive, minor caveats)
- 40-59: hold (mixed signals, or risk present)
- 20-39: sell (negative trend + risk)
- 0-19: sell (major risk + bearish)

## Expected-Value Contract (replaces hedging)
Every synthesis must state:
- ``p_up``: integer percent in steps of 5 — your probability that price reaches \
  target before stop over the stated horizon
- entry, stop, target — prefer the computed support/resistance levels; you may \
  adjust them with explicit justification
- the R multiple = (target - entry) / (entry - stop)
- expected value EV = p*R - (1 - p), where p = p_up/100

Emit hold/watch ONLY when EV < 0 or R < 1.5 — and every hold MUST name the \
specific price level or event (``flip_condition``) that would flip it to buy or \
sell. A moderate-risk trade with positive EV and R >= 1.5 is a buy with an \
honest p_up, not a hold.

Stability rules:
- Do not flip directly between buy and sell only because one trading day moved \
  up or down; a changed verdict must be anchored to a level or event.
- Base operation_advice on support/resistance, volume/chip context, main-force \
  capital flow where available, and risk flags.
- Markets without capital-flow data (US/HK/TW): price structure plus volume \
  suffice; never withhold a buy/sell verdict merely because capital flow is \
  unavailable.
- Buy needs support confirmation or a valid resistance breakout with volume \
  (and capital-flow where available) confirmation; sell needs support failure, \
  sustained outflow, or clearly elevated risk — expressed through EV, not vetoes.

Include ``p_up`` as a top-level output field and an ``ev_contract`` object with \
``entry``, ``stop``, ``target``, ``r_multiple``, ``expected_value``, \
``flip_condition``.

## Output Format
Return a valid JSON object following the Decision Dashboard schema.  The JSON \
must include at minimum these top-level keys:
  stock_name, sentiment_score, trend_prediction, operation_advice,
  decision_type, confidence_level, dashboard, analysis_summary,
  key_points, risk_warning

Important: ``decision_type`` must stay within the existing enum
``buy|hold|sell``. Express stronger conviction via ``confidence_level``,
``sentiment_score``, and the natural-language fields instead of inventing
new decision_type values.

The nested ``dashboard`` object must include ``phase_decision`` with these
keys: ``phase_context``, ``action_window``, ``immediate_action``,
``watch_conditions``, ``next_check_time``, ``confidence_reason``,
``data_limitations``. For intraday/lunch-break/near-close phases, describe the
current action, watch conditions, and next check point. For pre-market,
non-trading, or unknown phases, do not invent today's intraday movement. If
quote, daily bars, or technical data is stale, fallback, missing, fetch_failed,
partial, or estimated, ``confidence_level`` must not be High/高 and the
limitation must be reflected in ``confidence_reason`` or ``data_limitations``.

The nested ``dashboard`` object should include optional ``signal_attribution`` when
the available evidence supports attribution, with these keys: ``technical_indicators``, ``news_sentiment``, ``fundamentals``,
``market_conditions``, ``strongest_bullish_signal``, ``strongest_bearish_signal``.
The first four keys are contribution weights (0-100). Non-zero valid weights
should sum to 100; all-zero means no effective signal and must not be faked.
``technical_indicators`` explains the impact of technical signals on the recommendation.
``news_sentiment`` explains the impact of news/sentiment on the recommendation.
``fundamentals`` explains the impact of fundamental factors (valuation, earnings, financials) on the recommendation.
``market_conditions`` explains the impact of overall market environment on the recommendation.
``strongest_bullish_signal`` is the name of the strongest bullish signal (e.g., MACD golden cross, earnings surprise, low valuation).
``strongest_bearish_signal`` is the name of the strongest bearish signal (e.g., MA death cross, earnings warning, high valuation).
"""
        if report_language == "en":
            return prompt + """

## Output Language
- Keep every JSON key unchanged.
- `decision_type` must remain `buy|hold|sell`.
- Write all human-readable JSON values in English.
"""
        if report_language == "ko":
            return prompt + """

## Output Language
- Keep every JSON key unchanged.
- `decision_type` must remain `buy|hold|sell`.
- Write all human-readable JSON values in Korean (한국어).
"""
        return prompt + """

## 输出语言
- 所有 JSON 键名保持不变。
- `decision_type` 必须保持为 `buy|hold|sell`。
- 所有面向用户的人类可读文本值必须使用中文。
"""

    def build_user_message(self, ctx: AgentContext) -> str:
        if self._is_chat_mode(ctx):
            parts = [
                "# User Question",
                ctx.query,
                "",
                f"Stock: {ctx.stock_code} ({ctx.stock_name})" if ctx.stock_name else f"Stock: {ctx.stock_code}",
                "",
            ]
        else:
            parts = [
                f"# Synthesis Request for {ctx.stock_code}",
                f"Stock: {ctx.stock_code} ({ctx.stock_name})" if ctx.stock_name else f"Stock: {ctx.stock_code}",
                "",
            ]

        # Feed prior opinions — Orchestrator已在 _partition_skill_opinions 中完成
        # skill 观点的分拣，ctx.opinions 中不再含 invalid skill opinion；
        # invalid skill 观点存于 ctx.meta["invalid_opinions"]。
        # DecisionAgent 直接消费，不再二次过滤。
        if ctx.opinions:
            parts.append("## Agent Opinions (Evidence Chain)")
            for op in ctx.opinions:
                parts.append(f"\n### {op.agent_name}")
                parts.append(f"Signal: {op.signal} | Confidence: {op.confidence:.2f}")
                parts.append(f"Reasoning: {op.reasoning}")
                if op.key_levels:
                    parts.append(f"Key levels: {json.dumps(op.key_levels)}")
                if op.raw_data:
                    extra_keys = {k: v for k, v in op.raw_data.items()
                                  if k not in ("signal", "confidence", "reasoning", "key_levels", "invalid_signal")}
                    if extra_keys:
                        parts.append(f"Extra data: {json.dumps(extra_keys, ensure_ascii=False, default=str)}")
                parts.append("")

        invalid_opinions = ctx.meta.get("invalid_opinions") or []
        if invalid_opinions:
            reason_labels = {
                "skill_timeout": "执行超时",
                "skill_error": "执行异常或未产出结构化观点",
                "missing_signal": "signal 缺失",
                "unrecognized_signal": "signal 无法识别",
            }
            reason_counts = {}
            for item in invalid_opinions:
                if not isinstance(item, dict):
                    continue
                reason = str(item.get("reason") or "unrecognized_signal")
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            reason_summary = "、".join(
                f"{reason_labels.get(reason, reason)} {count} 个"
                for reason, count in reason_counts.items()
            )
            parts.append("## Invalid Skill Opinions (Diagnostics only — not in evidence chain)")
            parts.append(
                f"共 {len(invalid_opinions)} 个 skill 观点未进入证据链"
                f"（{reason_summary or '原因未分类'}）；"
                f"仅供你在 data_limitations 中标注，不得作为决策依据。"
            )
            parts.append("")

        # Feed risk flags
        if ctx.risk_flags:
            parts.append("## Risk Flags")
            for rf in ctx.risk_flags:
                parts.append(f"- [{rf.get('severity', 'medium')}] {rf.get('category', '')}: {rf.get('description', '')}")
            parts.append("")

        # RiskAgent 非投票输出：建议仓位系数 / severe 否决标志 / 风险提示。
        # 用于压缩 suggested position 与收紧止损，不参与方向投票。
        risk_context = self._collect_risk_context(ctx)
        if risk_context:
            parts.append("## Risk Context (non-voting)")
            parts.append(
                "Apply position_size_factor to the suggested position and tighten "
                "the stop for medium/high risk; severe_veto only blocks buys."
            )
            parts.append(json.dumps(risk_context, ensure_ascii=False, default=str))
            parts.append("")

        disagreement_summary = ctx.meta.get("agent_disagreement_summary")
        if isinstance(disagreement_summary, dict) and disagreement_summary:
            parts.append("## Agent Disagreement Summary")
            parts.append(json.dumps(disagreement_summary, ensure_ascii=False, default=str))
            parts.append("")

        # Skill meta
        requested_skills = ctx.meta.get("skills_requested") or ctx.meta.get("strategies_requested")
        if requested_skills:
            parts.append(f"## Skills: {', '.join(requested_skills)}")
            parts.append("")

        if self._is_chat_mode(ctx):
            parts.append(
                "Answer the user in natural language using the evidence above. "
                "Do not output JSON unless the user explicitly requests structured data."
            )
        else:
            parts.append("Synthesise the above into the Decision Dashboard JSON.")
        return "\n".join(parts)

    def post_process(self, ctx: AgentContext, raw_text: str) -> Optional[AgentOpinion]:
        """Store the parsed dashboard in ctx.meta; also return an opinion."""
        if self._is_chat_mode(ctx):
            text = (raw_text or "").strip()
            if not text:
                return None

            ctx.set_data("final_response_text", text)
            prior = next((op for op in reversed(ctx.opinions) if op.agent_name != self.agent_name), None)
            return AgentOpinion(
                agent_name=self.agent_name,
                signal=prior.signal if prior is not None else "hold",
                confidence=prior.confidence if prior is not None else 0.5,
                reasoning=text,
                raw_data={"response_mode": "chat"},
            )

        from src.agent.runner import parse_dashboard_json

        dashboard = parse_dashboard_json(raw_text)
        if dashboard:
            dashboard["decision_type"] = normalize_decision_signal(
                dashboard.get("decision_type", "hold")
            )
            ctx.set_data("final_dashboard", dashboard)
            try:
                _raw_score = dashboard.get("sentiment_score", 50) or 50
                _score = float(_raw_score)
            except (TypeError, ValueError):
                _score = 50.0
            return AgentOpinion(
                agent_name=self.agent_name,
                signal=dashboard.get("decision_type", "hold"),
                confidence=min(1.0, _score / 100.0),
                reasoning=dashboard.get("analysis_summary", ""),
                raw_data=dashboard,
            )
        else:
            # Even if JSON parsing fails, store the raw text for downstream use
            ctx.set_data("final_dashboard_raw", raw_text)
            logger.warning("[DecisionAgent] failed to parse dashboard JSON")
            return None
