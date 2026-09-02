# -*- coding: utf-8 -*-
"""News-driven US stock discovery for the daily analysis run (opt-in).

Instead of analyzing only the fixed ``STOCK_LIST``, this service scans the
overnight tape — Alpaca's news feed (Benzinga) plus the market-movers and
most-actives screeners — and asks a cheap triage model which symbols have a
*tradeable catalyst* (long or short). The shortlist is merged into the daily
run's stock list, where each candidate then goes through the exact same full
analysis, signal extraction, R:R gates and paper execution as a watchlist
name. Discovery therefore only nominates; it never trades by itself.

Configuration is env-driven like the paper-trading layer (``DISCOVERY_*``,
see ``.env.example``); reuses the ``ALPACA_<LABEL>_KEY_ID/_SECRET_KEY``
credentials. Every run is persisted to ``news_discovery_runs``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")

TRIAGE_PROMPT = """You are the pre-market triage desk of a systematic US equity strategy.
Below are (1) overnight/pre-market news headlines and (2) yesterday's top market movers and most-active stocks.

Select up to {max_candidates} symbols with a concrete, fresh, tradeable catalyst — earnings surprises, guidance changes, FDA/regulatory decisions, M&A, major contracts, analyst re-ratings, product events. For each, judge the likely direction over the next days.

Rules:
- Only symbols that appear in the data below. No indexes, no crypto, no OTC.
- A catalyst must be specific and dated; general market commentary or stale news is not a catalyst.
- direction "short" only for genuinely negative catalysts (missed earnings, guidance cuts, fraud, downgrades on news), not merely "overvalued".
- Fewer, higher-conviction picks beat a full list. Return an empty list if nothing qualifies.

Respond with ONLY a JSON array, no prose:
[{{"symbol": "XYZ", "direction": "long"|"short", "conviction": 1-5, "catalyst": "one line, cite the headline"}}]

=== NEWS (last {hours}h) ===
{news}

=== MOVERS (yesterday) ===
{movers}

