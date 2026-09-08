# -*- coding: utf-8 -*-
"""Paper-trading dashboard API tests (fake dashboard service via dependency override)."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import get_paper_dashboard_service
from src.services.paper_dashboard_service import PaperTradingUnavailableError, PositionNotFoundError


def _snapshot():
    return {
        "generated_at": "2026-09-04T14:00:00+00:00",
        "status": {
            "enabled": True, "dry_run": False, "kill_switch_active": False, "allow_short": True,
            "market_open": True, "next_open": None, "next_close": "2026-09-04T20:00:00+00:00", "max_positions": 10,
        },
        "account": {
            "label": "B", "account_number": "PA_TEST", "equity": 31000.0, "last_equity": 30500.0, "cash": 20000.0,
            "buying_power": 80000.0, "portfolio_value": 31000.0, "long_market_value": 11000.0,
            "short_market_value": 0.0, "created_at": None,
        },
        "pnl": {
            "total_pnl": 1000.0, "total_pnl_pct": 3.33, "total_pnl_basis": "account_equity_since_first_trade",
            "since": "2026-08-22", "baseline_equity": 30000.0, "day_pnl": 500.0, "day_pnl_pct": 1.64,
            "unrealized_pnl": 108.63, "realized_pnl": -44.22, "closed_trades": 2, "wins": 0, "losses": 1,
        },
        "exposure": {"open_positions": 1, "gross_exposure": 5950.0, "gross_exposure_pct": 19.19, "long_market_value": 5950.0, "short_market_value": 0.0},
        "positions": [{
            "symbol": "GOOGL", "side": "long", "qty": 17, "avg_entry": 343.61, "current_price": 350.0,
            "market_value": 5950.0, "cost_basis": 5841.37, "unrealized_pl": 108.63, "unrealized_plpc": 0.0186,
            "unrealized_intraday_pl": 19.55, "change_today": 0.0034, "stop_price": 340.0, "target_price": 380.0,
            "initial_stop": 330.0, "r_multiple": 0.47, "protected": True,
        }],
        "open_orders": [],
        "recent_trades": [],
    }


class FakeDashboardService:
    def __init__(self, *, unavailable=False):
        self.unavailable = unavailable
        self.closed = []
        self.close_all_calls = 0

    def snapshot(self):
        if self.unavailable:
            raise PaperTradingUnavailableError("No reachable Alpaca paper account")
        return _snapshot()

    def close_position(self, symbol):
        if symbol.upper() != "GOOGL":
            raise PositionNotFoundError(f"{symbol.upper()} is not an open position")
        self.closed.append(symbol.upper())
        return {"symbol": "GOOGL", "side": "sell", "qty": 17, "status": "submitted", "order_id": "close-1", "message": "close order submitted"}

    def close_all_positions(self):
        self.close_all_calls += 1
        return {"requested": 1, "closed": 1, "failed": 0, "results": [
            {"symbol": "GOOGL", "side": "sell", "qty": 17, "status": "submitted", "order_id": "close-1", "message": "close order submitted"},
        ]}


class PaperTradingApiTestCase(unittest.TestCase):
    def _client(self, service):
        self._tmp = tempfile.TemporaryDirectory()
        app = create_app(static_dir=Path(self._tmp.name))
        app.dependency_overrides[get_paper_dashboard_service] = lambda: service
        return TestClient(app)

    def tearDown(self):
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    def test_dashboard_payload(self):
        client = self._client(FakeDashboardService())
        resp = client.get("/api/v1/paper-trading/dashboard")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["pnl"]["total_pnl"], 1000.0)
        self.assertEqual(body["positions"][0]["symbol"], "GOOGL")
        self.assertTrue(body["positions"][0]["protected"])
        self.assertEqual(body["exposure"]["open_positions"], 1)
        self.assertTrue(body["status"]["market_open"])

    def test_dashboard_unavailable_is_503(self):
        client = self._client(FakeDashboardService(unavailable=True))
        resp = client.get("/api/v1/paper-trading/dashboard")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"], "paper_trading_unavailable")

    def test_close_position(self):
        service = FakeDashboardService()
        client = self._client(service)
        resp = client.post("/api/v1/paper-trading/positions/googl/close")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "submitted")
        self.assertEqual(service.closed, ["GOOGL"])

    def test_close_unknown_position_is_404(self):
        client = self._client(FakeDashboardService())
        resp = client.post("/api/v1/paper-trading/positions/TSLA/close")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"], "position_not_found")

    def test_close_all(self):
        service = FakeDashboardService()
        client = self._client(service)
        resp = client.post("/api/v1/paper-trading/positions/close-all")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["closed"], 1)
        self.assertEqual(service.close_all_calls, 1)


if __name__ == "__main__":
    unittest.main()
