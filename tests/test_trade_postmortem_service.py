# -*- coding: utf-8 -*-
"""Trade post-mortem tests: round-trip reconstruction, review parsing, lesson gating (no network)."""

import json
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from src.services.trade_postmortem_service import (
    PostmortemSettings,
    TradePostmortemService,
    build_lessons_context,
    format_postmortem_summary,
)

ENTRY_AT = datetime(2026, 9, 1, 13, 34, tzinfo=timezone.utc)


class _Rec:
    """paper_trades entry row."""
    def __init__(self, symbol="NVDA", side="buy", order_id="ord-1", qty=27,
                 limit_price=221.46, stop_price=216.7, target_price=230.47, r_multiple=2.0,
                 signal_id=None, created_at=ENTRY_AT):
        self.symbol, self.side, self.order_id, self.qty = symbol, side, order_id, qty
        self.limit_price, self.stop_price, self.target_price = limit_price, stop_price, target_price
        self.r_multiple, self.signal_id, self.created_at = r_multiple, signal_id, created_at


class FakeBroker:
    def __init__(self, orders=None, positions=None, flat_fill=None):
        self.orders = orders or {}
        self._positions = positions or {}
        self.flat_fill = flat_fill

    def order_with_legs(self, order_id):
        return self.orders.get(order_id)

    def positions(self):
        return dict(self._positions)

    def first_flattening_fill(self, symbol, *, side, after, qty):
        return self.flat_fill

    def daily_bars(self, symbol, *, start, limit=10):
        return [{"date": "2026-09-01", "open": 216.0, "high": 218.0, "low": 210.0, "close": 211.0}]


class FakeDB:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.rows = []
        self.lessons = []

    def list_unreviewed_paper_entries(self, days=30):
        return list(self.entries)

    def was_discovery_symbol(self, symbol, around=None, days=7):
        return symbol in getattr(self, "discovery_symbols", set())

    def get_postmortem_bucket_stats(self):
        return [{"source": "watchlist", "direction": "long", "trades": len(self.rows),
                 "wins": 0, "net_pnl_usd": -45.0, "avg_r": -1.0}]

    def get_recent_trade_lessons(self, limit=5):
        return list(self.lessons)[:limit]

    @contextmanager
    def session_scope(self):
        yield self

    def add(self, row):
        self.rows.append(row)


def _filled_order(order_id="ord-1", side="buy", price=216.08, qty=27, at=ENTRY_AT,
                  stop_fill=None, target_fill=None):
    legs = []
    if stop_fill is not None:
        legs.append({"id": "leg-stop", "status": "filled", "side": "sell", "stop_price": 216.7,
                     "limit_price": None, "filled_avg_price": stop_fill, "filled_qty": qty,
                     "filled_at": at + timedelta(minutes=1)})
    else:
        legs.append({"id": "leg-stop", "status": "held", "side": "sell", "stop_price": 216.7,
                     "limit_price": None, "filled_avg_price": None, "filled_qty": 0, "filled_at": None})
    if target_fill is not None:
        legs.append({"id": "leg-tp", "status": "filled", "side": "sell", "stop_price": None,
                     "limit_price": 230.47, "filled_avg_price": target_fill, "filled_qty": qty,
                     "filled_at": at + timedelta(hours=5)})
    else:
        legs.append({"id": "leg-tp", "status": "new", "side": "sell", "stop_price": None,
                     "limit_price": 230.47, "filled_avg_price": None, "filled_qty": 0, "filled_at": None})
    return {"id": order_id, "status": "filled", "side": side, "filled_qty": qty,
            "filled_avg_price": price, "filled_at": at, "stop_price": None, "limit_price": 221.46,
            "legs": legs}


REVIEW = json.dumps({"outcome": "loss", "process_quality": "flawed", "category": "bad_entry",
                     "lesson": "Do not chase entries priced off pre-market quotes.",
                     "lesson_confidence": "high", "review": "Entry filled below its own stop."})


