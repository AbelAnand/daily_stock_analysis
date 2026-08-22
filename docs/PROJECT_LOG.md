# Project Log

Running log of what was done to this fork, why, and how it was verified. Newest entries at the bottom. Every change to the system — code, config semantics, schedule, trading parameters — gets an entry here before it is pushed.

Fork: `github.com/AbelAnand/daily_stock_analysis` (upstream: `ZhuLinsen/daily_stock_analysis`, base `96bc532d`).
Runtime: this Linux box, `.venv/`, systemd user timer `dsa-daily.timer`. Secrets live only in the gitignored `.env`.

---

## 2026-08-20 — Clone, security audit, first look

- Cloned upstream into `/home/abe/daily_stock_analysis`, installed dependencies into `.venv`.
- **Security audit** of the upstream code before running it with real keys. Verdict: clean — no exfiltration, telemetry, obfuscation, or typosquatted dependencies; every outbound request goes to the configured provider. Real risks are configuration-side: the WebUI's API returns all API keys in plaintext and admin auth is off by default; `server.py` hardcodes a `0.0.0.0` bind; the Docker compose publishes port 8000 to the LAN.
  - Mitigations applied: `ADMIN_AUTH_ENABLED=true` in `.env`; WebUI stays disabled and bound to `127.0.0.1`; never use `python server.py`.
- **Signal-quality review.** Confirmed the hypothesis that the stock system produced hedged "hold" calls by construction:
  - `buy` was unreachable for US/HK stocks (missing A-share capital-flow feed was treated as disconfirming evidence);
  - a one-way post-processing ratchet (buy→hold, sell→hold, never the reverse);
  - sentiment score clamped into 45–59 whenever a guardrail fired, and capped at 52 on "cautious" market text;
  - prompts explicitly instructed "prefer neutral when between support and resistance";
  - the backtest rewarded hedging (hold won on any non-negative return; watch won whenever nothing happened; abstentions excluded from accuracy).
- **Gap analysis.** Outcome scoring and backtest existed but never ran; default schedule was 05:00 ET; portfolio/cost basis never reached the analysis; position sizing was an LLM sentence; no earnings calendar.

## 2026-08-21 — Anti-hedging overhaul (commit `0a337c4a`)

Implemented as four parallel work packages plus a wiring pass.

- **Decision core** (`src/analyzer.py`, guardrails, `decision_agent.py`, `skills/defaults.py`, `strategies/bull_trend.yaml`): removed the score clamps (guardrails now annotate metadata instead of rewriting decisions/scores); capital-flow `not_supported` treated as absent evidence; `stabilize_decision_with_structure` made two-way (confirmed breakout + volume can promote hold→buy, support failure + outflow/volume hold→sell); prompts rewritten to an **expected-value contract** (`p_up`, entry/stop/target, R-multiple, EV; hold only when EV<0 or R:R<1.5 and it must name a flip condition); bias/alignment hard filters converted to sizing inputs; phase guardrail retimes actions to the next session instead of erasing them.
- **Trade levels** (`src/stock_analyzer.py`, new `src/utils/trade_levels.py`, `sniper_points.py`, `decision_signal_service.py`): ATR(14) and swing highs/lows computed in code; `compute_trade_levels()` derives entry/stop/target targeting R:R≥2; plan geometry validated (inverted stops, sub-1.5R flagged in `metadata.plan_check`); USD price parsing fixed.
- **Ensemble/risk** (`risk_agent.py`, `aggregator.py`, `risk_override.py`, `skill_opinion_weight_service.py`): risk agent no longer votes direction (severe-only veto: fraud/delisting/halt; otherwise `position_size_factor`); aggregator decides by vote distribution instead of a mean that collapsed into the hold band; feedback-weight prior relaxed to Beta(5,5), clamp [0.625, 1.6].
- **Accountability** (`backtest_engine.py`, `backtest_service.py`, `decision_signal_outcome_service.py`, `main.py`, `notification.py`): symmetric benchmark-relative scoring; strict accuracy (abstentions in the denominator); Brier score + reliability table; engine version v1→v2; outcome scoring runs after every daily analysis (`OUTCOME_SCORING_ENABLED`); scorecard in the digest; track record injected back into the prompt with a calibration hint.
- **US support**: new `earnings_calendar_service.py` (annotates buys within 7 days of earnings, never vetoes); second workflow cron at 21:00 UTC gated on `STOCK_LIST_US`.
- Verification: full suite 5,942 passed; 3 pre-existing upstream failures in `tests/test_decision_signal_service.py` (backfill-TTL) verified failing on pristine upstream. First live AAPL run: decisive Buy with `p_up` 55 %, R 2.0, EV +0.65 — the same setup that the original code would have forced to Hold/Low.
- Follow-ups from the live run: `p_up` scale mismatch (0–100 emitted, 0–1 expected by calibration readers) normalized; `p_up`/R/EV persisted into signal metadata; Trade Math line added to the report; `max_output_tokens` raised 8192→16384 (thinking tokens truncated JSON and caused double LLM calls).

## 2026-08-21 — Setup and operations

