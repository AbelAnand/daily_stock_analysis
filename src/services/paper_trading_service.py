# -*- coding: utf-8 -*-
"""
Paper-trading execution of daily decision signals via the Alpaca API.

Runs after the daily analysis. For each analyzed US symbol it reads the latest
active decision signal and turns it into an order on an Alpaca PAPER account:

- buy / add  -> bracket order: GTC limit entry at the top of the entry range with
                an attached stop-loss and take-profit. Sized so that a stop-out
                loses at most ``risk_per_trade_usd`` (default $500).
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
    min_r_multiple: float = 1.5
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
class OpenOrder:
    id: str
    symbol: str
    side: str
    qty: float
    submitted_at: Optional[datetime]
    order_class: str = ""


class AlpacaPaperBroker:
    """Thin wrapper over alpaca-py's TradingClient restricted to paper accounts."""

    def __init__(self, key_id: str, secret_key: str, label: str = ""):
        from alpaca.trading.client import TradingClient  # lazy import: optional dependency

        self.label = label
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
                )
            )
        return out

    def is_market_open(self) -> bool:
        return bool(self._tc.get_clock().is_open)

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

    def sell_market(self, symbol: str, qty: float) -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
        order = self._tc.submit_order(req)
        return str(order.id)

    def close_position(self, symbol: str) -> str:
        # Cancel attached bracket legs first; Alpaca rejects closing a position
        # that still has open orders against it.
        self.cancel_symbol_orders(symbol)
        resp = self._tc.close_position(symbol)
        return str(getattr(resp, "id", "") or "")

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

    def run(self, stock_codes: List[str]) -> Dict[str, Any]:
        """Execute today's signals for ``stock_codes``. Never raises on per-symbol errors."""
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
        except Exception as exc:
            logger.error("Paper trading: broker unavailable: %s", exc)
            summary["error"] = f"broker_unavailable: {exc}"
            return summary

        summary["cancelled_stale"] = self._cancel_stale_entries(open_orders)
        open_symbols = {o.symbol for o in open_orders}
        open_position_count = len(positions)

        decisions: List[TradeDecision] = []
        for code in stock_codes:
            symbol = str(code).strip().upper()
            try:
                decision = self._decide(symbol, positions, open_symbols, equity, open_position_count)
                if decision.status == "planned":
                    self._execute(decision)
                    if decision.status in ("submitted", "dry_run") and decision.side == "buy":
                        open_position_count += 1
                        open_symbols.add(symbol)
            except Exception as exc:
                logger.exception("Paper trading: %s failed", symbol)
                decision = TradeDecision(symbol=symbol, signal_id=None, action="?", status="error", reason=str(exc)[:200])
            self._persist(decision)
            decisions.append(decision)

        summary["decisions"] = [d.to_dict() for d in decisions]
        return summary

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

        if action in self.EXIT_ACTIONS:
            if not held:
                d.reason = "sell signal but no position"
                return d
            d.side, d.qty, d.status, d.reason = "sell", int(held.qty), "planned", "close position"
            d.extra["close"] = True
            return d

        if action in self.TRIM_ACTIONS:
            if not held:
                d.reason = "reduce signal but no position"
                return d
            qty = int(math.floor(held.qty / 2))
            if qty < 1:
                d.reason = "position too small to trim"
                return d
            d.side, d.qty, d.status, d.reason = "sell", qty, "planned", "trim half"
            return d

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
        risk_per_share = entry - stop
        r_multiple = (target - entry) / risk_per_share
        d.r_multiple = round(r_multiple, 2)
        if r_multiple < self.settings.min_r_multiple:
            d.reason = f"R:R {r_multiple:.2f} below {self.settings.min_r_multiple}"
            return d

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
        d.status, d.reason = "planned", "bracket entry"
        d.extra.update({"entry_low": entry_low, "entry_high": entry_high, "qty_by_risk": qty_by_risk, "qty_by_notional": qty_by_notional})
        return d

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
            if d.side == "buy":
                d.order_id = self.broker.submit_bracket_buy(d.symbol, d.qty, d.limit_price, d.stop_price, d.target_price)
            elif d.extra.get("close"):
                d.order_id = self.broker.close_position(d.symbol)
            else:
                d.order_id = self.broker.sell_market(d.symbol, d.qty)
            d.status = "submitted"
            logger.info("Paper trading: submitted %s %s x%s (order %s): %s", d.side, d.symbol, d.qty, d.order_id, d.reason)
        except Exception as exc:
            d.status, d.reason = "error", f"{d.reason}; submit failed: {str(exc)[:200]}"
            logger.error("Paper trading: order for %s failed: %s", d.symbol, exc)

    def _cancel_stale_entries(self, open_orders: List[OpenOrder]) -> int:
        cutoff = self.now() - timedelta(days=self.settings.entry_ttl_days)
        cancelled = 0
        for o in open_orders:
            if o.side != "buy" or o.submitted_at is None:
                continue
            submitted = o.submitted_at if o.submitted_at.tzinfo else o.submitted_at.replace(tzinfo=timezone.utc)
            if submitted < cutoff:
                if self.settings.dry_run:
                    logger.info("Paper trading [DRY RUN] would cancel stale entry %s %s", o.symbol, o.id)
                    continue
                try:
                    self.broker.cancel_order(o.id)
                    cancelled += 1
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
    for d in acted:
        if d.get("side") == "buy":
            lines.append(
                f"🟢 BUY {d['symbol']} x{d['qty']} @≤{d['limit_price']} · stop {d['stop_price']} · "
                f"target {d['target_price']} · risk ${d.get('risk_usd', 0):,.0f} · R {d.get('r_multiple')}"
                + (" ❌ " + d["reason"] if d["status"] == "error" else "")
            )
        else:
            lines.append(f"🔴 SELL {d['symbol']} x{d['qty']} — {d['reason']}" + (" ❌" if d["status"] == "error" else ""))
    if not acted:
        lines.append("No orders today.")
    if skipped:
        lines.append("Skipped: " + "; ".join(f"{d['symbol']} ({d['reason']})" for d in skipped))
    if summary.get("cancelled_stale"):
        lines.append(f"Cancelled {summary['cancelled_stale']} stale unfilled entr{'y' if summary['cancelled_stale']==1 else 'ies'}.")
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
