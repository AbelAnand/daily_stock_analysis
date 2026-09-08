# -*- coding: utf-8 -*-
"""Paper-trading dashboard service tests (no network; fake broker + fake DB)."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone

from src.services.paper_dashboard_service import (
    MANUAL_CLOSE_ACTION,
    PaperDashboardService,
    PaperTradingUnavailableError,
    PositionNotFoundError,
)
from src.services.paper_trading_service import (
    BracketStop,
    OpenOrder,
    PaperTradingSettings,
    PositionDetail,
)

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)


def _pos(symbol, qty=10, entry=100.0, price=110.0, side="long"):
    signed = qty if side == "long" else -qty
    pl = (price - entry) * qty if side == "long" else (entry - price) * qty
    return PositionDetail(
        symbol=symbol, side=side, qty=signed, avg_entry=entry, current_price=price,
        market_value=price * signed, cost_basis=entry * qty, unrealized_pl=pl,
        unrealized_plpc=pl / (entry * qty), unrealized_intraday_pl=pl / 2, change_today=0.01,
        qty_available=signed,
    )


class FakeBroker:
    label = "B"
    account_number = "PA_TEST"

    def __init__(self, positions=None, equity=31000.0, last_equity=30500.0, base_value=30000.0, stops=None, market_open=True):
        self._positions = positions or []
        self._equity = equity
        self._last_equity = last_equity
        self._base_value = base_value
        self._stops = stops or {}
        self._market_open = market_open
        self.closed = []
        self.fail_close = set()
        self.history_calls = []

    def account_snapshot(self):
        return {
            "label": self.label, "account_number": self.account_number, "equity": self._equity,
            "last_equity": self._last_equity, "cash": 20000.0, "buying_power": 80000.0,
            "portfolio_value": self._equity, "long_market_value": 11000.0, "short_market_value": 0.0,
            "created_at": "2026-05-21T09:35:40+00:00",
        }

    def position_details(self):
        return list(self._positions)

    def open_orders(self):
        return [OpenOrder(id="o1", symbol="AAPL", side="buy", qty=5, submitted_at=NOW, order_class="bracket",
                          order_type="limit", limit_price=180.0, status="new")]

    def clock(self):
        return {"is_open": self._market_open, "next_open": "2026-09-05T13:30:00+00:00", "next_close": "2026-09-04T20:00:00+00:00"}

    def portfolio_history_since(self, day):
        self.history_calls.append(day)
        if self._base_value is None:
            return None
        return {"base_value": self._base_value, "points": [("2026-08-22", self._base_value), ("2026-08-25", self._base_value + 120.0), ("2026-09-03", self._base_value + 400.0)]}

    def bracket_stop(self, symbol, entry_side="buy"):
        return self._stops.get(symbol)

    def close_position(self, symbol):
        if symbol in self.fail_close:
            raise RuntimeError("insufficient qty available")
        self.closed.append(symbol)
        self._positions = [p for p in self._positions if p.symbol != symbol]
        return f"close-{symbol}"


class FakeEntry:
    def __init__(self, stop_price):
        self.stop_price = stop_price


class FakeDB:
    def __init__(self, first_entry=datetime(2026, 8, 22, 1, 25), entries=None, realized=None):
        self.rows = []
        self.first_entry = first_entry
        self.entries = entries or {}
        self.realized = realized or {"realized_pnl": -44.22, "closed_trades": 2, "wins": 0, "losses": 1}

    @contextmanager
    def session_scope(self):
        yield self

    def add(self, row):
        self.rows.append(row)

    def get_first_paper_entry_at(self, account):
        return self.first_entry

    def get_realized_paper_pnl(self):
        return dict(self.realized)

    def get_latest_paper_entry(self, account, symbol, side="buy"):
        return self.entries.get(symbol)

    def list_recent_paper_trades(self, account, limit=20, include_skipped=False):
        return [{"id": 1, "created_at": "2026-09-03T12:11:59", "symbol": "GOOGL", "action": "buy", "side": "buy",
                 "qty": 17, "limit_price": 343.61, "stop_price": 330.0, "target_price": 370.0, "status": "submitted",
                 "reason": "chase", "order_id": "abc", "dry_run": False}]


def _service(broker=None, db=None, now=NOW, **settings_kw):
    kw = dict(enabled=True, account="B", kill_switch_path=os.path.join(tempfile.gettempdir(), "no-such-kill-switch"))
    kw.update(settings_kw)
    return PaperDashboardService(PaperTradingSettings(**kw), broker or FakeBroker(), db=db or FakeDB(), now=now)


class SnapshotTestCase(unittest.TestCase):
    def test_total_pnl_is_anchored_at_first_bot_trade(self):
        broker = FakeBroker(positions=[_pos("GOOGL", qty=17, entry=343.61, price=350.0)], equity=31000.0, base_value=30000.0)
        snap = _service(broker=broker).snapshot()
        self.assertEqual(snap["pnl"]["total_pnl"], 1000.0)
        self.assertEqual(snap["pnl"]["total_pnl_basis"], "account_equity_since_first_trade")
        self.assertEqual(snap["pnl"]["since"], "2026-08-22")
        self.assertEqual(snap["pnl"]["baseline_equity"], 30000.0)
        self.assertEqual(broker.history_calls, [datetime(2026, 8, 22, 1, 25)])
        self.assertEqual(snap["pnl"]["day_pnl"], 500.0)
        self.assertAlmostEqual(snap["pnl"]["unrealized_pnl"], round((350.0 - 343.61) * 17, 2))
        self.assertEqual(snap["pnl"]["realized_pnl"], -44.22)
        self.assertEqual(snap["pnl"]["closed_trades"], 2)
        # Curve: history points as equity - baseline, plus a live point for today.
        self.assertEqual([(pt["date"], pt["pnl"]) for pt in snap["pnl_history"]],
                         [("2026-08-22", 0.0), ("2026-08-25", 120.0), ("2026-09-03", 400.0), ("2026-09-04", 1000.0)])

    def test_live_point_replaces_todays_history_point(self):
        broker = FakeBroker(positions=[], equity=31000.0, base_value=30000.0)
        svc = _service(broker=broker, now=datetime(2026, 9, 3, 18, 0, tzinfo=timezone.utc))
        snap = svc.snapshot()
        self.assertEqual(snap["pnl_history"][-1], {"date": "2026-09-03", "equity": 31000.0, "pnl": 1000.0})
        self.assertEqual(len(snap["pnl_history"]), 3)

    def test_total_pnl_falls_back_to_realized_plus_unrealized(self):
        broker = FakeBroker(positions=[_pos("GOOGL", qty=10, entry=100.0, price=110.0)], base_value=None)
        snap = _service(broker=broker).snapshot()
        self.assertEqual(snap["pnl"]["total_pnl_basis"], "realized_plus_unrealized")
        self.assertAlmostEqual(snap["pnl"]["total_pnl"], round(-44.22 + 100.0, 2))
        self.assertEqual(snap["pnl_history"], [])

    def test_no_bot_trades_yet_uses_fallback_without_history_call(self):
        broker = FakeBroker(positions=[])
        snap = _service(broker=broker, db=FakeDB(first_entry=None, realized={"realized_pnl": 0, "closed_trades": 0, "wins": 0, "losses": 0})).snapshot()
        self.assertEqual(broker.history_calls, [])
        self.assertEqual(snap["pnl"]["total_pnl"], 0.0)
        self.assertIsNone(snap["pnl"]["since"])

    def test_position_carries_stop_target_and_r_multiple(self):
        stops = {"GOOGL": BracketStop(order_id="s1", stop_price=340.0, status="held", target_price=380.0)}
        broker = FakeBroker(positions=[_pos("GOOGL", qty=10, entry=100.0, price=110.0)], stops=stops)
        db = FakeDB(entries={"GOOGL": FakeEntry(stop_price=95.0)})
        item = _service(broker=broker, db=db).snapshot()["positions"][0]
        self.assertEqual(item["stop_price"], 340.0)
        self.assertEqual(item["target_price"], 380.0)
        self.assertTrue(item["protected"])
        self.assertEqual(item["initial_stop"], 95.0)
        self.assertEqual(item["r_multiple"], 2.0)  # +$100 on 10 x $5 risk
        self.assertEqual(item["qty"], 10)

    def test_short_position_is_reported_with_positive_qty_and_short_side(self):
        broker = FakeBroker(positions=[_pos("NIO", qty=50, entry=10.0, price=9.0, side="short")])
        item = _service(broker=broker).snapshot()["positions"][0]
        self.assertEqual(item["side"], "short")
        self.assertEqual(item["qty"], 50)
        self.assertEqual(item["unrealized_pl"], 50.0)
        self.assertFalse(item["protected"])

    def test_status_and_exposure(self):
        broker = FakeBroker(positions=[_pos("A", qty=10, entry=100.0, price=100.0), _pos("B", qty=5, entry=200.0, price=200.0)], equity=20000.0)
        snap = _service(broker=broker, dry_run=True, allow_short=True).snapshot()
        self.assertEqual(snap["exposure"]["open_positions"], 2)
        self.assertEqual(snap["exposure"]["gross_exposure"], 2000.0)
        self.assertEqual(snap["exposure"]["gross_exposure_pct"], 10.0)
        self.assertTrue(snap["status"]["dry_run"])
        self.assertTrue(snap["status"]["allow_short"])
        self.assertTrue(snap["status"]["market_open"])
        self.assertFalse(snap["status"]["kill_switch_active"])
        self.assertEqual(snap["open_orders"][0]["limit_price"], 180.0)
        self.assertEqual(snap["recent_trades"][0]["symbol"], "GOOGL")

    def test_kill_switch_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            open(path, "w").close()
            snap = _service(kill_switch_path=path).snapshot()
            self.assertTrue(snap["status"]["kill_switch_active"])

    def test_unreachable_account_raises_unavailable(self):
        svc = PaperDashboardService(PaperTradingSettings(enabled=True, account="ZZZ"), env={}, db=FakeDB(), now=NOW)
        with self.assertRaises(PaperTradingUnavailableError):
            svc.snapshot()


class CloseTestCase(unittest.TestCase):
    def test_close_one_position_submits_and_records(self):
        broker = FakeBroker(positions=[_pos("GOOGL", qty=17), _pos("MSFT", qty=3)])
        db = FakeDB()
        result = _service(broker=broker, db=db).close_position("googl")
        self.assertEqual(result["status"], "submitted")
        self.assertEqual(result["order_id"], "close-GOOGL")
        self.assertEqual(result["side"], "sell")
        self.assertEqual(result["qty"], 17)
        self.assertEqual(broker.closed, ["GOOGL"])
        self.assertEqual(len(db.rows), 1)
        row = db.rows[0]
        self.assertEqual(row.action, MANUAL_CLOSE_ACTION)
        self.assertEqual(row.side, "sell")
        self.assertEqual(row.status, "submitted")
        self.assertEqual(row.account, "PA_TEST")
        self.assertIn("dashboard", row.reason)

    def test_close_short_is_buy_to_cover(self):
        broker = FakeBroker(positions=[_pos("NIO", qty=50, side="short")])
        result = _service(broker=broker).close_position("NIO")
        self.assertEqual(result["side"], "buy_to_cover")
        self.assertEqual(result["qty"], 50)

    def test_close_unknown_symbol_is_not_found(self):
        with self.assertRaises(PositionNotFoundError):
            _service(broker=FakeBroker(positions=[_pos("GOOGL")])).close_position("TSLA")

    def test_close_failure_is_reported_not_raised(self):
        broker = FakeBroker(positions=[_pos("GOOGL")])
        broker.fail_close.add("GOOGL")
        db = FakeDB()
        result = _service(broker=broker, db=db).close_position("GOOGL")
        self.assertEqual(result["status"], "error")
        self.assertIn("insufficient qty", result["message"])
        self.assertEqual(db.rows[0].status, "error")

    def test_dry_run_records_without_sending(self):
        broker = FakeBroker(positions=[_pos("GOOGL")])
        db = FakeDB()
        result = _service(broker=broker, db=db, dry_run=True).close_position("GOOGL")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(broker.closed, [])
        self.assertTrue(db.rows[0].dry_run)

    def test_close_all_reports_partial_failure(self):
        broker = FakeBroker(positions=[_pos("A"), _pos("B"), _pos("C")])
        broker.fail_close.add("B")
        summary = _service(broker=broker).close_all_positions()
        self.assertEqual(summary["requested"], 3)
        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(broker.closed, ["A", "C"])
        self.assertEqual([r["status"] for r in summary["results"]], ["submitted", "error", "submitted"])

    def test_close_all_with_nothing_open(self):
        summary = _service(broker=FakeBroker(positions=[])).close_all_positions()
        self.assertEqual(summary, {"requested": 0, "closed": 0, "failed": 0, "results": []})

    def test_close_allowed_while_kill_switch_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "STOP")
            open(path, "w").close()
            broker = FakeBroker(positions=[_pos("GOOGL")])
            result = _service(broker=broker, kill_switch_path=path).close_position("GOOGL")
            self.assertEqual(result["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
