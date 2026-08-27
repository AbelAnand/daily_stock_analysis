# -*- coding: utf-8 -*-
"""Paper-trading execution service tests (no network; fake broker + fake signals)."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from src.services.paper_trading_service import (
    OpenOrder,
    PaperTradingService,
    PaperTradingSettings,
    Position,
    format_summary,
)

NOW = datetime(2026, 8, 21, 21, 0, tzinfo=timezone.utc)


class FakeBroker:
    label = "B"
    account_number = "PA_TEST"

    def __init__(self, equity=30000.0, positions=None, open_orders=None, prices=None):
        self._equity = equity
        self._positions = positions or {}
        self._open_orders = open_orders or []
        self._prices = prices or {}
        self.submitted = []
        self.cancelled = []
        self.closed = []

    def equity(self):
        return self._equity

    def positions(self):
        return dict(self._positions)

    def open_orders(self):
        return list(self._open_orders)

    def submit_bracket_buy(self, symbol, qty, limit_price, stop_price, target_price):
        self.submitted.append(("buy", symbol, qty, limit_price, stop_price, target_price))
        return f"ord-{symbol}"

    def sell_market(self, symbol, qty):
        self.submitted.append(("sell", symbol, qty))
        return f"sell-{symbol}"

    def close_position(self, symbol):
        self.closed.append(symbol)
        return f"close-{symbol}"

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    def latest_price(self, symbol):
        return self._prices.get(symbol)


class FakeSignals:
    def __init__(self, signals):
        self._signals = signals

    def get_latest_active(self, *, stock_code, market=None, limit=1):
        sig = self._signals.get(stock_code)
        return {"items": [sig] if sig else []}


class FakeDB:
    def __init__(self):
        self.rows = []

    @contextmanager
    def session_scope(self):
        session = self

        yield session

    def add(self, row):
        self.rows.append(row)

    def get_analysis_history_by_id(self, record_id):
        return None


def _signal(symbol, action="buy", entry_low=308.2, entry_high=311.5, stop=299.91, target=333.96, **extra):
    sig = {
        "id": 1, "stock_code": symbol, "market": "us", "action": action,
        "entry_low": entry_low, "entry_high": entry_high, "stop_loss": stop, "target_price": target,
        "created_at": (NOW - timedelta(hours=1)).isoformat(), "metadata": {}, "source_report_id": None,
    }
    sig.update(extra)
    return sig


def _service(settings=None, broker=None, signals=None, db=None):
    settings = settings or PaperTradingSettings(enabled=True, account="B", risk_per_trade_usd=500, kill_switch_path=os.path.join(tempfile.gettempdir(), "no-such-kill-switch"))
    return PaperTradingService(settings, broker or FakeBroker(), signal_service=FakeSignals(signals or {}), db=db or FakeDB(), now=NOW)


class SizingTestCase(unittest.TestCase):
    def test_buy_is_sized_to_risk_and_notional_cap(self):
        broker = FakeBroker(equity=30000.0)
        svc = _service(broker=broker, signals={"AAPL": _signal("AAPL")})
        summary = svc.run(["AAPL"])
        d = summary["decisions"][0]
        self.assertEqual(d["status"], "submitted")
        # risk/share = 311.5 - 299.91 = 11.59 -> 43 by risk; notional cap 20% * 30000 / 311.5 = 19
        self.assertEqual(d["qty"], 19)
        self.assertLessEqual(d["risk_usd"], 500)
        self.assertEqual(broker.submitted[0][:3], ("buy", "AAPL", 19))
        self.assertEqual(d["limit_price"], 311.5)
        self.assertEqual(d["stop_price"], 299.91)
        self.assertEqual(d["target_price"], 333.96)

    def test_risk_cap_binds_when_account_is_large(self):
        broker = FakeBroker(equity=1_000_000.0)
        svc = _service(broker=broker, signals={"AAPL": _signal("AAPL")})
        d = svc.run(["AAPL"])["decisions"][0]
        self.assertEqual(d["qty"], 43)  # floor(500 / 11.59)
        self.assertLessEqual(d["risk_usd"], 500)

    def test_too_small_for_one_share_is_skipped(self):
        broker = FakeBroker(equity=500.0)
        svc = _service(broker=broker, signals={"BRK": _signal("BRK", entry_low=700000, entry_high=700000, stop=690000, target=730000)})
        d = svc.run(["BRK"])["decisions"][0]
        self.assertEqual(d["status"], "skipped")
        self.assertIn("size < 1 share", d["reason"])


class GuardTestCase(unittest.TestCase):
    def test_skips_when_already_holding(self):
        broker = FakeBroker(positions={"AAPL": Position("AAPL", 10, 300.0)})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "skipped")
        self.assertIn("already holding", d["reason"])
        self.assertEqual(broker.submitted, [])

    def test_skips_when_order_pending(self):
        broker = FakeBroker(open_orders=[OpenOrder("o1", "AAPL", "buy", 5, NOW)])
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertIn("pending", d["reason"])

    def test_skips_invalid_plan_and_poor_rr(self):
        svc = _service(signals={
            "BAD": _signal("BAD", metadata={"plan_check": {"valid": False, "issues": ["stop_not_below_entry"]}}),
            "LOWR": _signal("LOWR", entry_high=100, entry_low=100, stop=95, target=104),
        })
        decisions = {d["symbol"]: d for d in svc.run(["BAD", "LOWR"])["decisions"]}
        self.assertIn("plan failed validation", decisions["BAD"]["reason"])
        self.assertIn("R:R", decisions["LOWR"]["reason"])

    def test_skips_earnings_within_horizon(self):
        d = _service(signals={"NVDA": _signal("NVDA", metadata={"earnings_within_horizon": True})}).run(["NVDA"])["decisions"][0]
        self.assertIn("earnings", d["reason"])

    def test_stale_signal_is_ignored(self):
        sig = _signal("AAPL"); sig["created_at"] = (NOW - timedelta(days=3)).isoformat()
        d = _service(signals={"AAPL": sig}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["reason"], "no fresh signal")

    def test_max_positions(self):
        positions = {f"S{i}": Position(f"S{i}", 1, 1.0) for i in range(10)}
        broker = FakeBroker(positions=positions)
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertIn("max positions", d["reason"])

    def test_kill_switch_blocks_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop = os.path.join(tmp, "STOP"); open(stop, "w").close()
            settings = PaperTradingSettings(enabled=True, account="B", kill_switch_path=stop)
            broker = FakeBroker()
            summary = _service(settings=settings, broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])
        self.assertEqual(summary["error"], "kill_switch")
        self.assertEqual(broker.submitted, [])

    def test_dry_run_submits_nothing(self):
        settings = PaperTradingSettings(enabled=True, account="B", dry_run=True, kill_switch_path="/nonexistent/STOP")
        broker = FakeBroker()
        d = _service(settings=settings, broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "dry_run")
        self.assertEqual(broker.submitted, [])


class ExitTestCase(unittest.TestCase):
    def test_sell_closes_position(self):
        broker = FakeBroker(positions={"AAPL": Position("AAPL", 19, 300.0)})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL", action="sell")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "submitted")
        self.assertEqual(broker.closed, ["AAPL"])

    def test_reduce_sells_half(self):
        broker = FakeBroker(positions={"AAPL": Position("AAPL", 19, 300.0)})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL", action="reduce")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(broker.submitted, [("sell", "AAPL", 9)])
        self.assertEqual(d["qty"], 9)

    def test_sell_without_position_is_noop(self):
        broker = FakeBroker()
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL", action="sell")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "skipped")
        self.assertEqual(broker.closed, [])

    def test_stale_unfilled_entries_are_cancelled(self):
        broker = FakeBroker(open_orders=[
            OpenOrder("old", "MSFT", "buy", 5, NOW - timedelta(days=4)),
            OpenOrder("new", "GOOGL", "buy", 5, NOW - timedelta(days=1)),
            OpenOrder("leg", "AMZN", "sell", 5, NOW - timedelta(days=10)),  # bracket leg, never cancelled here
        ])
        summary = _service(broker=broker).run([])
        self.assertEqual(summary["cancelled_stale"], 1)
        self.assertEqual(broker.cancelled, ["old"])

    def test_cancelled_stale_entry_does_not_block_fresh_signal_same_run(self):
        # Regression: the stale MSFT entry (and its bracket legs) are cancelled
        # this run, so a fresh buy signal for MSFT must be executed, not
        # skipped as "open order already pending".
        broker = FakeBroker(open_orders=[
            OpenOrder("old", "MSFT", "buy", 5, NOW - timedelta(days=4)),
            OpenOrder("old-leg", "MSFT", "sell", 5, NOW - timedelta(days=4)),
            OpenOrder("live", "GOOGL", "buy", 5, NOW - timedelta(days=1)),
        ])
        summary = _service(broker=broker, signals={
            "MSFT": _signal("MSFT"), "GOOGL": _signal("GOOGL"),
        }).run(["MSFT", "GOOGL"])
        by_symbol = {d["symbol"]: d for d in summary["decisions"]}
        self.assertEqual(broker.cancelled, ["old"])
        self.assertEqual(by_symbol["MSFT"]["status"], "submitted")
        self.assertEqual(by_symbol["GOOGL"]["status"], "skipped")
        self.assertIn("pending", by_symbol["GOOGL"]["reason"])


class ChaseTestCase(unittest.TestCase):
    def test_price_above_entry_chases_with_marketable_limit(self):
        # planned entry 311.5, market at 312 -> limit = 312 * 1.003 = 312.94,
        # R:R = (333.96 - 312.936) / (312.936 - 299.91) ≈ 1.61, still >= 1.5
        broker = FakeBroker(prices={"AAPL": 312.0})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "submitted")
        self.assertAlmostEqual(d["limit_price"], round(312.0 * 1.003, 2))
        self.assertEqual(d["stop_price"], 299.91)
        self.assertEqual(d["target_price"], 333.96)
        # sizing uses the chased entry: risk/share = 312.936 - 299.91
        self.assertLessEqual(d["risk_usd"], 500)
        self.assertIn("chased", d["reason"])

    def test_chase_accepts_rr_between_chase_floor_and_plan_floor(self):
        # plan R:R at entry 311.5 is 1.94 (passes the 1.5 plan gate); market at
        # 313.2 -> chased 314.14, R:R ≈ 1.39: below 1.5 but above the 1.3
        # chase floor -> still submitted.
        broker = FakeBroker(prices={"AAPL": 313.2})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "submitted")
        self.assertAlmostEqual(d["limit_price"], round(313.2 * 1.003, 2))
        self.assertTrue(1.3 <= d["r_multiple"] < 1.5)

    def test_chase_skipped_when_rr_degrades_below_min(self):
        # market ran to 325: R:R = (333.96-325.975)/(325.975-299.91) ≈ 0.31
        broker = FakeBroker(prices={"AAPL": 325.0})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "skipped")
        self.assertIn("ran past entry", d["reason"])
        self.assertEqual(broker.submitted, [])

    def test_price_at_or_below_entry_keeps_planned_limit(self):
        broker = FakeBroker(prices={"AAPL": 305.0})
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "submitted")
        self.assertEqual(d["limit_price"], 311.5)

    def test_missing_quote_falls_back_to_planned_limit(self):
        broker = FakeBroker()  # latest_price returns None
        d = _service(broker=broker, signals={"AAPL": _signal("AAPL")}).run(["AAPL"])["decisions"][0]
        self.assertEqual(d["status"], "submitted")
        self.assertEqual(d["limit_price"], 311.5)


class PersistenceAndFormatTestCase(unittest.TestCase):
    def test_every_decision_is_persisted(self):
        db = FakeDB()
        _service(db=db, signals={"AAPL": _signal("AAPL"), "NVDA": _signal("NVDA", action="watch")}).run(["AAPL", "NVDA"])
        self.assertEqual(sorted(r.symbol for r in db.rows), ["AAPL", "NVDA"])
        self.assertEqual({r.status for r in db.rows}, {"submitted", "skipped"})

    def test_format_summary(self):
        summary = _service(signals={"AAPL": _signal("AAPL"), "NVDA": _signal("NVDA", action="watch")}).run(["AAPL", "NVDA"])
        text = format_summary(summary)
        self.assertIn("Paper Orders", text)
        self.assertIn("BUY AAPL x19", text)
        self.assertIn("Skipped: NVDA", text)
        self.assertNotIn("DRY RUN", text)

    def test_settings_from_env(self):
        env = {"PAPER_TRADING_ENABLED": "true", "PAPER_TRADING_ACCOUNT": "b", "PAPER_TRADING_RISK_PER_TRADE_USD": "500", "PAPER_TRADING_MAX_POSITION_PCT": "25", "PAPER_TRADING_CHASE_PCT": "0.5"}
        s = PaperTradingSettings.from_env(env)
        self.assertTrue(s.enabled); self.assertEqual(s.account, "B"); self.assertEqual(s.max_position_pct, 25.0)
        self.assertEqual(s.chase_pct, 0.5)


if __name__ == "__main__":
    unittest.main()