def _svc(broker, db, review_raw=REVIEW):
    settings = PostmortemSettings(enabled=True)
    return TradePostmortemService(settings, broker=broker, db=db, review_fn=lambda prompt: review_raw)


class RoundTripTestCase(unittest.TestCase):
    def test_stop_exit_reviewed_and_persisted(self):
        db = FakeDB(entries=[_Rec()])
        broker = FakeBroker(orders={"ord-1": _filled_order(stop_fill=216.09)})
        summary = _svc(broker, db).run()
        self.assertEqual(len(summary["reviewed"]), 1)
        r = summary["reviewed"][0]
        self.assertEqual((r["exit_kind"], r["category"]), ("stop", "bad_entry"))
        self.assertAlmostEqual(r["pnl_usd"], (216.09 - 216.08) * 27, places=2)
        self.assertTrue(r["prompt_inject"])
        self.assertEqual(len(db.rows), 1)

    def test_target_exit_win(self):
        db = FakeDB(entries=[_Rec()])
        broker = FakeBroker(orders={"ord-1": _filled_order(target_fill=230.5)})
        win_review = json.dumps({"outcome": "win", "process_quality": "good", "category": "good_process",
                                 "lesson": None, "lesson_confidence": None, "review": "worked"})
        summary = _svc(broker, db, review_raw=win_review).run()
        r = summary["reviewed"][0]
        self.assertEqual(r["exit_kind"], "target")
        self.assertGreater(r["pnl_usd"], 0)
        self.assertFalse(r["prompt_inject"])  # no lesson -> nothing injected

    def test_open_position_skipped(self):
        from src.services.paper_trading_service import Position

        db = FakeDB(entries=[_Rec()])
        broker = FakeBroker(orders={"ord-1": _filled_order()}, positions={"NVDA": Position("NVDA", 27, 216.08)})
        summary = _svc(broker, db).run()
        self.assertEqual(summary["reviewed"], [])
        self.assertEqual(db.rows, [])  # nothing persisted; retried next run

    def test_direct_close_found_via_flattening_fill(self):
        db = FakeDB(entries=[_Rec()])
        broker = FakeBroker(orders={"ord-1": _filled_order()},
                            flat_fill={"price": 220.0, "at": ENTRY_AT + timedelta(hours=3), "qty": 27})
        summary = _svc(broker, db).run()
        r = summary["reviewed"][0]
        self.assertEqual(r["exit_kind"], "close")
        self.assertAlmostEqual(r["pnl_usd"], (220.0 - 216.08) * 27, places=2)

    def test_short_round_trip_pnl_sign(self):
        rec = _Rec(symbol="XYZ", side="sell_short", limit_price=100.0, stop_price=105.0, target_price=88.0)
        order = _filled_order(side="sell", price=100.0, qty=27, target_fill=None, stop_fill=None)
        order["legs"] = [{"id": "leg-tp", "status": "filled", "side": "buy", "stop_price": None,
                          "limit_price": 88.0, "filled_avg_price": 88.1, "filled_qty": 27,
                          "filled_at": ENTRY_AT + timedelta(days=2)}]
        db = FakeDB(entries=[rec])
        broker = FakeBroker(orders={"ord-1": order})
        win_review = json.dumps({"outcome": "win", "process_quality": "good", "category": "good_process",
                                 "lesson": None, "lesson_confidence": None, "review": "ok"})
        summary = _svc(broker, db, review_raw=win_review).run()
        r = summary["reviewed"][0]
        self.assertEqual(r["direction"], "short")
        self.assertAlmostEqual(r["pnl_usd"], (100.0 - 88.1) * 27, places=2)  # short profit

    def test_cancelled_entry_recorded_without_llm(self):
        db = FakeDB(entries=[_Rec()])
        broker = FakeBroker(orders={"ord-1": {"id": "ord-1", "status": "canceled", "side": "buy",
                                              "filled_qty": 0, "filled_avg_price": None, "filled_at": None,
                                              "stop_price": None, "limit_price": 221.46, "legs": []}})
        called = []
        svc = TradePostmortemService(PostmortemSettings(enabled=True), broker=broker, db=db,
                                     review_fn=lambda prompt: called.append(1) or REVIEW)
        summary = svc.run()
        self.assertEqual(called, [])  # no LLM for unfilled entries
        self.assertEqual(db.rows[0].outcome, "no_fill")
        self.assertNotIn("no_fill", [r["outcome"] for r in summary["reviewed"] if r["outcome"] != "no_fill"])

    def test_execution_flaw_lesson_not_injected(self):
        db = FakeDB(entries=[_Rec()])
        broker = FakeBroker(orders={"ord-1": _filled_order(stop_fill=216.09)})
        flaw = json.dumps({"outcome": "scratch", "process_quality": "flawed", "category": "execution_flaw",
                           "lesson": "Fix order submission.", "lesson_confidence": "high", "review": "system bug"})
        summary = _svc(broker, db, review_raw=flaw).run()
        r = summary["reviewed"][0]
        self.assertEqual(r["category"], "execution_flaw")
        self.assertFalse(r["prompt_inject"])  # escalated to engineers, not fed to the analyst

    def test_discovery_source_tagged(self):
        db = FakeDB(entries=[_Rec()])
        db.discovery_symbols = {"NVDA"}
        broker = FakeBroker(orders={"ord-1": _filled_order(stop_fill=216.09)})
        summary = _svc(broker, db).run()
        self.assertEqual(summary["reviewed"][0]["source"], "discovery")


