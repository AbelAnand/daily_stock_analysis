# -*- coding: utf-8 -*-
"""财报日历服务（EarningsCalendarService）单元测试。全部 mock yfinance，不触网。"""

import json
import sys
import time
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.services.earnings_calendar_service import (
    EarningsCalendarService,
    get_earnings_calendar_service,
)


def _install_fake_yfinance(calendar=None, earnings_index=None, ticker_raises=False):
    """构造并注入一个假的 yfinance 模块，返回该模块对象。"""

    class _FakeEarningsDF:
        def __init__(self, index):
            self.index = list(index or [])

    class FakeTicker:
        call_count = 0

        def __init__(self, symbol):
            if ticker_raises:
                raise RuntimeError("network down")
            type(self).call_count += 1
            self.symbol = symbol
            self.calendar = calendar

        def get_earnings_dates(self, limit=8):
            if earnings_index is None:
                raise RuntimeError("no earnings dates")
            return _FakeEarningsDF(earnings_index)

    fake = types.ModuleType("yfinance")
    fake.Ticker = FakeTicker
    return fake


class EarningsCalendarServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "earnings_cache.json"
        self.today = date(2026, 8, 20)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _service(self, **kwargs) -> EarningsCalendarService:
        return EarningsCalendarService(cache_path=self.cache_path, **kwargs)

    def test_non_us_code_returns_none_without_network(self) -> None:
        fake = _install_fake_yfinance(ticker_raises=True)
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            self.assertIsNone(service.get_next_earnings_date("600519", today=self.today))
            self.assertIsNone(service.get_earnings_context("00700", today=self.today))
        # 非美股不应触发 Ticker 构造
        self.assertEqual(fake.Ticker.call_count, 0)

    def test_us_index_symbol_skipped(self) -> None:
        fake = _install_fake_yfinance(ticker_raises=True)
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            self.assertIsNone(service.get_next_earnings_date("^GSPC", today=self.today))

    def test_calendar_dict_path_returns_next_future_date(self) -> None:
        future = self.today + timedelta(days=5)
        fake = _install_fake_yfinance(calendar={"Earnings Date": [future]})
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            self.assertEqual(service.get_next_earnings_date("AAPL", today=self.today), future)
            context = service.get_earnings_context("AAPL", today=self.today)
        self.assertEqual(context["next_earnings_date"], future.isoformat())
        self.assertEqual(context["days_until"], 5)

    def test_earnings_dates_fallback_when_calendar_has_only_past(self) -> None:
        past = self.today - timedelta(days=30)
        future = self.today + timedelta(days=12)
        fake = _install_fake_yfinance(
            calendar={"Earnings Date": [past]},
            earnings_index=[past, future],
        )
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            self.assertEqual(service.get_next_earnings_date("MSFT", today=self.today), future)

    def test_network_failure_returns_none_and_never_raises(self) -> None:
        fake = _install_fake_yfinance(ticker_raises=True)
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            self.assertIsNone(service.get_next_earnings_date("AAPL", today=self.today))
            self.assertIsNone(service.get_earnings_context("AAPL", today=self.today))

    def test_memory_cache_prevents_second_fetch(self) -> None:
        future = self.today + timedelta(days=3)
        fake = _install_fake_yfinance(calendar={"Earnings Date": [future]})
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            service.get_next_earnings_date("NVDA", today=self.today)
            service.get_next_earnings_date("NVDA", today=self.today)
        self.assertEqual(fake.Ticker.call_count, 1)

    def test_negative_result_is_cached_too(self) -> None:
        fake = _install_fake_yfinance(calendar=None, earnings_index=None)
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service()
            self.assertIsNone(service.get_next_earnings_date("TSLA", today=self.today))
            self.assertIsNone(service.get_next_earnings_date("TSLA", today=self.today))
        self.assertEqual(fake.Ticker.call_count, 1)

    def test_disk_cache_shared_across_instances(self) -> None:
        future = self.today + timedelta(days=9)
        fake = _install_fake_yfinance(calendar={"Earnings Date": [future]})
        with patch.dict(sys.modules, {"yfinance": fake}):
            self._service().get_next_earnings_date("AAPL", today=self.today)
            # 新实例应命中磁盘缓存，不再触网
            second = self._service()
            self.assertEqual(second.get_next_earnings_date("AAPL", today=self.today), future)
        self.assertEqual(fake.Ticker.call_count, 1)
        data = json.loads(self.cache_path.read_text(encoding="utf-8"))
        self.assertEqual(data["AAPL"]["next_earnings_date"], future.isoformat())

    def test_expired_cache_triggers_refetch(self) -> None:
        future = self.today + timedelta(days=9)
        fake = _install_fake_yfinance(calendar={"Earnings Date": [future]})
        with patch.dict(sys.modules, {"yfinance": fake}):
            service = self._service(ttl_seconds=3600)
            service.get_next_earnings_date("AAPL", today=self.today)
            # 手动把缓存条目改为过期
            with service._lock:
                service._memory_cache["AAPL"]["fetched_at"] = time.time() - 7200
            service.get_next_earnings_date("AAPL", today=self.today)
        self.assertEqual(fake.Ticker.call_count, 2)

    def test_singleton_returns_same_instance(self) -> None:
        self.assertIs(get_earnings_calendar_service(), get_earnings_calendar_service())


if __name__ == "__main__":
    unittest.main()
