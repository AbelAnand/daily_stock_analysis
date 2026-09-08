# -*- coding: utf-8 -*-
"""
Paper-trading execution of daily decision signals via the Alpaca API.

Runs after the daily analysis. For each analyzed US symbol it reads the latest
active decision signal and turns it into an order on an Alpaca PAPER account:

- buy / add  -> bracket order: GTC limit entry with an attached stop-loss and
                take-profit, sized so that a stop-out loses at most
                ``risk_per_trade_usd`` (default $500). The limit is placed at the
                top of the entry range; if the market already trades above it,
                the entry *chases*: a marketable limit at the latest price plus
                ``chase_pct`` percent, but only while the R:R recomputed at that
                price still clears ``chase_min_r_multiple`` (the plan itself
                must clear ``min_r_multiple`` at the planned entry either way).
                Signals refresh daily, so
                an unfilled chase is repriced every morning instead of resting
                below the market for days.
- reduce     -> sell half of an existing position (market).
- sell       -> close the position (market).
- hold/watch -> no order.

Safety rails (all deliberate, all redundant):
- Paper accounts only: every client refuses account numbers not prefixed ``PA``.
- Kill switch: if ``data/paper_trading.STOP`` exists nothing is submitted.
- Dry-run mode logs and persists intended orders without calling the API.
- Max open positions, max % of equity per name, whole-share sizing.
- A new entry is skipped when the plan failed validation, R:R < min_r_multiple,
  earnings fall inside the horizon, or the symbol already has a position or an
  open order.
- Stale unfilled entries older than ``entry_ttl_days`` are cancelled.

Every intended / submitted / skipped order is persisted to ``paper_trades`` and
linked to the decision-signal id so realized results can be compared against
the signal track record later.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KILL_SWITCH_DEFAULT = "data/paper_trading.STOP"
_ACCOUNT_KEY_RE = re.compile(r"^ALPACA_([A-Z0-9]+)_KEY_ID$")


# ============================================================
# Settings
# ============================================================

@dataclass
class PaperTradingSettings:
    enabled: bool = False
    account: str = "auto"               # "auto" picks the reachable account with the highest equity
    risk_per_trade_usd: float = 500.0   # max loss at the stop per new entry
    max_positions: int = 10
    max_position_pct: float = 20.0      # max % of equity in one name
    entry_ttl_days: int = 3             # cancel unfilled entries older than this
    dry_run: bool = False
    min_r_multiple: float = 1.5         # plan-quality gate at the planned entry
    chase_pct: float = 0.3              # marketable-limit buffer above the latest price when chasing
    chase_min_r_multiple: float = 1.3   # execution floor for R:R recomputed at the chased price
    # Open-position management (stop ratchet). R = entry - initial stop.
    breakeven_trigger_r: float = 1.0    # once unrealized gain >= this many R, the stop is raised to at least break-even
    breakeven_buffer_pct: float = 0.2   # break-even stop = entry * (1 + this %) so the exit is a small profit, not a scratch
    trail_r: float = 1.0                # beyond the trigger, the stop trails the latest price by this many R (only ever up)
    stop_min_gap_pct: float = 0.5       # never place the stop closer than this % below the latest price
    # New-entry stop distance floor: a plan stop closer than this many ATR(14) to the
    # entry is widened to the floor before the R:R gates run (0 disables).
    stop_min_atr_mult: float = 1.0
    allow_short: bool = False           # execute bearish signals carrying a short_plan as short brackets
    kill_switch_path: str = KILL_SWITCH_DEFAULT

    @classmethod
    def from_env(cls, env: Optional[Dict[str, Any]] = None) -> "PaperTradingSettings":
        env = env if env is not None else os.environ

        def _bool(key: str, default: bool) -> bool:
            raw = env.get(key)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        def _float(key: str, default: float) -> float:
            try:
                raw = env.get(key)
                return float(raw) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default

        def _int(key: str, default: int) -> int:
            try:
                raw = env.get(key)
                return int(raw) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=_bool("PAPER_TRADING_ENABLED", False),
            account=str(env.get("PAPER_TRADING_ACCOUNT") or "auto").strip().upper() or "AUTO",
            risk_per_trade_usd=_float("PAPER_TRADING_RISK_PER_TRADE_USD", 500.0),
            max_positions=_int("PAPER_TRADING_MAX_POSITIONS", 10),
            max_position_pct=_float("PAPER_TRADING_MAX_POSITION_PCT", 20.0),
            entry_ttl_days=_int("PAPER_TRADING_ENTRY_TTL_DAYS", 3),
            dry_run=_bool("PAPER_TRADING_DRY_RUN", False),
            min_r_multiple=_float("PAPER_TRADING_MIN_R_MULTIPLE", 1.5),
            chase_pct=_float("PAPER_TRADING_CHASE_PCT", 0.3),
            chase_min_r_multiple=_float("PAPER_TRADING_CHASE_MIN_R", 1.3),
            breakeven_trigger_r=_float("PAPER_TRADING_BREAKEVEN_TRIGGER_R", 1.0),
            breakeven_buffer_pct=_float("PAPER_TRADING_BREAKEVEN_BUFFER_PCT", 0.2),
            trail_r=_float("PAPER_TRADING_TRAIL_R", 1.0),
            stop_min_gap_pct=_float("PAPER_TRADING_STOP_MIN_GAP_PCT", 0.5),
            stop_min_atr_mult=_float("PAPER_TRADING_STOP_MIN_ATR_MULT", 1.0),
            allow_short=_bool("PAPER_TRADING_ALLOW_SHORT", False),
            kill_switch_path=str(env.get("PAPER_TRADING_KILL_SWITCH") or KILL_SWITCH_DEFAULT),
        )


# ============================================================
# Broker wrapper (paper only)
# ============================================================

@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float


@dataclass
class PositionDetail:
    """Full broker view of one open position (dashboard / monitoring)."""
    symbol: str
    side: str                      # long / short
    qty: float                     # signed as reported by the broker (negative for shorts)
    avg_entry: float
    current_price: Optional[float]
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float         # fraction, e.g. 0.012 = +1.2 %
    unrealized_intraday_pl: float
    change_today: float            # fraction
    qty_available: float


@dataclass
class OpenOrder:
    id: str
    symbol: str
    side: str
    qty: float
    submitted_at: Optional[datetime]
    order_class: str = ""
    order_type: str = ""
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    status: str = ""


@dataclass
class BracketStop:
    """The live stop-loss leg protecting a filled bracket position."""
    order_id: str
    stop_price: float
    status: str
    parent_id: str = ""
    target_order_id: str = ""
    target_price: Optional[float] = None


class AlpacaPaperBroker:
    """Thin wrapper over alpaca-py's TradingClient restricted to paper accounts."""

    def __init__(self, key_id: str, secret_key: str, label: str = ""):
        from alpaca.trading.client import TradingClient  # lazy import: optional dependency

        self.label = label
        self._key_id, self._secret_key = key_id, secret_key
        self._data = None
        self._tc = TradingClient(key_id, secret_key, paper=True)
        acct = self._tc.get_account()
        self.account_number = str(acct.account_number)
        if not self.account_number.startswith("PA"):
            raise RuntimeError(
                f"Alpaca account {self.account_number} is NOT a paper account; refusing to trade"
            )

    # --- account state -------------------------------------------------
    def equity(self) -> float:
        return float(self._tc.get_account().equity)

    def buying_power(self) -> float:
        return float(self._tc.get_account().buying_power)

    def positions(self) -> Dict[str, Position]:
        out: Dict[str, Position] = {}
        for p in self._tc.get_all_positions():
            out[str(p.symbol).upper()] = Position(
                symbol=str(p.symbol).upper(), qty=float(p.qty), avg_entry=float(p.avg_entry_price)
            )
        return out

    def position_details(self) -> List[PositionDetail]:
        """Every open position with the broker's live valuation fields (dashboard view)."""
        out: List[PositionDetail] = []
        for p in self._tc.get_all_positions():
            side = str(getattr(getattr(p, "side", ""), "value", getattr(p, "side", "")) or "").lower()
            out.append(PositionDetail(
                symbol=str(p.symbol).upper(),
                side="short" if side == "short" else "long",
                qty=float(p.qty),
                avg_entry=float(p.avg_entry_price),
                current_price=_float_or_none(getattr(p, "current_price", None)),
                market_value=float(getattr(p, "market_value", 0) or 0),
                cost_basis=float(getattr(p, "cost_basis", 0) or 0),
                unrealized_pl=float(getattr(p, "unrealized_pl", 0) or 0),
                unrealized_plpc=float(getattr(p, "unrealized_plpc", 0) or 0),
                unrealized_intraday_pl=float(getattr(p, "unrealized_intraday_pl", 0) or 0),
                change_today=float(getattr(p, "change_today", 0) or 0),
                qty_available=float(getattr(p, "qty_available", p.qty) or 0),
            ))
        out.sort(key=lambda d: d.symbol)
        return out

    def account_snapshot(self) -> Dict[str, Any]:
        """Account balances as plain floats (equity, last_equity = previous close, cash, buying power)."""
        acct = self._tc.get_account()
        created = getattr(acct, "created_at", None)
        return {
            "label": self.label,
            "account_number": self.account_number,
            "equity": float(acct.equity or 0),
            "last_equity": float(getattr(acct, "last_equity", 0) or 0),
            "cash": float(getattr(acct, "cash", 0) or 0),
            "buying_power": float(acct.buying_power or 0),
            "portfolio_value": float(getattr(acct, "portfolio_value", 0) or 0),
            "long_market_value": float(getattr(acct, "long_market_value", 0) or 0),
            "short_market_value": float(getattr(acct, "short_market_value", 0) or 0),
            "created_at": created.isoformat() if hasattr(created, "isoformat") else (str(created) if created else None),
        }

    def clock(self) -> Dict[str, Any]:
        clk = self._tc.get_clock()

        def _iso(value: Any) -> Optional[str]:
            return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)

        return {
            "is_open": bool(clk.is_open),
            "next_open": _iso(getattr(clk, "next_open", None)),
            "next_close": _iso(getattr(clk, "next_close", None)),
        }

    def portfolio_history_since(self, day: datetime) -> Optional[Dict[str, Any]]:
        """Daily equity history from the start of ``day``: ``{"base_value", "points": [(date, equity)]}``.

        ``base_value`` is the account equity at the start of ``day``. It anchors
        "P&L since the bot's first trade": live equity minus it captures
        realized, unrealized and manual closes alike, unaffected by whatever the
        account did before the bot took over; ``equity - base_value`` per point is
        the bot's cumulative P&L curve. None when the history call fails.
        """
        try:
            from alpaca.trading.requests import GetPortfolioHistoryRequest

            start = day if day.tzinfo else day.replace(tzinfo=timezone.utc)
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            hist = self._tc.get_portfolio_history(GetPortfolioHistoryRequest(start=start, timeframe="1D"))
            base = getattr(hist, "base_value", None)
            if base is None:
                return None
            points: List[Tuple[str, float]] = []
            for ts, equity in zip(getattr(hist, "timestamp", None) or [], getattr(hist, "equity", None) or []):
                if equity is None:
                    continue
                when = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
                points.append((when, float(equity)))
            return {"base_value": float(base), "points": points}
        except Exception as exc:
            logger.warning("Portfolio history since %s unavailable: %s", day, exc)
            return None

    def equity_at_start_of(self, day: datetime) -> Optional[float]:
        """Account equity at the start of ``day`` (portfolio-history base value), or None."""
        hist = self.portfolio_history_since(day)
        return hist["base_value"] if hist else None

    def open_orders(self) -> List[OpenOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        out: List[OpenOrder] = []
        for o in orders:
            submitted = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            out.append(
                OpenOrder(
                    id=str(o.id),
                    symbol=str(o.symbol).upper(),
                    side=str(getattr(o.side, "value", o.side)).lower(),
                    qty=float(o.qty or 0),
                    submitted_at=submitted,
                    order_class=str(getattr(getattr(o, "order_class", ""), "value", getattr(o, "order_class", "")) or ""),
                    order_type=str(getattr(getattr(o, "order_type", ""), "value", getattr(o, "order_type", "")) or ""),
                    limit_price=_float_or_none(getattr(o, "limit_price", None)),
                    stop_price=_float_or_none(getattr(o, "stop_price", None)),
                    status=str(getattr(getattr(o, "status", ""), "value", getattr(o, "status", "")) or ""),
                )
            )
        return out

    def is_market_open(self) -> bool:
        return bool(self._tc.get_clock().is_open)

    def latest_price(self, symbol: str) -> Optional[float]:
        """Latest trade price from the Alpaca data API, or None when unavailable."""
        try:
            if self._data is None:
                from alpaca.data.historical import StockHistoricalDataClient

                self._data = StockHistoricalDataClient(self._key_id, self._secret_key)
            from alpaca.data.requests import StockLatestTradeRequest

            trades = self._data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
            price = float(trades[symbol].price)
            return price if price > 0 else None
        except Exception as exc:
            logger.warning("Latest price for %s unavailable: %s", symbol, exc)
            return None

    # --- orders ----------------------------------------------------------
    def submit_bracket_buy(
        self, symbol: str, qty: int, limit_price: float, stop_price: float, target_price: float
    ) -> str:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=_round_price(limit_price),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=_round_price(target_price)),
            stop_loss=StopLossRequest(stop_price=_round_price(stop_price)),
        )
        order = self._tc.submit_order(req)
        return str(order.id)

    def submit_bracket_sell_short(
        self, symbol: str, qty: int, limit_price: float, stop_price: float, target_price: float
    ) -> str:
        """Short entry: sell-short limit with buy-stop above and cover target below."""
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest

        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            limit_price=_round_price(limit_price),
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=_round_price(target_price)),
            stop_loss=StopLossRequest(stop_price=_round_price(stop_price)),
        )
        order = self._tc.submit_order(req)
        return str(order.id)

    def asset_shortable(self, symbol: str) -> bool:
        """True when Alpaca marks the asset shortable and easy-to-borrow."""
        try:
            asset = self._tc.get_asset(symbol)
            return bool(getattr(asset, "shortable", False)) and bool(getattr(asset, "easy_to_borrow", False))
        except Exception as exc:
            logger.warning("Shortable check for %s failed: %s", symbol, exc)
            return False

    def sell_market(self, symbol: str, qty: float) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = self._tc.submit_order(req)
        return str(order.id)

    def close_position(self, symbol: str) -> str:
        # Cancel attached bracket legs first; Alpaca rejects closing a position
        # that still has open orders against it. Cancels are asynchronous and
        # the OCO stop leg (status "held", invisible to the open-orders query)
        # keeps the shares held_for_orders until the cancel propagates, so
        # retry the close until the hold releases.
        import time

        self.cancel_symbol_orders(symbol)
        last_exc: Optional[Exception] = None
        for attempt in range(10):
            try:
                resp = self._tc.close_position(symbol)
                return str(getattr(resp, "id", "") or "")
            except Exception as exc:
                if "insufficient qty" not in str(exc) and "40310000" not in str(exc):
                    raise
                last_exc = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"close {symbol}: shares still held for orders after cancel: {last_exc}")

    def bracket_stop(self, symbol: str, entry_side: str = "buy") -> Optional[BracketStop]:
        """Live stop-loss leg of the most recent filled bracket for ``symbol``, or None.

        The OCO stop leg sits in status ``held`` and is not returned by the
        open-orders query, so look it up through the filled parent (nested).
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        live = {"held", "new", "accepted", "pending_new", "partially_filled", "pending_replace"}
        orders = self._tc.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=[symbol.upper()], limit=50, nested=True,
        ))
        for parent in orders:  # newest first
            if str(getattr(parent.status, "value", parent.status)) != "filled" or not getattr(parent, "legs", None):
                continue
            if str(getattr(parent.side, "value", parent.side)).lower() != entry_side:
                continue
            stop_leg = target_leg = None
            for leg in parent.legs:
                status = str(getattr(leg.status, "value", leg.status))
                if status not in live:
                    continue
                if getattr(leg, "stop_price", None) is not None:
                    stop_leg = leg
                elif getattr(leg, "limit_price", None) is not None:
                    target_leg = leg
            if stop_leg is None:
                continue
            return BracketStop(
                order_id=str(stop_leg.id),
                stop_price=float(stop_leg.stop_price),
                status=str(getattr(stop_leg.status, "value", stop_leg.status)),
                parent_id=str(parent.id),
                target_order_id=str(target_leg.id) if target_leg is not None else "",
                target_price=float(target_leg.limit_price) if target_leg is not None else None,
            )
        return None

    def order_with_legs(self, order_id: str) -> Optional[Dict[str, Any]]:
        """One order (with nested legs) as a plain dict, or None when not found."""
        try:
            from alpaca.trading.requests import GetOrderByIdRequest

            o = self._tc.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True))
        except Exception as exc:
            logger.info("Order %s lookup failed: %s", order_id, exc)
            return None

        def _plain(order: Any) -> Dict[str, Any]:
            return {
                "id": str(order.id),
                "status": str(getattr(order.status, "value", order.status)),
                "side": str(getattr(order.side, "value", order.side)).lower(),
                "filled_qty": getattr(order, "filled_qty", None),
                "filled_avg_price": getattr(order, "filled_avg_price", None),
                "filled_at": getattr(order, "filled_at", None),
                "stop_price": getattr(order, "stop_price", None),
                "limit_price": getattr(order, "limit_price", None),
            }

        out = _plain(o)
        out["legs"] = [_plain(leg) for leg in (getattr(o, "legs", None) or [])]
        return out

    def first_flattening_fill(self, symbol: str, *, side: str, after: Any, qty: float) -> Optional[Dict[str, Any]]:
        """Earliest filled ``side`` order for ``symbol`` after ``after`` (a direct position close)."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            orders = self._tc.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol.upper()], limit=100))
        except Exception as exc:
            logger.info("Fill search for %s failed: %s", symbol, exc)
            return None
        matches = []
        for o in orders:
            try:
                if str(getattr(o.side, "value", o.side)).lower() != side:
                    continue
                if not o.filled_at or not o.filled_avg_price or float(o.filled_qty or 0) <= 0:
                    continue
                if after is not None and o.filled_at <= after:
                    continue
                matches.append(o)
            except Exception:
                continue
        if not matches:
            return None
        first = min(matches, key=lambda o: o.filled_at)
        return {"price": float(first.filled_avg_price), "at": first.filled_at, "qty": float(first.filled_qty)}

    def daily_bars(self, symbol: str, *, start: Any, limit: int = 10) -> List[Dict[str, Any]]:
        """Daily OHLC bars since ``start`` (IEX feed), oldest first."""
        if self._data is None:
            from alpaca.data.historical import StockHistoricalDataClient

            self._data = StockHistoricalDataClient(self._key_id, self._secret_key)
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        bars = self._data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start))
        out = []
        for b in (bars.data.get(symbol) or [])[:limit]:
            out.append({"date": str(b.timestamp.date()), "open": round(float(b.open), 2),
                        "high": round(float(b.high), 2), "low": round(float(b.low), 2),
                        "close": round(float(b.close), 2)})
        return out

    def replace_stop(self, order_id: str, new_stop: float) -> str:
        """Raise/lower a stop leg in place. Alpaca issues a new order id; returns it."""
        from alpaca.trading.requests import ReplaceOrderRequest

        order = self._tc.replace_order_by_id(order_id, ReplaceOrderRequest(stop_price=_round_price(new_stop)))
        return str(order.id)

    def cancel_symbol_orders(self, symbol: str) -> int:
        count = 0
        for o in self.open_orders():
            if o.symbol == symbol.upper():
                try:
                    self._tc.cancel_order_by_id(o.id)
                    count += 1
                except Exception as exc:  # pragma: no cover - network
                    logger.warning("Cancel order %s for %s failed: %s", o.id, symbol, exc)
        return count

    def cancel_order(self, order_id: str) -> None:
        self._tc.cancel_order_by_id(order_id)


