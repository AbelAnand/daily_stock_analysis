# -*- coding: utf-8 -*-
"""
财报日历服务（目前仅美股）。

通过 yfinance 获取下一次财报日期，用于：
- 在分析 prompt 中注入 "下次财报: YYYY-MM-DD (N天后)"
- 当买入/加仓建议的持有周期覆盖财报日（7 天内）时做标注（annotate-not-veto）

设计约束：
- 只做标注，不否决模型结论
- 内存 + 磁盘双层缓存，TTL 约 1 天，避免每次分析都触网
- 绝不抛异常：网络失败/解析失败一律返回 None
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.trading_calendar import get_market_for_stock
from data_provider.base import normalize_stock_code

logger = logging.getLogger(__name__)

# 缓存 TTL：约 1 天
EARNINGS_CACHE_TTL_SECONDS = 24 * 3600

_CACHE_FILENAME = "earnings_calendar_cache.json"


def _get_data_dir() -> Path:
    """与 auth.py 同口径：DATA_DIR 取 DATABASE_PATH 的父目录。"""
    db_path = os.getenv("DATABASE_PATH", "./data/stock_analysis.db")
    return Path(db_path).resolve().parent


class EarningsCalendarService:
    """美股下次财报日期查询（带内存 + 磁盘缓存，永不抛异常）。"""

    def __init__(self, cache_path: Optional[Path] = None, ttl_seconds: int = EARNINGS_CACHE_TTL_SECONDS):
        self._ttl_seconds = ttl_seconds
        self._cache_path = cache_path
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._disk_loaded = False

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_next_earnings_date(self, code: str, today: Optional[date] = None) -> Optional[date]:
        """
        返回下一次财报日期（仅美股；其他市场返回 None）。

        网络失败、非美股、无数据一律返回 None，绝不抛异常。
        """
        try:
            symbol = self._us_symbol(code)
            if not symbol:
                return None
            today = today or date.today()

            cached = self._get_cached(symbol)
            if cached is not None:
                return self._parse_iso_date(cached.get("next_earnings_date"))

            next_date = self._fetch_next_earnings_date(symbol, today)
            self._put_cache(symbol, next_date)
            return next_date
        except Exception as exc:  # 防御性兜底：本服务承诺不抛异常
            logger.debug("EarningsCalendarService.get_next_earnings_date(%s) failed: %s", code, exc)
            return None

    def get_earnings_context(self, code: str, today: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """
        返回用于 prompt/标注的紧凑上下文：
        {"next_earnings_date": "YYYY-MM-DD", "days_until": N}
        无数据返回 None。
        """
        try:
            today = today or date.today()
            next_date = self.get_next_earnings_date(code, today=today)
            if next_date is None:
                return None
            return {
                "next_earnings_date": next_date.isoformat(),
                "days_until": (next_date - today).days,
            }
        except Exception as exc:
            logger.debug("EarningsCalendarService.get_earnings_context(%s) failed: %s", code, exc)
            return None

    # ------------------------------------------------------------------
    # 市场识别 / 抓取
    # ------------------------------------------------------------------

    @staticmethod
    def _us_symbol(code: str) -> Optional[str]:
        """仅当代码识别为美股时返回 yfinance symbol（美股即代码本身）。"""
        raw = str(code or "").strip()
        if not raw:
            return None
        if get_market_for_stock(normalize_stock_code(raw)) != "us":
            return None
        symbol = raw.upper()
        # 指数（^GSPC 等）无财报概念
        if symbol.startswith("^"):
            return None
        return symbol

    def _fetch_next_earnings_date(self, symbol: str, today: date) -> Optional[date]:
        """通过 yfinance 抓取下一次财报日期。失败返回 None。"""
        try:
            import yfinance as yf
        except Exception as exc:
            logger.debug("yfinance unavailable for earnings calendar: %s", exc)
            return None

        try:
            ticker = yf.Ticker(symbol)
        except Exception as exc:
            logger.debug("yf.Ticker(%s) failed: %s", symbol, exc)
            return None

        candidates: list[date] = []

        # 路径 1：Ticker.calendar（新版返回 dict，含 "Earnings Date" 列表）
        try:
            calendar = ticker.calendar
            candidates.extend(self._dates_from_calendar(calendar))
        except Exception as exc:
            logger.debug("ticker.calendar failed for %s: %s", symbol, exc)

        # 路径 2：get_earnings_dates（DataFrame，索引为时间戳，含未来日期）
        if not any(d >= today for d in candidates):
            try:
                df = ticker.get_earnings_dates(limit=8)
                candidates.extend(self._dates_from_earnings_df(df))
            except Exception as exc:
                logger.debug("ticker.get_earnings_dates failed for %s: %s", symbol, exc)

        future = sorted(d for d in candidates if d >= today)
        return future[0] if future else None

    @classmethod
    def _dates_from_calendar(cls, calendar: Any) -> list[date]:
        """兼容 dict 与 DataFrame 两种 Ticker.calendar 返回形态。"""
        results: list[date] = []
        if calendar is None:
            return results
        try:
            if isinstance(calendar, dict):
                values = calendar.get("Earnings Date") or []
                if not isinstance(values, (list, tuple)):
                    values = [values]
                for value in values:
                    parsed = cls._coerce_date(value)
                    if parsed is not None:
                        results.append(parsed)
            elif hasattr(calendar, "loc") and hasattr(calendar, "index"):
                # 旧版 DataFrame：行 "Earnings Date"，列为若干候选日期
                if "Earnings Date" in getattr(calendar, "index", []):
                    for value in calendar.loc["Earnings Date"]:
                        parsed = cls._coerce_date(value)
                        if parsed is not None:
                            results.append(parsed)
        except Exception:
            pass
        return results

    @classmethod
    def _dates_from_earnings_df(cls, df: Any) -> list[date]:
        results: list[date] = []
        if df is None:
            return results
        try:
            for value in getattr(df, "index", []):
                parsed = cls._coerce_date(value)
                if parsed is not None:
                    results.append(parsed)
        except Exception:
            pass
        return results

    @staticmethod
    def _coerce_date(value: Any) -> Optional[date]:
        if value is None:
            return None
        try:
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            # pandas.Timestamp 等
            if hasattr(value, "to_pydatetime"):
                return value.to_pydatetime().date()
            text = str(value).strip()
            if not text:
                return None
            return datetime.fromisoformat(text[:10]).date()
        except Exception:
            return None

    @staticmethod
    def _parse_iso_date(value: Any) -> Optional[date]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)[:10]).date()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 缓存（内存 + 磁盘，TTL 约 1 天；None 结果同样缓存以避免反复触网）
    # ------------------------------------------------------------------

    def _resolve_cache_path(self) -> Path:
        if self._cache_path is not None:
            return self._cache_path
        return _get_data_dir() / _CACHE_FILENAME

    def _load_disk_cache_locked(self) -> None:
        if self._disk_loaded:
            return
        self._disk_loaded = True
        try:
            path = self._resolve_cache_path()
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for symbol, entry in data.items():
                    if isinstance(entry, dict) and "fetched_at" in entry:
                        # 内存中已有的更新条目优先
                        self._memory_cache.setdefault(str(symbol), entry)
        except Exception as exc:
            logger.debug("Earnings calendar disk cache load failed: %s", exc)

    def _save_disk_cache_locked(self) -> None:
        try:
            path = self._resolve_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._memory_cache, ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("Earnings calendar disk cache save failed: %s", exc)

    def _get_cached(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._load_disk_cache_locked()
            entry = self._memory_cache.get(symbol)
            if not isinstance(entry, dict):
                return None
            try:
                fetched_at = float(entry.get("fetched_at") or 0)
            except (TypeError, ValueError):
                return None
            if time.time() - fetched_at > self._ttl_seconds:
                return None
            return entry

    def _put_cache(self, symbol: str, next_date: Optional[date]) -> None:
        with self._lock:
            self._load_disk_cache_locked()
            self._memory_cache[symbol] = {
                "fetched_at": time.time(),
                "next_earnings_date": next_date.isoformat() if next_date else None,
            }
            self._save_disk_cache_locked()


_service_lock = threading.Lock()
_service_instance: Optional[EarningsCalendarService] = None


def get_earnings_calendar_service() -> EarningsCalendarService:
    """进程级单例（内存缓存跨调用复用）。"""
    global _service_instance
    with _service_lock:
        if _service_instance is None:
            _service_instance = EarningsCalendarService()
        return _service_instance
