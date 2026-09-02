# -*- coding: utf-8 -*-
"""News discovery tests: triage parsing and shortlist filtering (no network)."""

import json
import unittest

from src.services.news_discovery_service import Candidate, DiscoverySettings, NewsDiscoveryService


def _svc(triage_raw="[]", market_data=None, tradable=True, **settings_kw):
    settings = DiscoverySettings(enabled=True, **settings_kw)
    md = market_data if market_data is not None else {
        "news": [{"headline": "ACME beats earnings", "symbols": ["ACME"], "at": "2026-09-02 08:00"}],
        "gainers": [{"symbol": "ACME", "pct": 12.0}], "losers": [], "most_actives": [],
    }
    svc = NewsDiscoveryService(settings, db=_FakeDB(), data_clients=lambda: md,
                               triage_fn=lambda prompt: triage_raw)
    svc._tradable = lambda symbol: tradable
    return svc


class _FakeDB:
    def __init__(self):
        self.rows = []

    def session_scope(self):
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            yield self
        return _scope()

    def add(self, row):
        self.rows.append(row)


class TriageParseTestCase(unittest.TestCase):
    def test_parses_json_array_with_prose_around_it(self):
        raw = 'Here you go:\n[{"symbol": "acme", "direction": "long", "conviction": 4, "catalyst": "beat"}]\nDone.'
        out = _svc()._parse_triage(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].symbol, out[0].direction, out[0].conviction), ("ACME", "long", 4))

    def test_rejects_garbage_and_bad_symbols(self):
        raw = json.dumps([
            {"symbol": "bad symbol!", "direction": "long", "conviction": 5},
            {"symbol": "OK", "direction": "sideways", "conviction": 9},
            "not-a-dict",
        ])
        out = _svc()._parse_triage(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].symbol, "OK")
        self.assertEqual(out[0].direction, "long")   # unknown direction coerced
        self.assertEqual(out[0].conviction, 5)       # clamped to 1-5

    def test_no_json_returns_empty(self):
        self.assertEqual(_svc()._parse_triage("nothing tradeable today"), [])


class ShortlistTestCase(unittest.TestCase):
    def _raw(self, *items):
        return json.dumps(list(items))

    def test_conviction_floor_and_cap(self):
        raw = self._raw(
            {"symbol": "AAA", "direction": "long", "conviction": 5, "catalyst": "x"},
            {"symbol": "BBB", "direction": "short", "conviction": 4, "catalyst": "y"},
            {"symbol": "CCC", "direction": "long", "conviction": 2, "catalyst": "weak"},
        )
        svc = _svc(triage_raw=raw, max_candidates=2)
        shortlist = svc.discover()
        self.assertEqual([c.symbol for c in shortlist], ["AAA", "BBB"])  # CCC below floor; cap 2

    def test_untradable_dropped(self):
        raw = self._raw({"symbol": "AAA", "direction": "long", "conviction": 5, "catalyst": "x"})
        svc = _svc(triage_raw=raw, tradable=False)
        self.assertEqual(svc.discover(), [])

    def test_empty_market_data_short_circuits(self):
        svc = _svc(market_data={"news": [], "gainers": [], "losers": [], "most_actives": []})
        self.assertEqual(svc.discover(), [])

    def test_discover_never_raises(self):
        svc = _svc()
        svc._fetch_market_data = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(svc.discover(), [])


if __name__ == "__main__":
    unittest.main()