=== MOST ACTIVE ===
{actives}
"""


@dataclass
class DiscoverySettings:
    enabled: bool = False
    max_candidates: int = 6         # symbols merged into the daily run
    news_hours: int = 18            # lookback for the news sweep
    news_limit: int = 50            # max headlines fetched
    min_price: float = 5.0          # ignore sub-$5 names
    min_conviction: int = 3         # triage conviction floor (1-5)
    triage_model: str = "anthropic/claude-haiku-4-5-20251001"
    account: str = "auto"           # Alpaca credential label for data APIs

    @classmethod
    def from_env(cls, env: Optional[Dict[str, Any]] = None) -> "DiscoverySettings":
        env = env if env is not None else os.environ

        def _get(key: str, default: Any, cast) -> Any:
            raw = env.get(key)
            if raw is None or str(raw).strip() == "":
                return default
            try:
                return cast(raw)
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=str(env.get("DISCOVERY_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on"),
            max_candidates=_get("DISCOVERY_MAX_CANDIDATES", 6, int),
            news_hours=_get("DISCOVERY_NEWS_HOURS", 18, int),
            news_limit=_get("DISCOVERY_NEWS_LIMIT", 50, int),
            min_price=_get("DISCOVERY_MIN_PRICE", 5.0, float),
            min_conviction=_get("DISCOVERY_MIN_CONVICTION", 3, int),
            triage_model=str(env.get("DISCOVERY_TRIAGE_MODEL") or cls.triage_model).strip(),
            account=str(env.get("PAPER_TRADING_ACCOUNT") or "auto").strip().upper() or "AUTO",
        )


@dataclass
class Candidate:
    symbol: str
    direction: str = "long"        # long | short (a hint for the log; the analyst decides)
    conviction: int = 0
    catalyst: str = ""
    source: str = ""               # news / gainers / losers / most_actives

    def to_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "direction": self.direction, "conviction": self.conviction,
                "catalyst": self.catalyst, "source": self.source}


class NewsDiscoveryService:
    """Scan news + movers, triage with a cheap LLM, return a tradeable shortlist."""

    def __init__(self, settings: Optional[DiscoverySettings] = None, *,
                 env: Optional[Dict[str, Any]] = None, db: Any = None,
                 data_clients: Any = None, triage_fn: Any = None):
        self.settings = settings or DiscoverySettings.from_env(env)
        self._env = env if env is not None else os.environ
        self._db = db
        self._clients = data_clients   # injectable for tests: object with news/movers/actives/asset methods
        self._triage_fn = triage_fn    # injectable for tests: (prompt) -> str

    # --- Alpaca data ----------------------------------------------------
    def _credentials(self) -> tuple:
        from src.services.paper_trading_service import discover_accounts

        labels = [self.settings.account] if self.settings.account not in ("AUTO", "") else discover_accounts(self._env)
        for label in labels:
            key = self._env.get(f"ALPACA_{label}_KEY_ID")
            secret = self._env.get(f"ALPACA_{label}_SECRET_KEY")
            if key and secret:
                return key, secret
        raise RuntimeError("Discovery: no Alpaca credentials (ALPACA_<X>_KEY_ID / _SECRET_KEY)")

    def _fetch_market_data(self) -> Dict[str, Any]:
        if self._clients is not None:
            return self._clients()
        key, secret = self._credentials()
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MarketMoversRequest, MostActivesRequest, NewsRequest

        out: Dict[str, Any] = {"news": [], "gainers": [], "losers": [], "most_actives": []}
        try:
            news = NewsClient(key, secret).get_news(NewsRequest(
                start=datetime.now(timezone.utc) - timedelta(hours=self.settings.news_hours),
                limit=self.settings.news_limit, include_content=False,
            ))
            for item in getattr(news, "news", None) or getattr(news, "data", {}).get("news", []):
                symbols = [s for s in (getattr(item, "symbols", None) or []) if _SYMBOL_RE.match(str(s))]
                if symbols:
                    out["news"].append({"headline": str(getattr(item, "headline", ""))[:200], "symbols": symbols[:6],
                                        "at": str(getattr(item, "created_at", ""))[:16]})
        except Exception as exc:
            logger.warning("Discovery: news fetch failed: %s", exc)
        try:
            screener = ScreenerClient(key, secret)
            movers = screener.get_market_movers(MarketMoversRequest(top=20))
            out["gainers"] = [{"symbol": m.symbol, "pct": round(float(m.percent_change), 1)} for m in movers.gainers]
            out["losers"] = [{"symbol": m.symbol, "pct": round(float(m.percent_change), 1)} for m in movers.losers]
            actives = screener.get_most_actives(MostActivesRequest(top=20))
            out["most_actives"] = [{"symbol": a.symbol} for a in actives.most_actives]
        except Exception as exc:
            logger.warning("Discovery: screener fetch failed: %s", exc)
        return out

    def _tradable(self, symbol: str) -> bool:
        """US equity, active, tradable, and priced above the floor."""
        try:
            key, secret = self._credentials()
            from alpaca.trading.client import TradingClient

            tc = TradingClient(key, secret, paper=True)
            asset = tc.get_asset(symbol)
            if not (asset and asset.tradable and str(getattr(asset.status, "value", asset.status)) == "active"):
                return False
            if str(getattr(asset, "asset_class", "")).lower() not in ("", "us_equity", "assetclass.us_equity"):
                return False
        except Exception as exc:
            logger.info("Discovery: asset check failed for %s: %s", symbol, exc)
            return False
        if self.settings.min_price > 0:
            try:
                from src.services.paper_trading_service import AlpacaPaperBroker

                price = AlpacaPaperBroker(key, secret).latest_price(symbol)
                if price is not None and price < self.settings.min_price:
                    return False
            except Exception:
                pass  # price check is best-effort; the analysis layer sees real data anyway
        return True

    # --- triage -----------------------------------------------------------
    def _triage(self, market_data: Dict[str, Any]) -> List[Candidate]:
        news_lines = "\n".join(
            f"- [{n['at']}] {','.join(n['symbols'])}: {n['headline']}" for n in market_data.get("news", [])
        ) or "(no news)"
        movers_lines = ("gainers: " + ", ".join(f"{m['symbol']} {m['pct']:+}%" for m in market_data.get("gainers", []))
                        + "\nlosers: " + ", ".join(f"{m['symbol']} {m['pct']:+}%" for m in market_data.get("losers", []))) or "(none)"
        actives_lines = ", ".join(a["symbol"] for a in market_data.get("most_actives", [])) or "(none)"
        prompt = TRIAGE_PROMPT.format(max_candidates=self.settings.max_candidates,
                                      hours=self.settings.news_hours,
                                      news=news_lines, movers=movers_lines, actives=actives_lines)
        raw = self._triage_fn(prompt) if self._triage_fn else self._call_llm(prompt)
        return self._parse_triage(raw)

    def _call_llm(self, prompt: str) -> str:
        import litellm

        api_key = (self._env.get("ANTHROPIC_API_KEY") or self._env.get("LLM_ANTHROPIC_API_KEY") or "").strip()
        kwargs: Dict[str, Any] = {
            "model": self.settings.triage_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1500,
            "timeout": 120,
        }
        if api_key:
            kwargs["api_key"] = api_key
        response = litellm.completion(**kwargs)
        return response.choices[0].message.content or ""

    def _parse_triage(self, raw: str) -> List[Candidate]:
        text = str(raw).strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            items = json.loads(match.group(0))
        except json.JSONDecodeError:
            logger.warning("Discovery: triage output was not valid JSON")
            return []
        out: List[Candidate] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            if not _SYMBOL_RE.match(symbol):
                continue
            direction = str(item.get("direction") or "long").strip().lower()
            try:
                conviction = int(item.get("conviction") or 0)
            except (TypeError, ValueError):
                conviction = 0
            out.append(Candidate(symbol=symbol, direction=direction if direction in ("long", "short") else "long",
                                 conviction=max(0, min(5, conviction)),
                                 catalyst=str(item.get("catalyst") or "")[:300], source="triage"))
        return out

    # --- public -----------------------------------------------------------
    def discover(self) -> List[Candidate]:
        """Return the vetted shortlist (may be empty). Never raises."""
        try:
            market_data = self._fetch_market_data()
            n_news = len(market_data.get("news", []))
            if n_news == 0 and not market_data.get("gainers") and not market_data.get("most_actives"):
                self._persist(market_data, [], error="no market data")
                return []
            candidates = self._triage(market_data)
            shortlist: List[Candidate] = []
            for c in sorted(candidates, key=lambda c: -c.conviction):
                if c.conviction < self.settings.min_conviction:
                    continue
                if len(shortlist) >= self.settings.max_candidates:
                    break
                if any(s.symbol == c.symbol for s in shortlist):
                    continue
                if not self._tradable(c.symbol):
                    logger.info("Discovery: %s dropped (not tradable / below $%.0f)", c.symbol, self.settings.min_price)
                    continue
                shortlist.append(c)
            self._persist(market_data, shortlist)
            logger.info("Discovery: %d headlines -> %d candidates -> shortlist [%s]",
                        n_news, len(candidates),
                        ", ".join(f"{c.symbol}({c.direction},c{c.conviction})" for c in shortlist) or "empty")
            return shortlist
        except Exception as exc:
            logger.error("Discovery failed (run continues with the configured list): %s", exc)
            try:
                self._persist({}, [], error=str(exc)[:300])
            except Exception:
                pass
            return []

    def _persist(self, market_data: Dict[str, Any], shortlist: List[Candidate], error: Optional[str] = None) -> None:
        try:
            from src.storage import NewsDiscoveryRun, get_db

            db = self._db or get_db()
            with db.session_scope() as session:
                session.add(NewsDiscoveryRun(
                    model=self.settings.triage_model,
                    news_count=len(market_data.get("news", [])),
                    shortlist_json=json.dumps([c.to_dict() for c in shortlist], ensure_ascii=False),
                    raw_json=json.dumps(market_data, ensure_ascii=False, default=str)[:200000],
                    error=error,
                ))
        except Exception as exc:
            logger.warning("Discovery: persisting run failed: %s", exc)