def _round_price(value: float) -> float:
    # US equities: sub-penny increments are rejected for prices >= $1.
    return round(float(value), 2) if float(value) >= 1 else round(float(value), 4)


def _wilder_atr_from_bars(bars: List[Dict[str, Any]], period: int = 14) -> Optional[float]:
    """Wilder-smoothed ATR over daily OHLC dicts (oldest first).

    Same recursion as ``ewm(alpha=1/period, adjust=False)`` used by the analyzer,
    so the number matches the report's ``computed_trade_levels.atr``. None when
    fewer than ``period + 1`` usable bars.
    """
    true_ranges: List[float] = []
    prev_close: Optional[float] = None
    for bar in bars or []:
        try:
            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])
        except (KeyError, TypeError, ValueError):
            continue
        tr = high - low if prev_close is None else max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
        prev_close = close
    if len(true_ranges) < period + 1:
        return None
    atr = true_ranges[0]
    for tr in true_ranges[1:]:
        atr += (tr - atr) / period
    return atr if atr > 0 else None


def discover_accounts(env: Optional[Dict[str, Any]] = None) -> List[str]:
    env = env if env is not None else os.environ
    labels = []
    for key in env:
        m = _ACCOUNT_KEY_RE.match(str(key))
        if m and env.get(f"ALPACA_{m.group(1)}_SECRET_KEY"):
            labels.append(m.group(1))
    return sorted(labels)