class ReviewParseTestCase(unittest.TestCase):
    def test_bad_category_and_prose_tolerated(self):
        svc = _svc(FakeBroker(), FakeDB())
        out = svc._parse_review('Sure! {"outcome": "loss", "category": "bad_vibes", "lesson": "  ", "process_quality": "flawed"} done')
        self.assertEqual(out["outcome"], "loss")
        self.assertIsNone(out["category"])
        self.assertIsNone(out["lesson"])

    def test_garbage_returns_empty(self):
        svc = _svc(FakeBroker(), FakeDB())
        self.assertIsNone(svc._parse_review("no json here")["outcome"])


class LessonsAndFormatTestCase(unittest.TestCase):
    def test_lessons_context_shape(self):
        class _L:
            category, lesson, symbol = "bad_entry", "Don't chase pre-market quotes.", "NVDA"
            direction, r_realized, outcome = "long", 0.0, "loss"
            created_at = ENTRY_AT
        db = FakeDB()
        db.lessons = [_L()]
        ctx = build_lessons_context(db=db, limit=5)
        self.assertEqual(len(ctx["lessons"]), 1)
        self.assertEqual(ctx["lessons"][0]["category"], "bad_entry")

    def test_lessons_context_none_when_empty_or_disabled(self):
        self.assertIsNone(build_lessons_context(db=FakeDB(), limit=5))
        self.assertIsNone(build_lessons_context(db=FakeDB(), limit=0))

    def test_format_includes_lesson_scorecard_and_flags_execution_flaws(self):
        summary = {
            "enabled": True,
            "reviewed": [{"symbol": "NVDA", "direction": "long", "pnl_usd": -45.0, "r_realized": -1.0,
                          "exit_kind": "stop", "category": "execution_flaw", "outcome": "loss",
                          "lesson": "Fix the thing.", "prompt_inject": True, "source": "watchlist"}],
            "scorecard": [{"source": "watchlist", "direction": "long", "trades": 3, "wins": 1,
                           "net_pnl_usd": -45.0, "avg_r": -0.3}],
        }
        text = format_postmortem_summary(summary)
        self.assertIn("🧠", text)
        self.assertIn("📚 Fix the thing.", text)
        self.assertIn("🔧", text)
        self.assertIn("watchlist/long: 1/3 wins", text)

    def test_format_quiet_when_nothing_new(self):
        self.assertEqual(format_postmortem_summary({"enabled": True, "reviewed": [], "scorecard": []}), "")


if __name__ == "__main__":
    unittest.main()
