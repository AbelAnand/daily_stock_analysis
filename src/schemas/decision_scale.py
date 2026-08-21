# -*- coding: utf-8 -*-
"""Canonical score-to-decision scale shared by reports and DecisionSignal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional


CANONICAL_DECISION_SCALE_VERSION = "decision-scale-v1"


@dataclass(frozen=True)
class DecisionScaleBand:
    min_score: int
    max_score: int
    signal_key: str
    action: str
    decision_type: str
    label: str
    description: str

    # Backward-compatible aliases for the previous Chinese-named fields.
    @property
    def label_zh(self) -> str:
        return self.label

    @property
    def description_zh(self) -> str:
        return self.description


CANONICAL_DECISION_SCALE: tuple[DecisionScaleBand, ...] = (
    DecisionScaleBand(80, 100, "strong_buy", "buy", "buy", "Strong Buy", "High-probability setup; execute the buy/add plan"),
    DecisionScaleBand(60, 79, "buy", "buy", "buy", "Buy", "Constructive setup; a few items may still await confirmation"),
    DecisionScaleBand(40, 59, "watch", "watch", "hold", "Watch", "Mixed signals or insufficient expected value (EV<0 or R<1.5); a clear flip condition is required"),
    DecisionScaleBand(20, 39, "reduce", "reduce", "sell", "Reduce", "Risk is clearly rising; prioritize lowering exposure"),
    DecisionScaleBand(0, 19, "sell", "sell", "sell", "Sell", "Trend or risk has deteriorated materially; prioritize exiting"),
)


CANONICAL_DECISION_SCALE_PROMPT = """## Canonical score and action scale

- `sentiment_score`, `operation_advice`, the three-state `decision_type` and the eight-state `action` must all express the same verdict.
- The score expresses the conviction behind the conclusion, not the action itself; the final `action` must obey the expected-value (EV) contract: staying at hold/watch is only allowed when EV < 0 or R < 1.5.
- 80-100: Strong Buy, `action=buy`, `decision_type=buy`.
- 60-79: Buy, `action=buy`, `decision_type=buy`.
- 40-59: neutral zone, defaults to Watch (`action=watch`, `decision_type=hold`), but this is not a mechanical mapping: if EV is positive and R >= 1.5, give the directional conclusion honestly and let the score reflect real conviction; when staying at watch, `ev_contract.flip_condition` must state the specific price level or event that would flip the conclusion to buy/sell.
- 20-39: Reduce, `action=reduce`, `decision_type=sell`.
- 0-19: Sell, `action=sell`, `decision_type=sell`.
- `decision_type` only keeps `buy|hold|sell` for compatible statistics; finer-grained advice must go into `action`.
- If score >= 60 but the final `action` is `hold/watch`, or score < 40 but the final `action` is `hold/watch`, the downgrade reason must be stated in `guardrail_reason` or `dashboard.decision_stability.reason` (normally citing EV/R values or the flip condition)."""

# Backward-compatible alias (the prompt is now English-first).
CANONICAL_DECISION_SCALE_PROMPT_ZH = CANONICAL_DECISION_SCALE_PROMPT


def normalize_score(value: Any) -> Optional[int]:
    """Return a bounded integer score when possible."""

    try:
        score = int(float(value))
    except (TypeError, ValueError):
        return None
    if 0 <= score <= 100:
        return score
    return None


def decision_band_for_score(value: Any) -> Optional[DecisionScaleBand]:
    """Return the canonical decision band for a 0-100 score."""

    score = normalize_score(value)
    if score is None:
        return None
    for band in CANONICAL_DECISION_SCALE:
        if band.min_score <= score <= band.max_score:
            return band
    return None


def signal_key_for_score(value: Any) -> Optional[str]:
    band = decision_band_for_score(value)
    return band.signal_key if band else None


def action_for_score(value: Any) -> Optional[str]:
    band = decision_band_for_score(value)
    return band.action if band else None


def decision_type_for_score(value: Any) -> Optional[str]:
    band = decision_band_for_score(value)
    return band.decision_type if band else None


def score_band_metadata(value: Any) -> dict[str, Any]:
    """Return stable metadata for persistence and diagnostics."""

    score = normalize_score(value)
    band = decision_band_for_score(score)
    if score is None or band is None:
        return {}
    return {
        "scale_version": CANONICAL_DECISION_SCALE_VERSION,
        "score": score,
        "score_band": f"{band.min_score}-{band.max_score}",
        "signal_key": band.signal_key,
        "canonical_action": band.action,
        "canonical_decision_type": band.decision_type,
    }


def extract_decision_guardrail_reason(payload: Any) -> Optional[str]:
    """Extract an applied score/action guardrail reason from a result payload."""

    data = payload if isinstance(payload, Mapping) else {}
    dashboard = data.get("dashboard") if isinstance(data.get("dashboard"), Mapping) else {}
    calibration = (
        dashboard.get("decision_score_calibration")
        if isinstance(dashboard.get("decision_score_calibration"), Mapping)
        else {}
    )
    stability = (
        dashboard.get("decision_stability")
        if isinstance(dashboard.get("decision_stability"), Mapping)
        else {}
    )
    metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}

    stability_applied = stability.get("applied")
    include_stability_reason = stability_applied not in (False, 0, "0", "false", "False")
    candidates = [
        data.get("guardrail_reason"),
        data.get("downgrade_reason"),
        data.get("decision_score_guardrail_reason"),
        metadata.get("guardrail_reason"),
        metadata.get("downgrade_reason"),
        calibration.get("guardrail_reason"),
        calibration.get("downgrade_reason"),
    ]
    if include_stability_reason:
        candidates.extend(
            [
                stability.get("guardrail_reason"),
                stability.get("downgrade_reason"),
                stability.get("reason"),
            ]
        )

    for candidate in candidates:
        text = str(candidate or "").strip()
        if text:
            return text
    return None


def score_action_conflicts_without_guardrail(
    *,
    score: Any,
    action: Any,
    guardrail_reason: Any = None,
) -> bool:
    """Return True when a neutral action conflicts with a directional score."""

    if str(guardrail_reason or "").strip():
        return False
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"hold", "watch"}:
        return False
    score_action = action_for_score(score)
    return score_action in {"buy", "reduce", "sell"}
