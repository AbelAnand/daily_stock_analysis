# -*- coding: utf-8 -*-
"""Trade post-mortems: the paper-trading system learning from its own closed trades.

Human traders review losers (and winners) and adjust; autonomous systems
usually don't. This service closes that loop, deliberately bounded:

1. **Reconstruct** every finished round trip from the recorded entry orders
   (``paper_trades``) and the broker's fill history — entry fill, exit fill,
   which bracket leg fired, realized P&L and R.
2. **Review** each one with an LLM given the original thesis and plan, the
   fills, and the price action afterwards. The reviewer classifies the trade
   (``bad_thesis`` / ``bad_entry`` / ``bad_stop_placement`` / ``bad_target`` /
   ``execution_flaw`` / ``variance`` / ``good_process``) and may distill ONE
   generalizable lesson.
3. **Feed back**: recent lessons are injected into the analyst prompt as the
   system's own track record (annotate, never veto — same philosophy as the
   guardrails). ``execution_flaw`` findings are escalated to the notification
   digest for a human/code fix; the system never tunes its own parameters.

A strategy scorecard by (source × direction) accompanies the digest so weak
strategies can be killed by the operator with evidence, not vibes.

Everything is best-effort and never breaks the trading flow. One LLM call per
closed trade (low volume); each entry order is reviewed exactly once.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

VALID_CATEGORIES = ("bad_thesis", "bad_entry", "bad_stop_placement", "bad_target",
                    "execution_flaw", "variance", "good_process")

REVIEW_PROMPT = """You are the risk/review desk of a systematic equity strategy doing a post-mortem on ONE closed paper trade. Be blunt and specific; the goal is a better process, not comfort.

## The trade
{trade_block}

## The original thesis and plan
{plan_block}

## Price action after entry (daily bars)
{bars_block}

## Your task
Classify what this trade says about the PROCESS (not the outcome — a losing trade can be good process, a winner can be luck):
- bad_thesis: the reasoning was wrong or the catalyst was stale/priced in
- bad_entry: right idea, but the entry price/timing gave it no room
- bad_stop_placement: stop inside the noise band, or not at the invalidation level
- bad_target: target unrealistic or too timid for the thesis
- execution_flaw: the SYSTEM did something mechanical wrong (fills, order handling) — flag for engineers
- variance: sound process, adverse outcome (or lucky win); nothing to learn
- good_process: sound process AND it worked as designed

Then, ONLY if there is a genuinely generalizable lesson a future analysis should apply (not a restatement of the outcome, not symbol-specific trivia), state it in one sentence. Prefer no lesson over a weak one.