def connect_account(settings: PaperTradingSettings, env: Optional[Dict[str, Any]] = None) -> AlpacaPaperBroker:
    """Connect to the configured account, or with ``auto`` the reachable one with the most equity."""
    env = env if env is not None else os.environ
    candidates = [settings.account] if settings.account not in ("AUTO", "auto", "") else discover_accounts(env)
    if not candidates:
        raise RuntimeError("No Alpaca paper credentials found (ALPACA_<X>_KEY_ID / _SECRET_KEY)")

    best: Optional[Tuple[float, AlpacaPaperBroker]] = None
    errors: List[str] = []
    for label in candidates:
        key, secret = env.get(f"ALPACA_{label}_KEY_ID"), env.get(f"ALPACA_{label}_SECRET_KEY")
        if not key or not secret:
            errors.append(f"{label}: missing credentials")
            continue
        try:
            broker = AlpacaPaperBroker(key, secret, label=label)
            eq = broker.equity()
            logger.info("Alpaca paper account %s (%s): equity $%.2f", label, broker.account_number, eq)
            if best is None or eq > best[0]:
                best = (eq, broker)
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:120]}")
            logger.warning("Alpaca paper account %s unreachable: %s", label, exc)
    if best is None:
        raise RuntimeError("No reachable Alpaca paper account: " + "; ".join(errors))
    return best[1]


