# -*- coding: utf-8 -*-
"""
Paper-trading dashboard: live account / position view plus manual position closes.

Read side (``snapshot``) combines the broker's live state (balances, positions
with unrealized P&L, protective bracket legs, open orders, market clock) with
the local audit trail (``paper_trades`` / ``trade_postmortems``).

P&L definition
--------------
"Total P&L" is the bot's P&L, not the account's lifetime P&L: the paper account
may have traded before the bot took over. It is anchored at the day of the
bot's first real entry order and computed as ``live equity - equity at the
start of that day`` (Alpaca portfolio history ``base_value``), which captures
realized, unrealized and manual closes the moment they happen. When the
history call is unavailable the figure degrades to
``realized (post-mortem loop) + unrealized (open positions)`` and the payload
says so via ``total_pnl_basis`` so the UI can label it.

Write side (``close_position`` / ``close_all_positions``) goes through the
same broker wrapper the bot uses (bracket legs cancelled first, retry while
the OCO hold releases) and records every manual close in ``paper_trades`` so
the post-mortem loop reviews it like any other exit. Closing is risk-reducing
and therefore allowed while the kill switch is set; ``PAPER_TRADING_DRY_RUN``
is honoured (recorded, not sent).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.services.paper_trading_service import (
    AlpacaPaperBroker,
    PaperTradingSettings,
    PositionDetail,
    connect_account,
)

logger = logging.getLogger(__name__)

MANUAL_CLOSE_ACTION = "manual_close"


class PaperTradingUnavailableError(RuntimeError):
    """No paper account is configured or reachable."""


class PositionNotFoundError(LookupError):
    """The requested symbol is not an open position."""


class PaperDashboardService:
    def __init__(
        self,
        settings: PaperTradingSettings,
        broker: Optional[AlpacaPaperBroker] = None,
        *,
        db: Any = None,
        env: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
        recent_trades_limit: int = 25,
    ):
        self.settings = settings
        self._broker = broker
        self._db = db
        self._env = env
        self._now = now
        self.recent_trades_limit = recent_trades_limit

    # --- collaborators ---------------------------------------------------
    @property
    def broker(self) -> AlpacaPaperBroker:
        if self._broker is None:
            try:
                self._broker = connect_account(self.settings, self._env)
            except Exception as exc:
                raise PaperTradingUnavailableError(str(exc)) from exc
        return self._broker

    @property
    def db(self):
        if self._db is None:
            from src.storage import get_db
            self._db = get_db()
        return self._db

    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def kill_switch_active(self) -> bool:
        return Path(self.settings.kill_switch_path).exists()

    # --- read ------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        broker = self.broker
        account = broker.account_snapshot()
        account_number = account.get("account_number")
        positions = broker.position_details()
        open_orders = broker.open_orders()
        clock = self._clock()

        position_items = [self._position_item(p, account_number) for p in positions]
        unrealized = round(sum(p.unrealized_pl for p in positions), 2)
        pnl, pnl_history = self._pnl(account, account_number, unrealized)

        gross_exposure = round(sum(abs(p.market_value) for p in positions), 2)
        equity = float(account.get("equity") or 0)

        return {
            "generated_at": self.now().isoformat(),
            "status": {
                "enabled": bool(self.settings.enabled),
                "dry_run": bool(self.settings.dry_run),
                "kill_switch_active": self.kill_switch_active(),
                "allow_short": bool(self.settings.allow_short),
                "market_open": bool(clock.get("is_open")),
                "next_open": clock.get("next_open"),
                "next_close": clock.get("next_close"),
                "max_positions": int(self.settings.max_positions),
            },
            "account": account,
            "pnl": pnl,
            "pnl_history": pnl_history,
            "exposure": {
                "open_positions": len(positions),
                "gross_exposure": gross_exposure,
                "gross_exposure_pct": round(gross_exposure / equity * 100, 2) if equity else 0.0,
                "long_market_value": float(account.get("long_market_value") or 0),
                "short_market_value": float(account.get("short_market_value") or 0),
            },
            "positions": position_items,
            "open_orders": [self._order_item(o) for o in open_orders],
            "recent_trades": self._recent_trades(account_number),
        }

    def _clock(self) -> Dict[str, Any]:
        try:
            return self.broker.clock()
        except Exception as exc:
            logger.warning("Market clock unavailable: %s", exc)
            return {"is_open": False, "next_open": None, "next_close": None}

    def _pnl(
        self, account: Dict[str, Any], account_number: Optional[str], unrealized: float
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """P&L summary plus the bot's cumulative P&L curve (one point per trading day, last point live)."""
        equity = float(account.get("equity") or 0)
        last_equity = float(account.get("last_equity") or 0)
        realized = self._realized()

        since: Optional[datetime] = None
        baseline: Optional[float] = None
        history: List[Dict[str, Any]] = []
        try:
            since = self.db.get_first_paper_entry_at(account_number)
        except Exception as exc:
            logger.warning("First paper entry lookup failed: %s", exc)
        if since is not None:
            hist = self.broker.portfolio_history_since(since)
            if hist and hist.get("base_value"):
                baseline = float(hist["base_value"])
                history = [
                    {"date": day, "equity": round(eq, 2), "pnl": round(eq - baseline, 2)}
                    for day, eq in hist.get("points", [])
                ]
                today = self.now().date().isoformat()
                live = {"date": today, "equity": round(equity, 2), "pnl": round(equity - baseline, 2)}
                if history and history[-1]["date"] >= today:
                    history[-1] = live
                else:
                    history.append(live)

        if baseline:
            total = round(equity - baseline, 2)
            basis = "account_equity_since_first_trade"
            total_pct = round(total / baseline * 100, 2)
        else:
            total = round(float(realized.get("realized_pnl") or 0) + unrealized, 2)
            basis = "realized_plus_unrealized"
            total_pct = round(total / equity * 100, 2) if equity else 0.0

        day = round(equity - last_equity, 2) if last_equity else 0.0
        return ({
            "total_pnl": total,
            "total_pnl_pct": total_pct,
            "total_pnl_basis": basis,
            "since": since.date().isoformat() if since else None,
            "baseline_equity": round(baseline, 2) if baseline else None,
            "day_pnl": day,
            "day_pnl_pct": round(day / last_equity * 100, 2) if last_equity else 0.0,
            "unrealized_pnl": unrealized,
            "realized_pnl": float(realized.get("realized_pnl") or 0),
            "closed_trades": int(realized.get("closed_trades") or 0),
            "wins": int(realized.get("wins") or 0),
            "losses": int(realized.get("losses") or 0),
        }, history)

    def _realized(self) -> Dict[str, Any]:
        try:
            return self.db.get_realized_paper_pnl()
        except Exception as exc:
            logger.warning("Realized paper P&L lookup failed: %s", exc)
            return {"realized_pnl": 0.0, "closed_trades": 0, "wins": 0, "losses": 0}

    def _position_item(self, p: PositionDetail, account_number: Optional[str]) -> Dict[str, Any]:
        is_short = p.side == "short"
        stop_price: Optional[float] = None
        target_price: Optional[float] = None
        try:
            leg = self.broker.bracket_stop(p.symbol, entry_side="sell" if is_short else "buy")
            if leg is not None:
                stop_price, target_price = leg.stop_price, leg.target_price
        except Exception as exc:
            logger.warning("Bracket stop lookup for %s failed: %s", p.symbol, exc)

        initial_stop: Optional[float] = None
        try:
            entry = self.db.get_latest_paper_entry(account_number, p.symbol, side="sell_short" if is_short else "buy")
            if entry is not None and entry.stop_price:
                initial_stop = float(entry.stop_price)
        except Exception as exc:
            logger.warning("Recorded entry lookup for %s failed: %s", p.symbol, exc)

        r_multiple: Optional[float] = None
        if initial_stop and p.avg_entry:
            risk_per_share = abs(p.avg_entry - initial_stop)
            if risk_per_share > 0:
                r_multiple = round(p.unrealized_pl / (abs(p.qty) * risk_per_share), 2)

        return {
            "symbol": p.symbol,
            "side": p.side,
            "qty": abs(p.qty),
            "avg_entry": p.avg_entry,
            "current_price": p.current_price,
            "market_value": p.market_value,
            "cost_basis": p.cost_basis,
            "unrealized_pl": p.unrealized_pl,
            "unrealized_plpc": p.unrealized_plpc,
            "unrealized_intraday_pl": p.unrealized_intraday_pl,
            "change_today": p.change_today,
            "stop_price": stop_price,
            "target_price": target_price,
            "initial_stop": initial_stop,
            "r_multiple": r_multiple,
            "protected": stop_price is not None,
        }

    @staticmethod
    def _order_item(o: Any) -> Dict[str, Any]:
        submitted = getattr(o, "submitted_at", None)
        return {
            "id": o.id,
            "symbol": o.symbol,
            "side": o.side,
            "qty": o.qty,
            "order_class": getattr(o, "order_class", "") or "",
            "order_type": getattr(o, "order_type", "") or "",
            "limit_price": getattr(o, "limit_price", None),
            "stop_price": getattr(o, "stop_price", None),
            "status": getattr(o, "status", "") or "",
            "submitted_at": submitted.isoformat() if hasattr(submitted, "isoformat") else (str(submitted) if submitted else None),
        }

    def _recent_trades(self, account_number: Optional[str]) -> List[Dict[str, Any]]:
        try:
            return self.db.list_recent_paper_trades(account_number, limit=self.recent_trades_limit)
        except Exception as exc:
            logger.warning("Recent paper trades lookup failed: %s", exc)
            return []

    # --- write -----------------------------------------------------------
    def close_position(self, symbol: str, *, reason: str = "dashboard: close position") -> Dict[str, Any]:
        symbol = str(symbol).upper().strip()
        positions = {p.symbol: p for p in self.broker.position_details()}
        if symbol not in positions:
            raise PositionNotFoundError(f"{symbol} is not an open position")
        return self._close(positions[symbol], reason)

    def close_all_positions(self) -> Dict[str, Any]:
        positions = self.broker.position_details()
        results = [self._close(p, "dashboard: close all") for p in positions]
        closed = sum(1 for r in results if r["status"] in ("submitted", "dry_run"))
        return {
            "requested": len(results),
            "closed": closed,
            "failed": len(results) - closed,
            "results": results,
        }

    def _close(self, p: PositionDetail, reason: str) -> Dict[str, Any]:
        side = "buy_to_cover" if p.side == "short" else "sell"
        result: Dict[str, Any] = {
            "symbol": p.symbol,
            "side": side,
            "qty": abs(p.qty),
            "status": "",
            "order_id": None,
            "message": "",
        }
        if self.settings.dry_run:
            result["status"] = "dry_run"
            result["message"] = "dry run: close recorded, not sent"
            logger.info("Paper dashboard [DRY RUN] would close %s x%s (%s)", p.symbol, abs(p.qty), reason)
        else:
            try:
                result["order_id"] = self.broker.close_position(p.symbol)
                result["status"] = "submitted"
                result["message"] = "close order submitted"
                logger.info("Paper dashboard: close %s x%s submitted (order %s): %s", p.symbol, abs(p.qty), result["order_id"], reason)
            except Exception as exc:
                result["status"] = "error"
                result["message"] = str(exc)[:200]
                logger.error("Paper dashboard: close %s failed: %s", p.symbol, exc)
        self._persist(p, result, reason)
        return result

    def _persist(self, p: PositionDetail, result: Dict[str, Any], reason: str) -> None:
        try:
            from src.storage import PaperTradeRecord

            account = getattr(self._broker, "account_number", None) if self._broker else None
            text = reason if result["status"] != "error" else f"{reason}; close failed: {result['message']}"
            with self.db.session_scope() as session:
                session.add(PaperTradeRecord(
                    account=account, symbol=p.symbol, signal_id=None, action=MANUAL_CLOSE_ACTION,
                    side=result["side"], qty=abs(p.qty), limit_price=None, stop_price=None, target_price=None,
                    risk_usd=None, r_multiple=None, status=result["status"], reason=text,
                    order_id=result.get("order_id"), dry_run=bool(self.settings.dry_run),
                    raw_json=json.dumps({
                        "avg_entry": p.avg_entry, "current_price": p.current_price,
                        "unrealized_pl": p.unrealized_pl, "market_value": p.market_value,
                    }, ensure_ascii=False, default=str),
                ))
        except Exception as exc:
            logger.warning("Paper dashboard: persisting manual close failed: %s", exc)