Respond with ONLY this JSON:
{{"outcome": "win"|"loss"|"scratch", "process_quality": "good"|"flawed", "category": "<one of the categories>", "lesson": "<one sentence>"|null, "lesson_confidence": "low"|"medium"|"high", "review": "<2-3 sentences of reasoning>"}}
"""


@dataclass
class PostmortemSettings:
    enabled: bool = False
    model: str = "anthropic/claude-sonnet-5"
    lookback_days: int = 30
    lessons_in_prompt: int = 5

    @classmethod
    def from_env(cls, env: Optional[Dict[str, Any]] = None) -> "PostmortemSettings":
        env = env if env is not None else os.environ

        def _int(key: str, default: int) -> int:
            try:
                raw = env.get(key)
                return int(raw) if raw not in (None, "") else default
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=str(env.get("TRADE_POSTMORTEM_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on"),
            model=str(env.get("TRADE_POSTMORTEM_MODEL") or cls.model).strip(),
            lookback_days=_int("TRADE_POSTMORTEM_LOOKBACK_DAYS", 30),
            lessons_in_prompt=_int("TRADE_LESSONS_IN_PROMPT", 5),
        )


class TradePostmortemService:
    def __init__(self, settings: Optional[PostmortemSettings] = None, *,
                 broker: Any = None, db: Any = None, env: Optional[Dict[str, Any]] = None,
                 review_fn: Any = None):
        self.settings = settings or PostmortemSettings.from_env(env)
        self._broker = broker          # AlpacaPaperBroker (or fake)
        self._db = db
        self._env = env if env is not None else os.environ
        self._review_fn = review_fn    # injectable for tests: (prompt) -> str

    @property
    def db(self):
        if self._db is None:
            from src.storage import get_db
            self._db = get_db()
        return self._db

    @property
    def broker(self):
        if self._broker is None:
            from src.services.paper_trading_service import PaperTradingSettings, connect_account
            self._broker = connect_account(PaperTradingSettings.from_env(self._env), self._env)
        return self._broker

    # --- public -----------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        """Review every newly closed round trip. Never raises."""
        summary: Dict[str, Any] = {"enabled": self.settings.enabled, "reviewed": [], "error": None}
        if not self.settings.enabled:
            summary["error"] = "disabled"
            return summary
        try:
            entries = self.db.list_unreviewed_paper_entries(days=self.settings.lookback_days)
        except Exception as exc:
            logger.error("Postmortem: entry lookup failed: %s", exc)
            summary["error"] = f"db_unavailable: {exc}"
            return summary
        for rec in entries:
            try:
                row = self._review_entry(rec)
                if row is not None:
                    summary["reviewed"].append(row)
            except Exception as exc:
                logger.exception("Postmortem for %s (%s) failed", rec.symbol, rec.order_id)
        try:
            summary["scorecard"] = self.db.get_postmortem_bucket_stats()
        except Exception:
            summary["scorecard"] = []
        return summary

    # --- round-trip reconstruction -----------------------------------------
    def _review_entry(self, rec: Any) -> Optional[Dict[str, Any]]:
        """Review one recorded entry order; returns the persisted row dict, or None when still open."""
        trip = self._round_trip(rec)
        if trip is None:
            return None  # entry still working or position still open — try again next run
        if trip.get("exit_kind") == "no_entry":
            return self._persist(rec, trip, review=None)  # cancelled/expired entry: record, no LLM
        review = self._review_llm(rec, trip)
        return self._persist(rec, trip, review)

    def _round_trip(self, rec: Any) -> Optional[Dict[str, Any]]:
        is_short = rec.side == "sell_short"
        order = self.broker.order_with_legs(rec.order_id)
        if order is None:
            return {"exit_kind": "no_entry", "note": "entry order not found at broker"}
        status = str(order.get("status") or "")
        if status in ("new", "accepted", "pending_new", "partially_filled", "held"):
            return None  # entry still working
        if status != "filled":
            return {"exit_kind": "no_entry", "note": f"entry {status}"}

        entry_price = _f(order.get("filled_avg_price"))
        entry_at = order.get("filled_at")
        qty = _f(order.get("filled_qty")) or _f(rec.qty) or 0.0
        exit_price = exit_at = None
        exit_kind = None
        for leg in order.get("legs") or []:
            if str(leg.get("status")) == "filled" and _f(leg.get("filled_avg_price")):
                exit_price = _f(leg.get("filled_avg_price"))
                exit_at = leg.get("filled_at")
                exit_kind = "stop" if leg.get("stop_price") is not None else "target"
                break
        if exit_price is None:
            # No bracket leg fired. If the position is gone, it was closed directly
            # (cover on flip / sell signal) — find the flattening fill.
            positions = self.broker.positions()
            pos = positions.get(str(rec.symbol).upper())
            if pos is not None and ((pos.qty < 0) == is_short) and pos.qty != 0:
                return None  # still open
            flat = self.broker.first_flattening_fill(
                rec.symbol, side="buy" if is_short else "sell", after=entry_at, qty=qty)
            if flat is None:
                return None  # closed but fill not found yet; retry next run
            exit_price, exit_at, exit_kind = flat.get("price"), flat.get("at"), "close"

        if not entry_price or not exit_price or not qty:
            return None
        pnl = (entry_price - exit_price) * qty if is_short else (exit_price - entry_price) * qty
        plan_stop = _f(rec.stop_price)
        risk_ps = abs(plan_stop - entry_price) if plan_stop else None
        r_realized = round(pnl / (risk_ps * qty), 2) if risk_ps else None
        holding_hours = None
        try:
            holding_hours = round((_dt(exit_at) - _dt(entry_at)).total_seconds() / 3600.0, 1)
        except Exception:
            pass
        return {
            "direction": "short" if is_short else "long",
            "entry_price": round(entry_price, 4), "exit_price": round(exit_price, 4),
            "entry_at": str(entry_at), "exit_at": str(exit_at), "qty": qty,
            "pnl_usd": round(pnl, 2), "r_realized": r_realized,
            "exit_kind": exit_kind, "holding_hours": holding_hours,
        }

    # --- LLM review ---------------------------------------------------------
    def _review_llm(self, rec: Any, trip: Dict[str, Any]) -> Dict[str, Any]:
        prompt = self._build_prompt(rec, trip)
        raw = self._review_fn(prompt) if self._review_fn else self._call_llm(prompt)
        return self._parse_review(raw)

    def _build_prompt(self, rec: Any, trip: Dict[str, Any]) -> str:
        trade_block = (
            f"- {trip['direction'].upper()} {rec.symbol} x{int(trip['qty'])}\n"
            f"- Entry {trip['entry_price']} at {trip['entry_at']}\n"
            f"- Exit {trip['exit_price']} at {trip['exit_at']} via {trip['exit_kind']}\n"
            f"- P&L ${trip['pnl_usd']} ({trip['r_realized'] if trip['r_realized'] is not None else '?'}R of planned risk)"
            f" · held {trip.get('holding_hours', '?')}h"
        )
        plan_lines = [f"- Plan: entry {rec.limit_price}, stop {rec.stop_price}, target {rec.target_price}, planned R {rec.r_multiple}"]
        signal = self._signal_context(rec.signal_id)
        if signal:
            for k in ("action", "score", "confidence", "reason"):
                v = signal.get(k)
                if v not in (None, ""):
                    plan_lines.append(f"- {k}: {str(v)[:400]}")
            meta = signal.get("metadata") or {}
            for k in ("p_up", "ev_r_multiple", "ev_expected_value", "flip_condition"):
                if meta.get(k) is not None:
                    plan_lines.append(f"- {k}: {str(meta[k])[:200]}")
        bars = self._bars_since(rec.symbol, trip.get("entry_at"))
        bars_block = "\n".join(bars) if bars else "(no bars available)"
        return REVIEW_PROMPT.format(trade_block=trade_block, plan_block="\n".join(plan_lines), bars_block=bars_block)

    def _signal_context(self, signal_id: Optional[int]) -> Optional[Dict[str, Any]]:
        if not signal_id:
            return None
        try:
            from src.services.decision_signal_service import DecisionSignalService
            got = DecisionSignalService().get_signal(int(signal_id))
            return got if isinstance(got, dict) else None
        except Exception as exc:
            logger.debug("Postmortem: signal %s lookup failed: %s", signal_id, exc)
            return None

    def _bars_since(self, symbol: str, entry_at: Any, max_bars: int = 10) -> List[str]:
        try:
            start = _dt(entry_at) - timedelta(days=1)
        except Exception:
            start = datetime.now(timezone.utc) - timedelta(days=10)
        try:
            bars = self.broker.daily_bars(symbol, start=start, limit=max_bars)
            return [f"- {b['date']}: open {b['open']} high {b['high']} low {b['low']} close {b['close']}" for b in bars]
        except Exception as exc:
            logger.debug("Postmortem: bars for %s unavailable: %s", symbol, exc)
            return []

    def _call_llm(self, prompt: str) -> str:
        import litellm

        api_key = (self._env.get("ANTHROPIC_API_KEY") or self._env.get("LLM_ANTHROPIC_API_KEY") or "").strip()
        kwargs: Dict[str, Any] = {
            "model": self.settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000,
            "timeout": 120,
        }
        if api_key:
            kwargs["api_key"] = api_key
        response = litellm.completion(**kwargs)
        return response.choices[0].message.content or ""

    def _parse_review(self, raw: str) -> Dict[str, Any]:
        match = re.search(r"\{.*\}", str(raw), re.DOTALL)
        out: Dict[str, Any] = {"outcome": None, "process_quality": None, "category": None,
                               "lesson": None, "lesson_confidence": None, "review": None}
        if not match:
            return out
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return out
        if not isinstance(data, dict):
            return out
        out["outcome"] = data.get("outcome") if data.get("outcome") in ("win", "loss", "scratch") else None
        out["process_quality"] = data.get("process_quality") if data.get("process_quality") in ("good", "flawed") else None
        out["category"] = data.get("category") if data.get("category") in VALID_CATEGORIES else None
        lesson = data.get("lesson")
        out["lesson"] = str(lesson)[:400] if isinstance(lesson, str) and lesson.strip() else None
        out["lesson_confidence"] = data.get("lesson_confidence") if data.get("lesson_confidence") in ("low", "medium", "high") else None
        review = data.get("review")
        out["review"] = str(review)[:1000] if isinstance(review, str) else None
        return out

    # --- persistence ----------------------------------------------------------
    def _persist(self, rec: Any, trip: Dict[str, Any], review: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        no_entry = trip.get("exit_kind") == "no_entry"
        outcome = "no_fill" if no_entry else ((review or {}).get("outcome")
                                              or ("win" if trip.get("pnl_usd", 0) > 0 else "loss" if trip.get("pnl_usd", 0) < 0 else "scratch"))
        category = "no_entry" if no_entry else ((review or {}).get("category") or "variance")
        lesson = None if no_entry else (review or {}).get("lesson")
        # execution_flaw is an engineering escalation, not analyst guidance — never inject it.
        inject = bool(lesson) and category not in ("variance", "no_entry", "execution_flaw")
        source = "watchlist"
        try:
            if self.db.was_discovery_symbol(rec.symbol, around=rec.created_at):
                source = "discovery"
        except Exception:
            pass
        row = {
            "symbol": str(rec.symbol).upper(),
            "direction": trip.get("direction") or ("short" if rec.side == "sell_short" else "long"),
            "source": source, "entry_order_id": rec.order_id, "signal_id": rec.signal_id,
            "entry_price": trip.get("entry_price"), "exit_price": trip.get("exit_price"),
            "qty": trip.get("qty"), "pnl_usd": trip.get("pnl_usd"), "r_realized": trip.get("r_realized"),
            "exit_kind": trip.get("exit_kind"), "holding_hours": trip.get("holding_hours"),
            "outcome": outcome, "process_quality": (review or {}).get("process_quality"),
            "category": category, "lesson": lesson,
            "lesson_confidence": (review or {}).get("lesson_confidence"),
            "prompt_inject": inject, "model": None if no_entry else self.settings.model,
        }
        try:
            from src.storage import TradePostmortemRecord

            with self.db.session_scope() as session:
                session.add(TradePostmortemRecord(
                    raw_json=json.dumps({"trip": trip, "review": review}, ensure_ascii=False, default=str),
                    **row,
                ))
        except Exception as exc:
            logger.warning("Postmortem: persisting %s failed: %s", rec.symbol, exc)
        logger.info("Postmortem %s %s: %s / %s%s", row["symbol"], row["direction"], outcome, category,
                    f' — lesson: {lesson}' if lesson else "")
        return row


# ============================================================
# Prompt lessons + digest formatting
# ============================================================

def build_lessons_context(db: Any = None, limit: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Lessons context for the analyst prompt (None when there are none). Never raises."""
    try:
        settings = PostmortemSettings.from_env()
        n = limit if limit is not None else settings.lessons_in_prompt
        if n <= 0:
            return None
        if db is None:
            from src.storage import get_db
            db = get_db()
        rows = db.get_recent_trade_lessons(limit=n)
        if not rows:
            return None
        lessons = []
        for r in rows:
            when = ""
            try:
                when = r.created_at.date().isoformat()
            except Exception:
                pass
            lessons.append({
                "category": r.category, "lesson": r.lesson, "symbol": r.symbol,
                "direction": r.direction, "r_realized": r.r_realized, "outcome": r.outcome, "when": when,
            })
        return {"lessons": lessons}
    except Exception as exc:
        logger.debug("Lessons context unavailable: %s", exc)
        return None