# ============================================================
# Execution service
# ============================================================

@dataclass
class TradeDecision:
    symbol: str
    signal_id: Optional[int]
    action: str                 # signal action: buy/add/reduce/sell/hold/watch/avoid
    side: Optional[str] = None  # buy / sell
    qty: Optional[int] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    status: str = "skipped"     # submitted / dry_run / skipped / error
    reason: str = ""
    order_id: Optional[str] = None
    risk_usd: Optional[float] = None
    r_multiple: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "signal_id": self.signal_id, "action": self.action, "side": self.side,
            "qty": self.qty, "limit_price": self.limit_price, "stop_price": self.stop_price,
            "target_price": self.target_price, "status": self.status, "reason": self.reason,
            "order_id": self.order_id, "risk_usd": self.risk_usd, "r_multiple": self.r_multiple,
            "extra": self.extra,
        }


class PaperTradingService:
    NEW_ENTRY_ACTIONS = ("buy", "add")
    EXIT_ACTIONS = ("sell",)
    TRIM_ACTIONS = ("reduce",)
    SHORT_ENTRY_ACTIONS = ("sell", "avoid")   # bearish actions that may carry a short_plan

    def __init__(
        self,
        settings: PaperTradingSettings,
        broker: Optional[AlpacaPaperBroker] = None,
        *,
        signal_service: Any = None,
        db: Any = None,
        env: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ):
        self.settings = settings
        self._broker = broker
        self._signal_service = signal_service
        self._db = db
        self._env = env
        self._now = now

    # --- lazy collaborators --------------------------------------------
    @property
    def broker(self) -> AlpacaPaperBroker:
        if self._broker is None:
            self._broker = connect_account(self.settings, self._env)
        return self._broker

    @property
    def signal_service(self):
        if self._signal_service is None:
            from src.services.decision_signal_service import DecisionSignalService
            self._signal_service = DecisionSignalService()
        return self._signal_service

    @property
    def db(self):
        if self._db is None:
            from src.storage import get_db
            self._db = get_db()
        return self._db

    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    # --- public API ------------------------------------------------------
    def kill_switch_active(self) -> bool:
        return Path(self.settings.kill_switch_path).exists()

    def run(self, stock_codes: List[str], *, persist_skips: bool = True) -> Dict[str, Any]:
        """Execute today's signals for ``stock_codes``. Never raises on per-symbol errors.

        ``persist_skips=False`` (the repeating intraday pass) records only actions,
        deferrals and errors — not the identical skip reasons every 15 minutes.
        """
        summary: Dict[str, Any] = {
            "enabled": self.settings.enabled, "dry_run": self.settings.dry_run,
            "account": None, "equity": None, "decisions": [], "cancelled_stale": 0, "error": None,
        }
        if not self.settings.enabled:
            summary["error"] = "disabled"
            return summary
        if self.kill_switch_active():
            logger.warning("Paper trading kill switch present at %s; no orders submitted", self.settings.kill_switch_path)
            summary["error"] = "kill_switch"
            return summary

        try:
            broker = self.broker
            summary["account"] = f"{broker.label}:{broker.account_number}"
            equity = broker.equity()
            summary["equity"] = equity
            positions = broker.positions()
            open_orders = broker.open_orders()
            market_open = broker.is_market_open()
        except Exception as exc:
            logger.error("Paper trading: broker unavailable: %s", exc)
            summary["error"] = f"broker_unavailable: {exc}"
            return summary
        summary["market_open"] = market_open

        cancelled = self._cancel_stale_entries(open_orders)
        summary["cancelled_stale"] = len(cancelled)
        # A just-cancelled entry must not block a fresh signal for the same
        # symbol this run. Exclude by symbol, not order id: the snapshot also
        # holds the cancelled parent's bracket legs, which die with it. Held
        # positions are still protected by the "already holding" check.
        cancelled_symbols = {o.symbol for o in cancelled}
        open_symbols = {o.symbol for o in open_orders if o.symbol not in cancelled_symbols}
        open_position_count = len(positions)

        decisions: List[TradeDecision] = []
        for code in stock_codes:
            symbol = str(code).strip().upper()
            try:
                decision = self._decide(symbol, positions, open_symbols, equity, open_position_count)
                if decision.status == "planned":
                    # A new entry needs a live market: a marketable order submitted
                    # pre-market prices off a stale quote and can fill through its
                    # own stop on an opening gap (seen live: NVDA 2026-09-01).
                    # Defer it; the intraday pass re-decides with real prices.
                    # Exits stay allowed — they reduce risk wherever they queue.
                    if not market_open and decision.side in ("buy", "sell_short") and not decision.extra.get("close"):
                        decision.status = "deferred"
                        decision.reason += " — market closed; deferred to the post-open pass"
                    else:
                        self._execute(decision)
                        if decision.status in ("submitted", "dry_run") and decision.side in ("buy", "sell_short"):
                            open_position_count += 1
                            open_symbols.add(symbol)
            except Exception as exc:
                logger.exception("Paper trading: %s failed", symbol)
                decision = TradeDecision(symbol=symbol, signal_id=None, action="?", status="error", reason=str(exc)[:200])
            if persist_skips or decision.status in ("submitted", "dry_run", "deferred", "error"):
                self._persist(decision)
            decisions.append(decision)

        summary["decisions"] = [d.to_dict() for d in decisions]
        return summary

    # --- open-position management ---------------------------------------
    def manage_positions(self) -> Dict[str, Any]:
        """Ratchet the protective stop of every open bracket position; never lowers a stop.

        Policy (R = entry - initial stop, from the recorded entry order):
        once the latest price is at least ``breakeven_trigger_r`` R above entry,
        the stop becomes ``max(entry * (1 + breakeven_buffer_pct%), price - trail_r * R)``,
        capped ``stop_min_gap_pct`` % below the latest price. Only replaces when the
        new stop is higher than the current one. Runs only while the market is
        open (quotes are stale otherwise). Never raises on per-symbol errors.
        """
        summary: Dict[str, Any] = {
            "enabled": self.settings.enabled, "dry_run": self.settings.dry_run,
            "account": None, "checked": 0, "adjustments": [], "error": None,
        }
        if not self.settings.enabled:
            summary["error"] = "disabled"
            return summary
        if self.kill_switch_active():
            logger.warning("Paper trading kill switch present at %s; stops left untouched", self.settings.kill_switch_path)
            summary["error"] = "kill_switch"
            return summary
        try:
            broker = self.broker
            summary["account"] = f"{broker.label}:{broker.account_number}"
            if not broker.is_market_open():
                summary["error"] = "market_closed"
                logger.info("Paper trading: market closed; stop management skipped")
                return summary
            positions = broker.positions()
        except Exception as exc:
            logger.error("Paper trading: broker unavailable: %s", exc)
            summary["error"] = f"broker_unavailable: {exc}"
            return summary

        for symbol, pos in sorted(positions.items()):
            if not pos.qty:
                continue
            summary["checked"] += 1
            try:
                d = self._manage_one(symbol, pos)
            except Exception as exc:
                logger.exception("Paper trading: stop management for %s failed", symbol)
                d = TradeDecision(symbol=symbol, signal_id=None, action="trail_stop", status="error", reason=str(exc)[:200])
            if d.status in ("submitted", "dry_run", "error", "unprotected"):
                self._persist(d)
                summary["adjustments"].append(d.to_dict())
            else:
                logger.info("Paper trading: %s stop unchanged (%s)", symbol, d.reason)
        return summary

    def _manage_one(self, symbol: str, pos: Position) -> TradeDecision:
        is_short = pos.qty < 0
        d = TradeDecision(symbol=symbol, signal_id=None, action="trail_stop",
                          side="buy" if is_short else "sell", qty=int(abs(pos.qty)))
        leg = self.broker.bracket_stop(symbol, entry_side="sell" if is_short else "buy")
        if leg is None:
            d.status, d.reason = "unprotected", "no live bracket stop found for this position"
            logger.warning("Paper trading: %s x%s has NO protective stop", symbol, pos.qty)
            return d
        price = self.broker.latest_price(symbol)
        if price is None:
            d.reason = "latest price unavailable"
            return d

        entry, cur_stop = float(pos.avg_entry), float(leg.stop_price)
        risk = self._initial_risk(symbol, entry, cur_stop, is_short=is_short)
        d.extra.update({"entry": entry, "price": price, "old_stop": cur_stop, "risk_per_share": round(risk, 4),
                        "stop_order_id": leg.order_id, "target_price": leg.target_price,
                        "direction": "short" if is_short else "long"})
        if risk <= 0:
            d.reason = f"cannot determine initial risk (entry {entry}, stop {cur_stop})"
            return d
        gain_r = ((entry - price) if is_short else (price - entry)) / risk
        d.r_multiple = round(gain_r, 2)
        if gain_r < self.settings.breakeven_trigger_r:
            d.reason = f"{gain_r:+.2f}R, below break-even trigger {self.settings.breakeven_trigger_r}R"
            return d

        buf = self.settings.breakeven_buffer_pct / 100.0
        gap = self.settings.stop_min_gap_pct / 100.0
        if is_short:
            # Mirror image: the protective stop sits ABOVE the price and ratchets DOWN.
            breakeven = entry * (1 - buf)
            trail = price + self.settings.trail_r * risk
            candidate = _round_price(max(min(breakeven, trail), price * (1 + gap)))
            improved = candidate < cur_stop - 0.005
        else:
            breakeven = entry * (1 + buf)
            trail = price - self.settings.trail_r * risk
            candidate = _round_price(min(max(breakeven, trail), price * (1 - gap)))
            improved = candidate > cur_stop + 0.005
        if not improved:
            d.reason = f"{gain_r:+.2f}R; stop {cur_stop} already at/beyond computed {candidate}"
            return d

        d.stop_price = candidate
        locked = ((entry - candidate) if is_short else (candidate - entry)) * abs(pos.qty)
        d.extra["locked_in_usd"] = round(locked, 2)
        at_breakeven = abs(candidate - breakeven) <= 0.005 or (candidate >= breakeven if is_short else candidate <= breakeven)
        d.reason = (f"{gain_r:+.2f}R: stop {cur_stop} -> {candidate} "
                    f"({'break-even' if at_breakeven else 'trailing'}, locks ${locked:,.0f})")
        if self.settings.dry_run:
            d.status = "dry_run"
            logger.info("Paper trading [DRY RUN] %s %s", symbol, d.reason)
            return d
        d.order_id = self.broker.replace_stop(leg.order_id, candidate)
        d.status = "submitted"
        logger.info("Paper trading: %s %s (stop order %s -> %s)", symbol, d.reason, leg.order_id, d.order_id)
        return d

    def _initial_risk(self, symbol: str, entry: float, current_stop: float, *, is_short: bool = False) -> float:
        """Per-share risk of the original plan (entry to the *initial* stop).

        Anchored to the recorded entry order so a ratcheted stop does not shrink R.
        Falls back to the distance to the current stop (exact until the first ratchet).
        """
        try:
            account = getattr(self._broker, "account_number", None) if self._broker else None
            rec = self.db.get_latest_paper_entry(
                account=account, symbol=symbol, side="sell_short" if is_short else "buy"
            )
            if rec is not None and rec.limit_price and rec.stop_price:
                if is_short and rec.stop_price > rec.limit_price:
                    return (float(rec.stop_price) - float(entry)) if float(rec.stop_price) > float(entry) \
                        else float(rec.stop_price) - float(rec.limit_price)
                if not is_short and rec.stop_price < rec.limit_price:
                    return (float(entry) - float(rec.stop_price)) if float(rec.stop_price) < float(entry) \
                        else float(rec.limit_price) - float(rec.stop_price)
        except Exception as exc:
            logger.debug("Paper trading: initial-risk lookup failed for %s: %s", symbol, exc)
        return (float(current_stop) - float(entry)) if is_short else (float(entry) - float(current_stop))

    # --- decision --------------------------------------------------------
    def _latest_signal(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            result = self.signal_service.get_latest_active(stock_code=symbol, market="us", limit=1)
        except Exception as exc:
            logger.warning("Paper trading: signal lookup failed for %s: %s", symbol, exc)
            return None
        items = result.get("items") if isinstance(result, dict) else None
        if not items:
            return None
        signal = items[0]
        created = _parse_dt(signal.get("created_at"))
        if created is not None and self.now() - created > timedelta(hours=36):
            logger.info("Paper trading: latest signal for %s is stale (%s); ignoring", symbol, created)
            return None
        return signal

    def _decide(
        self,
        symbol: str,
        positions: Dict[str, Position],
        open_symbols: set,
        equity: float,
        open_position_count: int,
    ) -> TradeDecision:
        signal = self._latest_signal(symbol)
        if signal is None:
            return TradeDecision(symbol=symbol, signal_id=None, action="none", reason="no fresh signal")
        if str(signal.get("market") or "us").lower() != "us":
            return TradeDecision(symbol=symbol, signal_id=signal.get("id"), action=str(signal.get("action")), reason="non-US symbol")

        action = str(signal.get("action") or "").lower()
        d = TradeDecision(symbol=symbol, signal_id=signal.get("id"), action=action)
        held = positions.get(symbol)
        held_long = held is not None and held.qty > 0
        held_short = held is not None and held.qty < 0

        # A bullish signal against an open short is an exit signal for the short.
        if action in self.NEW_ENTRY_ACTIONS and held_short:
            d.side, d.qty, d.status = "buy", int(abs(held.qty)), "planned"
            d.reason = "bullish flip; covering short"
            d.extra["close"] = True
            return d

        if action in self.EXIT_ACTIONS:
            if held_long:
                d.side, d.qty, d.status, d.reason = "sell", int(held.qty), "planned", "close position"
                d.extra["close"] = True
                return d
            if held_short:
                d.reason = "already short; bearish signal keeps the position"
                return d
            # No position: a bearish signal may still be an executable short (below).

        if action in self.TRIM_ACTIONS:
            if not held_long:
                d.reason = "reduce signal but no long position"
                return d
            qty = int(math.floor(held.qty / 2))
            if qty < 1:
                d.reason = "position too small to trim"
                return d
            d.side, d.qty, d.status, d.reason = "sell", qty, "planned", "trim half"
            return d

        if action in self.SHORT_ENTRY_ACTIONS:
            return self._decide_short_entry(d, signal, open_symbols, equity, open_position_count)

        if action not in self.NEW_ENTRY_ACTIONS:
            d.reason = f"no order for action '{action}'"
            return d

        # --- new entry -----------------------------------------------------
        if held:
            d.reason = "already holding; not adding"
            return d
        if symbol in open_symbols:
            d.reason = "open order already pending"
            return d
        if open_position_count >= self.settings.max_positions:
            d.reason = f"max positions ({self.settings.max_positions}) reached"
            return d
        if self._entry_already_submitted_today(symbol):
            d.reason = "an entry for this symbol was already submitted today"
            return d

        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        plan_check = metadata.get("plan_check") if isinstance(metadata.get("plan_check"), dict) else None
        if plan_check and plan_check.get("valid") is False:
            d.reason = f"plan failed validation: {', '.join(map(str, plan_check.get('issues') or []))}"
            return d
        if self._earnings_within_horizon(signal):
            d.reason = "earnings within horizon; skipping new entry"
            return d

        entry_low, entry_high = _float_or_none(signal.get("entry_low")), _float_or_none(signal.get("entry_high"))
        stop, target = _float_or_none(signal.get("stop_loss")), _float_or_none(signal.get("target_price"))
        entry = entry_high or entry_low
        if entry is None or stop is None or target is None:
            d.reason = "missing entry/stop/target"
            return d
        if not (stop < entry < target):
            d.reason = f"invalid levels (stop {stop}, entry {entry}, target {target})"
            return d

        # Stop distance floor: the stop must sit at least stop_min_atr_mult ATR
        # below the planned entry; a tighter plan stop is widened and every
        # gate below runs on the widened geometry.
        atr = self._atr_for(symbol, signal)
        plan_stop = stop
        stop = self._floored_stop(entry, stop, atr)

        def widened_note() -> str:
            return (f" (stop widened {plan_stop} -> {stop}, {self.settings.stop_min_atr_mult:g}×ATR floor)"
                    if stop != plan_stop else "")

        # Plan-quality gate at the planned entry: the plan itself must promise
        # at least min_r_multiple.
        plan_r = (target - entry) / (entry - stop)
        if plan_r < self.settings.min_r_multiple:
            d.r_multiple = round(plan_r, 2)
            d.reason = f"R:R {plan_r:.2f} below {self.settings.min_r_multiple}" + widened_note()
            return d

        # If the market already trades above the planned entry, chase with a
        # marketable limit instead of resting below the market: latest price
        # plus chase_pct. Execution accepts R:R degradation at the chased
        # price down to chase_min_r_multiple (a valid plan may be taken at a
        # slightly worse price; a bad plan is rejected above regardless). At
        # or below the planned entry the original limit is already marketable
        # and fills on its own.
        price = None
        try:
            price = self.broker.latest_price(symbol)
        except Exception as exc:
            logger.warning("Paper trading: quote lookup failed for %s: %s", symbol, exc)
        if price is not None and price > entry:
            chased = price * (1 + self.settings.chase_pct / 100.0)
            # Chasing moves the entry away from the stop, so the floor applied
            # at the planned entry still holds at the chased price.
            chase_r = (target - chased) / (chased - stop) if stop < chased < target else -1.0
            if chase_r < self.settings.chase_min_r_multiple:
                d.r_multiple = round(chase_r, 2)
                d.reason = (f"price ${price:.2f} ran past entry {entry}: "
                            f"R:R {chase_r:.2f} below chase floor {self.settings.chase_min_r_multiple}" + widened_note())
                return d
            d.extra["chased"] = {"planned_entry": entry, "latest_price": price}
            entry = chased

        risk_per_share = entry - stop
        d.r_multiple = round((target - entry) / risk_per_share, 2)
        if stop != plan_stop:
            d.extra["stop_widened"] = {"plan_stop": plan_stop, "stop": stop, "atr": round(atr, 4),
                                       "min_atr_mult": self.settings.stop_min_atr_mult}

        qty_by_risk = int(math.floor(self.settings.risk_per_trade_usd / risk_per_share))
        max_notional = equity * self.settings.max_position_pct / 100.0
        qty_by_notional = int(math.floor(max_notional / entry))
        qty = min(qty_by_risk, qty_by_notional)
        if qty < 1:
            d.reason = f"size < 1 share (risk/share ${risk_per_share:.2f}, cap ${max_notional:.0f})"
            return d

        d.side, d.qty = "buy", qty
        d.limit_price, d.stop_price, d.target_price = _round_price(entry), _round_price(stop), _round_price(target)
        d.risk_usd = round(qty * risk_per_share, 2)
        d.status = "planned"
        d.reason = "bracket entry (chased to market)" if "chased" in d.extra else "bracket entry"
        d.reason += widened_note()
        d.extra.update({"entry_low": entry_low, "entry_high": entry_high, "atr": round(atr, 4) if atr else None,
                        "qty_by_risk": qty_by_risk, "qty_by_notional": qty_by_notional})
        return d

    def _decide_short_entry(
        self,
        d: TradeDecision,
        signal: Dict[str, Any],
        open_symbols: set,
        equity: float,
        open_position_count: int,
    ) -> TradeDecision:
        """Bearish signal on a stock we don't hold: execute its short_plan as a short bracket.

        Mirror of the long entry path: plan gate at the planned entry, chase *down*
        with a marketable limit when price has already fallen below it, sizing by
        risk at the stop (which sits ABOVE entry).
        """
        symbol = d.symbol
        no_order = f"no order for action '{d.action}'"
        if not self.settings.allow_short:
            d.reason = no_order + " (shorting disabled)"
            return d
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        plan = metadata.get("short_plan") if isinstance(metadata.get("short_plan"), dict) else None
        if not plan:
            d.reason = no_order + " (no short plan)"
            return d
        if symbol in open_symbols:
            d.reason = "open order already pending"
            return d
        if open_position_count >= self.settings.max_positions:
            d.reason = f"max positions ({self.settings.max_positions}) reached"
            return d
        if self._entry_already_submitted_today(symbol):
            d.reason = "an entry for this symbol was already submitted today"
            return d
        if self._earnings_within_horizon(signal):
            d.reason = "earnings within horizon; skipping new entry"
            return d

        entry = _float_or_none(plan.get("entry"))
        stop = _float_or_none(plan.get("stop"))
        target = _float_or_none(plan.get("target"))
        if entry is None or stop is None or target is None:
            d.reason = "short plan missing entry/stop/target"
            return d
        if not (target < entry < stop):
            d.reason = f"invalid short levels (target {target}, entry {entry}, stop {stop})"
            return d

        # Mirror of the long stop distance floor: the buy-stop sits at least
        # stop_min_atr_mult ATR above the entry.
        atr = self._atr_for(symbol, signal)
        plan_stop = stop
        stop = self._floored_stop(entry, stop, atr, is_short=True)

        def widened_note() -> str:
            return (f" (stop widened {plan_stop} -> {stop}, {self.settings.stop_min_atr_mult:g}×ATR floor)"
                    if stop != plan_stop else "")

        plan_r = (entry - target) / (stop - entry)
        if plan_r < self.settings.min_r_multiple:
            d.r_multiple = round(plan_r, 2)
            d.reason = f"short R:R {plan_r:.2f} below {self.settings.min_r_multiple}" + widened_note()
            return d

        price = None
        try:
            price = self.broker.latest_price(symbol)
        except Exception as exc:
            logger.warning("Paper trading: quote lookup failed for %s: %s", symbol, exc)
        if price is not None and price < entry:
            chased = price * (1 - self.settings.chase_pct / 100.0)
            chase_r = (chased - target) / (stop - chased) if target < chased < stop else -1.0
            if chase_r < self.settings.chase_min_r_multiple:
                d.r_multiple = round(chase_r, 2)
                d.reason = (f"price ${price:.2f} ran below short entry {entry}: "
                            f"R:R {chase_r:.2f} below chase floor {self.settings.chase_min_r_multiple}" + widened_note())
                return d
            d.extra["chased"] = {"planned_entry": entry, "latest_price": price}
            entry = chased

        if not self.broker.asset_shortable(symbol):
            d.reason = "not shortable / not easy-to-borrow at Alpaca"
            return d

        risk_per_share = stop - entry
        d.r_multiple = round((entry - target) / risk_per_share, 2)
        qty_by_risk = int(math.floor(self.settings.risk_per_trade_usd / risk_per_share))
        max_notional = equity * self.settings.max_position_pct / 100.0
        qty_by_notional = int(math.floor(max_notional / entry))
        qty = min(qty_by_risk, qty_by_notional)
        if qty < 1:
            d.reason = f"size < 1 share (risk/share ${risk_per_share:.2f}, cap ${max_notional:.0f})"
            return d

        d.side, d.qty = "sell_short", qty
        d.limit_price, d.stop_price, d.target_price = _round_price(entry), _round_price(stop), _round_price(target)
        d.risk_usd = round(qty * risk_per_share, 2)
        d.status = "planned"
        d.reason = "short bracket entry (chased to market)" if "chased" in d.extra else "short bracket entry"
        d.reason += widened_note()
        if stop != plan_stop:
            d.extra["stop_widened"] = {"plan_stop": plan_stop, "stop": stop, "atr": round(atr, 4),
                                       "min_atr_mult": self.settings.stop_min_atr_mult}
        d.extra.update({"atr": round(atr, 4) if atr else None, "qty_by_risk": qty_by_risk, "qty_by_notional": qty_by_notional})
        return d

    # --- stop distance floor ---------------------------------------------
    def _atr_for(self, symbol: str, signal: Dict[str, Any]) -> Optional[float]:
        """ATR(14) behind the stop-distance floor.

        The source report's system reference levels (``computed_trade_levels.atr``
        in the stored context snapshot) come first, so the floor uses the same
        volatility the analyst was shown; otherwise it is recomputed from the
        broker's daily bars. None when neither is available.
        """
        report_id = signal.get("source_report_id")
        if report_id:
            try:
                record = self.db.get_analysis_history_by_id(int(report_id))
                raw = getattr(record, "context_snapshot", None)
                snapshot = json.loads(raw) if isinstance(raw, str) else (raw or {})
                enhanced = snapshot.get("enhanced_context") if isinstance(snapshot, dict) else None
                levels = enhanced.get("computed_trade_levels") if isinstance(enhanced, dict) else None
                atr = _float_or_none(levels.get("atr")) if isinstance(levels, dict) else None
                if atr:
                    return atr
            except Exception as exc:
                logger.debug("Paper trading: report ATR lookup failed for %s: %s", symbol, exc)
        try:
            bars = self.broker.daily_bars(symbol, start=self.now() - timedelta(days=45), limit=40)
            return _wilder_atr_from_bars(bars)
        except Exception as exc:
            logger.warning("Paper trading: ATR from daily bars unavailable for %s: %s", symbol, exc)
            return None

    def _floored_stop(self, entry: float, stop: float, atr: Optional[float], *, is_short: bool = False) -> float:
        """``stop`` pushed out to at least ``stop_min_atr_mult`` ATR from ``entry``.

        A plan stop inside one day's average range is not a thesis invalidation,
        it is noise: seen live on BKR 2026-09-04 (stop 0.56 ATR below entry,
        hit 90 minutes after the fill) and MSFT 2026-09-02 (0.7 ATR). The analyst
        writes such stops as "a close below MA10", but the bracket leg fires on
        a tick. Widening keeps the dollar risk (fewer shares) and lets the R:R
        gates reject plans whose target cannot pay for a survivable stop.
        Unchanged when ATR is unknown or the floor is disabled.
        """
        mult = self.settings.stop_min_atr_mult
        if not atr or mult <= 0:
            return stop
        floor = entry + mult * atr if is_short else entry - mult * atr
        if (stop >= floor) if is_short else (stop <= floor):
            return stop
        return _round_price(floor)

    def _entry_already_submitted_today(self, symbol: str) -> bool:
        """True when a real entry (long or short) for ``symbol`` was already submitted today (UTC).

        Blocks intraday re-entry loops: without it the post-open pass would happily
        re-enter a symbol right after its stop closed the position.
        """
        try:
            account = getattr(self._broker, "account_number", None) if self._broker else None
            return bool(self.db.has_paper_entry_today(account=account, symbol=symbol))
        except Exception as exc:
            logger.debug("Paper trading: same-day entry lookup failed for %s: %s", symbol, exc)
            return False

    def _recent_short_entry_ids(self) -> set:
        try:
            account = getattr(self._broker, "account_number", None) if self._broker else None
            return set(self.db.get_recent_paper_entry_order_ids(
                account=account, side="sell_short", days=self.settings.entry_ttl_days + 7))
        except Exception as exc:
            logger.debug("Paper trading: short entry id lookup failed: %s", exc)
            return set()

    def _earnings_within_horizon(self, signal: Dict[str, Any]) -> bool:
        metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
        if metadata.get("earnings_within_horizon") is True:
            return True
        report_id = signal.get("source_report_id")
        if not report_id:
            return False
        try:
            record = self.db.get_analysis_history_by_id(int(report_id))
            raw = getattr(record, "raw_result", None)
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
            dashboard = parsed.get("dashboard") if isinstance(parsed, dict) else None
            return bool(isinstance(dashboard, dict) and dashboard.get("earnings_within_horizon"))
        except Exception as exc:
            logger.debug("Paper trading: earnings flag lookup failed for %s: %s", signal.get("stock_code"), exc)
            return False

    # --- execution -------------------------------------------------------
    def _execute(self, d: TradeDecision) -> None:
        if self.settings.dry_run:
            d.status = "dry_run"
            logger.info("Paper trading [DRY RUN] %s %s x%s limit=%s stop=%s target=%s (%s)",
                        d.side, d.symbol, d.qty, d.limit_price, d.stop_price, d.target_price, d.reason)
            return
        try:
            if d.extra.get("close"):
                d.order_id = self.broker.close_position(d.symbol)
            elif d.side == "buy":
                d.order_id = self.broker.submit_bracket_buy(d.symbol, d.qty, d.limit_price, d.stop_price, d.target_price)
            elif d.side == "sell_short":
                d.order_id = self.broker.submit_bracket_sell_short(d.symbol, d.qty, d.limit_price, d.stop_price, d.target_price)
            else:
                d.order_id = self.broker.sell_market(d.symbol, d.qty)
            d.status = "submitted"
            logger.info("Paper trading: submitted %s %s x%s (order %s): %s", d.side, d.symbol, d.qty, d.order_id, d.reason)
        except Exception as exc:
            d.status, d.reason = "error", f"{d.reason}; submit failed: {str(exc)[:200]}"
            logger.error("Paper trading: order for %s failed: %s", d.symbol, exc)

    def _cancel_stale_entries(self, open_orders: List[OpenOrder]) -> List[OpenOrder]:
        cutoff = self.now() - timedelta(days=self.settings.entry_ttl_days)
        cancelled: List[OpenOrder] = []
        # Sell-side open orders are usually protective bracket legs of live LONG
        # positions — never TTL those. A short *entry* is also sell-side, so it
        # is identified by its recorded order id instead of by side.
        short_entry_ids = self._recent_short_entry_ids()
        for o in open_orders:
            if o.submitted_at is None:
                continue
            if o.side != "buy" and o.id not in short_entry_ids:
                continue
            submitted = o.submitted_at if o.submitted_at.tzinfo else o.submitted_at.replace(tzinfo=timezone.utc)
            if submitted < cutoff:
                if self.settings.dry_run:
                    logger.info("Paper trading [DRY RUN] would cancel stale entry %s %s", o.symbol, o.id)
                    continue
                try:
                    self.broker.cancel_order(o.id)
                    cancelled.append(o)
                    logger.info("Paper trading: cancelled stale entry %s %s (submitted %s)", o.symbol, o.id, submitted)
                except Exception as exc:
                    logger.warning("Paper trading: cancel %s failed: %s", o.id, exc)
        return cancelled

    # --- persistence -----------------------------------------------------
    def _persist(self, d: TradeDecision) -> None:
        try:
            from src.storage import PaperTradeRecord
            account = getattr(self._broker, "account_number", None) if self._broker else None
            with self.db.session_scope() as session:
                session.add(PaperTradeRecord(
                    account=account, symbol=d.symbol, signal_id=d.signal_id, action=d.action, side=d.side,
                    qty=d.qty, limit_price=d.limit_price, stop_price=d.stop_price, target_price=d.target_price,
                    risk_usd=d.risk_usd, r_multiple=d.r_multiple, status=d.status, reason=d.reason,
                    order_id=d.order_id, dry_run=bool(self.settings.dry_run),
                    raw_json=json.dumps(d.to_dict(), ensure_ascii=False, default=str),
                ))
        except Exception as exc:
            logger.warning("Paper trading: persisting trade record failed: %s", exc)


# ============================================================
# Report formatting
# ============================================================

def format_summary(summary: Dict[str, Any]) -> str:
    """Compact, phone-friendly summary for the notification channel."""
    if not summary or summary.get("error") == "disabled":
        return ""
    lines = ["🧾 **Paper Orders**"]
    if summary.get("error"):
        lines.append(f"Not executed: {summary['error']}")
        return "\n".join(lines)
    acct = summary.get("account") or "?"
    eq = summary.get("equity")
    lines.append(f"Account {acct} · equity ${eq:,.0f}" if isinstance(eq, (int, float)) else f"Account {acct}")
    if summary.get("dry_run"):
        lines.append("DRY RUN — nothing was sent to the broker")
    acted = [d for d in summary.get("decisions", []) if d.get("status") in ("submitted", "dry_run", "error")]
    skipped = [d for d in summary.get("decisions", []) if d.get("status") == "skipped"]
    deferred = [d for d in summary.get("decisions", []) if d.get("status") == "deferred"]
    for d in acted:
        err = " ❌ " + d["reason"] if d["status"] == "error" else ""
        widened = (d.get("extra") or {}).get("stop_widened")
        widened_txt = f" · stop widened from {widened['plan_stop']} (ATR floor)" if widened else ""
        if d.get("side") == "buy" and not (d.get("extra") or {}).get("close"):
            lines.append(
                f"🟢 BUY {d['symbol']} x{d['qty']} @≤{d['limit_price']} · stop {d['stop_price']} · "
                f"target {d['target_price']} · risk ${d.get('risk_usd', 0):,.0f} · R {d.get('r_multiple')}" + widened_txt + err
            )
        elif d.get("side") == "sell_short":
            lines.append(
                f"🔻 SHORT {d['symbol']} x{d['qty']} @≥{d['limit_price']} · stop {d['stop_price']} · "
                f"target {d['target_price']} · risk ${d.get('risk_usd', 0):,.0f} · R {d.get('r_multiple')}" + widened_txt + err
            )
        elif d.get("side") == "buy":
            lines.append(f"🟦 COVER {d['symbol']} x{d['qty']} — {d['reason']}" + (" ❌" if d["status"] == "error" else ""))
        else:
            lines.append(f"🔴 SELL {d['symbol']} x{d['qty']} — {d['reason']}" + (" ❌" if d["status"] == "error" else ""))
    if not acted:
        lines.append("No orders today.")
    if deferred:
        lines.append("Deferred to the post-open pass: " + "; ".join(
            f"{'SHORT ' if d.get('side') == 'sell_short' else ''}{d['symbol']}" for d in deferred))
    if skipped:
        lines.append("Skipped: " + "; ".join(f"{d['symbol']} ({d['reason']})" for d in skipped))
    if summary.get("cancelled_stale"):
        lines.append(f"Cancelled {summary['cancelled_stale']} stale unfilled entr{'y' if summary['cancelled_stale']==1 else 'ies'}.")
    return "\n".join(lines)


def format_manage_summary(summary: Dict[str, Any], *, quiet: bool = False) -> str:
    """Summary of the stop-management pass. With ``quiet`` returns "" when nothing changed."""
    if not summary or summary.get("error") == "disabled":
        return ""
    adjustments = summary.get("adjustments") or []
    if quiet and not adjustments and summary.get("error") in (None, "market_closed"):
        return ""
    lines = ["🛡️ **Stop Management**"]
    if summary.get("error"):
        lines.append(f"Not run: {summary['error']}")
        return "\n".join(lines)
    if summary.get("dry_run"):
        lines.append("DRY RUN — nothing was sent to the broker")
    for d in adjustments:
        sym = d["symbol"]
        if d["status"] == "unprotected":
            lines.append(f"⚠️ {sym}: NO protective stop on an open position")
        elif d["status"] == "error":
            lines.append(f"❌ {sym}: {d['reason']}")
        else:
            lines.append(f"⬆️ {sym} {d['reason']}")
    if not adjustments:
        lines.append(f"Checked {summary.get('checked', 0)} position(s); no stop changes.")
    return "\n".join(lines)


# ============================================================
# helpers
# ============================================================

def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        f = float(value)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
