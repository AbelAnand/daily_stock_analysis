# -*- coding: utf-8 -*-
"""
基于技术指标计算可验证的交易价位（entry / stop / target）。

与 LLM 生成的"狙击点位"不同，本模块的价位全部由数据计算得出：
- 止损：结构性摆动低点与 ATR 止损中取较高者（更紧的一档）
- 目标：下一压力位（摆动高点）与 ATR 目标中取较低者
- 盈亏比：r_multiple = (target - entry) / (entry - stop)

纯数值计算，不关心币种（A股"元"或美股"$"由展示层处理）。
所有输入缺失/NaN 时返回 None 字段，绝不抛异常。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, List, Optional


# 止损/目标的 ATR 倍数（Wilder ATR14 口径）
DEFAULT_ATR_STOP_MULT = 1.5    # 止损 = entry - 1.5 * ATR（与结构低点取较高者）
DEFAULT_ATR_TARGET_MULT = 3.0  # 目标 = entry + 3 * ATR（与压力位取较低者）
DEFAULT_MIN_RR = 2.0           # 结构允许时要求的最低盈亏比
SWING_STOP_BUFFER = 0.005      # 结构止损设在摆动低点下方 0.5%


@dataclass(frozen=True)
class TradeLevels:
    """计算得出的交易价位。字段为 None 表示数据不足无法计算。"""

    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    r_multiple: Optional[float] = None
    # good: R:R >= 2；acceptable: 1.5 <= R:R < 2；poor_risk_reward: R:R < 1.5；
    # invalid: 价位几何关系不成立；insufficient_data: 数据不足
    quality: str = "insufficient_data"
    atr: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "r_multiple": self.r_multiple,
            "quality": self.quality,
            "atr": self.atr,
            "notes": list(self.notes),
        }


def _clean(value: Any) -> Optional[float]:
    """归一化价格类数值：非有限数或非正数视为缺失。"""
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _wilder_atr(df: Any, period: int = 14) -> Optional[float]:
    """从 OHLC DataFrame 计算 Wilder 平滑 ATR（与 stock_analyzer 同口径）。"""
    try:
        import pandas as pd

        if df is None or len(df) < 2:
            return None
        cols = {c.lower(): c for c in df.columns}
        if not all(k in cols for k in ("high", "low", "close")):
            return None
        high = df[cols["high"]].astype(float)
        low = df[cols["low"]].astype(float)
        close = df[cols["close"]].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean().iloc[-1]
        return _clean(atr)
    except Exception:
        return None


def _extract_inputs(data: Any) -> dict:
    """从指标 Mapping 或 OHLC DataFrame 中提取 ATR/摆动高低点。"""
    out = {"atr": None, "swing_low": None, "swing_high": None}
    if data is None:
        return out

    if isinstance(data, Mapping):
        out["atr"] = _clean(data.get("atr_14") or data.get("atr"))
        out["swing_low"] = _clean(data.get("swing_low_20") or data.get("swing_low"))
        out["swing_high"] = _clean(data.get("swing_high_20") or data.get("swing_high"))
        return out

    # 兼容 TrendAnalysisResult 之类的对象
    if hasattr(data, "atr_14"):
        out["atr"] = _clean(getattr(data, "atr_14", None))
        out["swing_low"] = _clean(getattr(data, "swing_low_20", None))
        out["swing_high"] = _clean(getattr(data, "swing_high_20", None))
        return out

    # OHLC DataFrame：现算 ATR 与摆动高低点
    if hasattr(data, "columns"):
        out["atr"] = _wilder_atr(data)
        try:
            cols = {c.lower(): c for c in data.columns}
            if "low" in cols and len(data) >= 1:
                out["swing_low"] = _clean(data[cols["low"]].iloc[-20:].min())
            if "high" in cols and len(data) >= 1:
                out["swing_high"] = _clean(data[cols["high"]].iloc[-20:].max())
        except Exception:
            pass
    return out


def compute_trade_levels(
    data: Any,
    current_price: Any,
    direction: str = "long",
    *,
    atr_stop_mult: float = DEFAULT_ATR_STOP_MULT,
    atr_target_mult: float = DEFAULT_ATR_TARGET_MULT,
    min_rr: float = DEFAULT_MIN_RR,
) -> TradeLevels:
    """
    计算 entry / stop / target 价位（纯函数，不抛异常）。

    Args:
        data: 指标 dict（含 atr_14 / swing_low_20 / swing_high_20，
              即 TrendAnalysisResult.to_dict() 的输出）、TrendAnalysisResult
              对象，或 OHLC DataFrame（列名 high/low/close，不区分大小写）
        current_price: 现价
        direction: 目前仅支持 "long"（做空方向返回 insufficient_data 并注明）

    Returns:
        TradeLevels（字段可能为 None，quality 描述整体质量）
    """
    notes: List[str] = []

    entry = _clean(current_price)
    if entry is None:
        return TradeLevels(notes=["Current price missing; cannot compute trade levels"])

    if direction != "long":
        return TradeLevels(
            entry=entry,
            notes=[f"direction={direction} is not supported yet; only long is supported"],
        )

    inputs = _extract_inputs(data)
    atr = inputs["atr"]
    swing_low = inputs["swing_low"]
    swing_high = inputs["swing_high"]

    if atr is None and swing_low is None:
        return TradeLevels(entry=entry, notes=["Missing ATR and structural swing low; cannot compute stop"])

    # --- 止损：结构低点与 ATR 止损取较高者（更紧的一档，控制单笔风险）---
    stop_candidates: List[float] = []
    if swing_low is not None and swing_low < entry:
        stop_candidates.append(swing_low * (1 - SWING_STOP_BUFFER))
        notes.append(f"Structural stop references recent swing low {swing_low:.2f}")
    if atr is not None:
        stop_candidates.append(entry - atr_stop_mult * atr)
        notes.append(f"ATR stop = entry - {atr_stop_mult}*ATR({atr:.2f})")

    stop_candidates = [s for s in stop_candidates if 0 < s < entry]
    if not stop_candidates:
        return TradeLevels(
            entry=entry,
            atr=atr,
            quality="invalid",
            notes=notes + ["No valid stop below the entry price could be derived"],
        )
    stop = max(stop_candidates)
    risk = entry - stop

    # --- 目标：压力位与 ATR 目标取较低者；m 保证结构允许时 R:R >= min_rr ---
    target_candidates: List[float] = []
    if atr is not None:
        eff_mult = max(atr_target_mult, min_rr * risk / atr)
        target_candidates.append(entry + eff_mult * atr)
    else:
        target_candidates.append(entry + min_rr * risk)
    if swing_high is not None and swing_high > entry:
        target_candidates.append(swing_high)
        notes.append(f"Target capped by recent resistance {swing_high:.2f}")

    target = min(target_candidates)
    if target <= entry:
        return TradeLevels(
            entry=entry,
            stop=round(stop, 4),
            atr=atr,
            quality="invalid",
            notes=notes + ["Target price is not above the entry price"],
        )

    r_multiple = (target - entry) / risk if risk > 0 else None

    if r_multiple is None:
        quality = "invalid"
    elif r_multiple >= min_rr:
        quality = "good"
    elif r_multiple >= 1.5:
        quality = "acceptable"
    else:
        quality = "poor_risk_reward"
        notes.append(f"Risk/reward {r_multiple:.2f} < 1.5; resistance too close or stop too wide")

    return TradeLevels(
        entry=round(entry, 4),
        stop=round(stop, 4),
        target=round(target, 4),
        r_multiple=round(r_multiple, 4) if r_multiple is not None else None,
        quality=quality,
        atr=round(atr, 6) if atr is not None else None,
        notes=notes,
    )