def format_postmortem_summary(summary: Dict[str, Any]) -> str:
    """Digest section: newly reviewed trades + the strategy scorecard."""
    if not summary or summary.get("error") == "disabled":
        return ""
    reviewed = [r for r in summary.get("reviewed", []) if r.get("outcome") != "no_fill"]
    dead = [r for r in summary.get("reviewed", []) if r.get("outcome") == "no_fill"]
    if not reviewed and not dead and not summary.get("error"):
        return ""
    lines = ["🧠 **Trade Review**"]
    if summary.get("error"):
        lines.append(f"Not run: {summary['error']}")
        return "\n".join(lines)
    for r in reviewed:
        emoji = "✅" if (r.get("pnl_usd") or 0) > 0 else "❌"
        r_txt = f" ({r['r_realized']:+.1f}R)" if r.get("r_realized") is not None else ""
        lines.append(f"{emoji} {r['symbol']} {r['direction']} ${r.get('pnl_usd', 0):+,.0f}{r_txt} via {r.get('exit_kind')} — {r.get('category')}")
        if r.get("lesson"):
            lines.append(f"   📚 {r['lesson']}")
        if r.get("category") == "execution_flaw":
            lines.append("   🔧 execution flaw — needs an engineering fix, not a prompt lesson")
    if dead:
        lines.append(f"Entries that never filled: {', '.join(r['symbol'] for r in dead)}")
    scorecard = summary.get("scorecard") or []
    if scorecard and reviewed:
        lines.append("Strategy scorecard (closed trades):")
        for b in scorecard:
            avg_r = f", avg {b['avg_r']:+.2f}R" if b.get("avg_r") is not None else ""
            lines.append(f"  · {b['source']}/{b['direction']}: {b['wins']}/{b['trades']} wins, ${b['net_pnl_usd']:+,.0f}{avg_r}")
    return "\n".join(lines)


# --- helpers -----------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    try:
        f = float(value)
        return f if math.isfinite(f) and f > 0 else None
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