- `.env`: Anthropic key via LiteLLM channel, Tavily for news, Telegram bot + chat, `REPORT_LANGUAGE=en`, `MARKET_REVIEW_REGION=us`, temporary `STOCK_LIST=AAPL,MSFT,NVDA,AMZN,GOOGL` pending the user's real watchlist.
- Model: started on `claude-opus-5` (~$0.23/stock); **switched to `claude-sonnet-5`** at the user's request. Measured: Sonnet uses ~2× the output tokens (adaptive thinking), so per-stock cost is ~$0.16–0.23 (≈$0.11–0.16 at intro pricing through 2026-08-31) — a 20–40 % saving, less than the sticker price suggests. Calibration table will decide whether quality holds.
- Schedule: systemd user timer `dsa-daily.timer`, Mon–Fri 16:30 America/New_York, `Persistent=true`. Telegram delivery verified.
- Test hygiene: `src/auth.py` reads `ADMIN_AUTH_ENABLED` from the `.env` file and upstream tests pop `ENV_FILE`, so a real `.env` with auth on causes ~12 bogus 401 failures. Run the suite with `.env` moved aside: `mv .env .env.local-config && .venv/bin/python -m pytest tests/ -q; mv .env.local-config .env`.

## 2026-08-21 — English-first pass (commits `460dc0ab`, `df250861`)

- Scope decided with the user: everything user-visible plus the LLM prompts and strategy files; code comments, tests and docs left alone.
- Translated: analyzer/agent/market-review prompts (English base; zh/ko blocks kept for `REPORT_LANGUAGE`), all 15 `strategies/*.yaml`, report layer with locale-aware units (K/M/B shares, `$…B`), share-image poster, notification senders and templates, logs/CLI/progress/validation text, alert messages, bot replies, Web UI default language `en` (explicit preference or toggle switches to zh), desktop shell dialogs.
- Intentionally unchanged: zh label tables, Chinese keyword-acceptance lists (stored reports still parse), A-share data column keys, CN/HK search queries.
- Bugs found by the pass: screening score maps only recognized Chinese advice values (would have scored English reports as 0); MA-alignment labels and raw trend/volume enum values leaked into English prompts; `KeyError` for Korean in source display names; alert excerpts ignored `report_language`; Chinese mapped company names surfaced in English logs.
- Operational lesson: 8 parallel subagents exhausted the plan's 5-hour session window and spilled into usage credits up to the monthly cap. Going forward: ≤3 concurrent agents, Sonnet for mechanical work.
- Verification: 5,943 passed; remaining failures are the 3 upstream ones plus 2 live-network TWSE tests. A full scheduled run on the final tree delivered to Telegram with zero Chinese characters in the report; outcome scorer evaluated 7 signals.

## 2026-08-21 — Paper-trading execution via Alpaca (commit `6e84f0f4`)

- Decision: make the system autonomous on **paper**. Parameters set by the user: max loss **$500 per trade** at the stop; account **B or C, whichever is larger**; place orders through the API (no approval gate).
- Account check: B = `PA3Z9EX3PG0C`, $30,708 equity, no positions, confirmed paper. **C's keys are rejected by Alpaca ("unauthorized")** — pinned `PAPER_TRADING_ACCOUNT=B`; `auto` would pick the larger reachable account if C is fixed.
- Built `src/services/paper_trading_service.py` + `paper_trades` table + `main.py` hook (after outcome scoring):
  - buy/add → GTC **bracket** (limit at the top of the entry range, attached stop and target); sell → close; reduce → sell half; hold/watch → nothing.
  - sizing `floor($500 / (entry − stop))`, capped by 20 % of equity per name and 10 open positions; whole shares.
  - skips: already holding, order pending, `plan_check.valid == false`, R:R < 1.5, earnings within horizon, signal older than 36 h, non-US.
  - cancels unfilled entries older than 3 days; paper-account assertion on every client; kill switch `data/paper_trading.STOP`; `PAPER_TRADING_DRY_RUN`.
  - every decision (including skips with reason) persisted and summarised to Telegram as "Paper Orders".
- First orders placed 2026-08-21 evening for Monday's open: AAPL 19 @≤308.00 (stop 298.24 / target 332.05), MSFT 12 @≤481.59 (464.64 / 512.76), GOOGL 17 @≤343.68 (330.47 / 373.51). NVDA and AMZN were Watch → no orders. Realized risk per trade $185–225: the 20 % position cap binds before the $500 risk cap on a $30k account.
- Verification: 18 unit tests with a fake broker; dry run against real signals; live submission confirmed via Alpaca open-orders query.

## Open decisions

- **Run timing.** Agreed in discussion that a single **pre-market run (~08:00 ET)** is better than post-close for an execution-driven system: same daily bars, fresher news (overnight/weekend/pre-market earnings), knowledge of gaps, and orders live minutes later instead of 17–65 h later. Not yet moved — awaiting go-ahead.
- **Real watchlist** — `STOCK_LIST` is still the temporary sample.
- **Sizing** — raise `PAPER_TRADING_MAX_POSITION_PCT` if the full $500 risk per trade is wanted on a $30k account (implies ~40 % of equity in one name at current prices).
- **Account C** — regenerate keys if it should be eligible.
