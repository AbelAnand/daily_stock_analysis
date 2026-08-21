# -*- coding: utf-8 -*-
"""Helpers for parsing and validating report sniper-point price values."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional


SNIPER_KEYS = ("ideal_buy", "secondary_buy", "stop_loss", "take_profit")

# "$150.50" / "US$150.50" 前缀式美元价格
_DOLLAR_PRICE_RE = re.compile(r"(?:US\$|\$)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_sniper_value(value: Any) -> Optional[float]:
    """Parse a sniper point value from report text into a positive price."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else None

    text = str(value).replace(",", "").replace("，", "").strip()
    if not text or text in {"-", "—", "N/A"}:
        return None

    try:
        parsed = float(text)
        return parsed if parsed > 0 else None
    except ValueError:
        pass

    colon_pos = max(text.rfind("："), text.rfind(":"))
    yuan_pos = text.find("元", colon_pos + 1 if colon_pos != -1 else 0)
    if yuan_pos != -1:
        segment_start = colon_pos + 1 if colon_pos != -1 else 0
        segment = text[segment_start:yuan_pos]
        valid_numbers = []
        for match in re.finditer(r"-?\d+(?:\.\d+)?", segment):
            start_idx = match.start()
            if start_idx >= 2 and segment[start_idx - 2:start_idx].upper() == "MA":
                continue
            valid_numbers.append(match.group())
        if valid_numbers:
            try:
                parsed = abs(float(valid_numbers[-1]))
                return parsed if parsed > 0 else None
            except ValueError:
                pass

    # 美元前缀格式："$150.50"、"US$150.50"（价格跟在符号后面，与"元"后缀相反）
    dollar_matches = _DOLLAR_PRICE_RE.findall(text)
    if dollar_matches:
        try:
            parsed = float(dollar_matches[-1])
            if parsed > 0:
                return parsed
        except ValueError:
            pass

    paren_pos = len(text)
    for paren_char in ("(", "（"):
        pos = text.find(paren_char)
        if pos != -1:
            paren_pos = min(paren_pos, pos)
    search_text = text[:paren_pos].strip() or text

    valid_numbers = []
    for match in re.finditer(r"\d+(?:\.\d+)?", search_text):
        start_idx = match.start()
        if start_idx >= 2 and search_text[start_idx - 2:start_idx].upper() == "MA":
            continue
        valid_numbers.append(match.group())
    if valid_numbers:
        try:
            parsed = float(valid_numbers[-1])
            return parsed if parsed > 0 else None
        except ValueError:
            pass
    return None


def extract_sniper_points(result: Any) -> Dict[str, Optional[float]]:
    """Extract normalized sniper-point prices from a completed analysis result."""

    raw_points: Mapping[str, Any] = {}

    if hasattr(result, "get_sniper_points"):
        candidate = result.get_sniper_points() or {}
        if isinstance(candidate, Mapping):
            raw_points = candidate

    if not _has_any_sniper_value(raw_points):
        dashboard = getattr(result, "dashboard", None)
        if isinstance(dashboard, Mapping):
            raw_points = find_sniper_points(dashboard) or raw_points

    if not _has_any_sniper_value(raw_points):
        raw_response = getattr(result, "raw_response", None)
        if isinstance(raw_response, Mapping):
            raw_points = find_sniper_points(raw_response) or raw_points

    return {key: parse_sniper_value(raw_points.get(key)) for key in SNIPER_KEYS}


def _has_any_sniper_value(points: Mapping[str, Any]) -> bool:
    return any(points.get(key) not in (None, "") for key in SNIPER_KEYS)


def find_sniper_points(data: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    if not isinstance(data, Mapping):
        return None

    if any(key in data for key in SNIPER_KEYS):
        return data

    sniper_points = data.get("sniper_points")
    if isinstance(sniper_points, Mapping) and sniper_points:
        return sniper_points

    battle_plan = data.get("battle_plan")
    if isinstance(battle_plan, Mapping):
        sniper_points = battle_plan.get("sniper_points")
        if isinstance(sniper_points, Mapping) and sniper_points:
            return sniper_points

    inner_dashboard = data.get("dashboard")
    if isinstance(inner_dashboard, Mapping):
        found = find_sniper_points(inner_dashboard)
        if found:
            return found

    return None


def _finite_price(value: Any) -> Optional[float]:
    """Normalize a price-like value: None/NaN/inf/non-positive -> None."""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def validate_levels(
    entry: Any,
    stop: Any,
    target: Any,
    *,
    current_price: Any = None,
    min_rr: float = 1.5,
    max_distance_pct: float = 30.0,
) -> Dict[str, Any]:
    """
    校验做多计划的价位几何关系（entry/stop/target 通常来自 LLM 输出）。

    检查项：
    - stop_not_below_entry: 止损 >= 入场价（风险无界）
    - target_not_above_entry: 目标 <= 入场价（无盈利空间）
    - poor_risk_reward: 盈亏比 (target-entry)/(entry-stop) < min_rr
    - *_far_from_price: 价位偏离现价超过 max_distance_pct%（疑似臆造）

    Returns:
        {"valid": bool, "issues": [issue codes], "r_multiple": float | None}
        缺失的价位不参与校验（只校验可校验的部分），绝不抛异常。
    """
    issues: List[str] = []
    entry_f = _finite_price(entry)
    stop_f = _finite_price(stop)
    target_f = _finite_price(target)
    price_f = _finite_price(current_price)

    r_multiple: Optional[float] = None

    if entry_f is not None and stop_f is not None and stop_f >= entry_f:
        issues.append("stop_not_below_entry")
    if entry_f is not None and target_f is not None and target_f <= entry_f:
        issues.append("target_not_above_entry")

    if (
        entry_f is not None
        and stop_f is not None
        and target_f is not None
        and stop_f < entry_f < target_f
    ):
        risk = entry_f - stop_f
        if risk > 0:
            r_multiple = round((target_f - entry_f) / risk, 4)
            if r_multiple < min_rr:
                issues.append("poor_risk_reward")

    if price_f is not None and max_distance_pct > 0:
        for name, level in (("entry", entry_f), ("stop", stop_f), ("target", target_f)):
            if level is None:
                continue
            distance_pct = abs(level - price_f) / price_f * 100
            if distance_pct > max_distance_pct:
                issues.append(f"{name}_far_from_price")

    return {"valid": not issues, "issues": issues, "r_multiple": r_multiple}
