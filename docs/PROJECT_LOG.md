# Project Log

Running log of what was done to this fork, why, and how it was verified. Newest entries at the bottom. Every change to the system — code, config semantics, schedule, trading parameters — gets an entry here before it is pushed.

Fork: `github.com/AbelAnand/daily_stock_analysis` (upstream: `ZhuLinsen/daily_stock_analysis`, base `96bc532d`).
Runtime: this Linux box, `.venv/`, systemd user timer `dsa-daily.timer`. Secrets live only in the gitignored `.env`.

---

## Runbook — the trading dashboard (web UI)

The dashboard is the project's web UI served by FastAPI. It is **not** a systemd unit: it is a plain background process that dies on reboot and must be started by hand.

**Start**

```bash
cd /home/abe/daily_stock_analysis
nohup .venv/bin/python webui.py > logs/web_server.nohup.log 2>&1 &
```

Then open http://127.0.0.1:8000/trading (API docs at http://127.0.0.1:8000/docs). Startup takes a few seconds; `WEBUI_AUTO_BUILD=true` re-runs the frontend build only when `apps/dsa-web/` changed.

**Check / stop / restart**

```bash
pgrep -fa "python webui.py"                               # is it running?
curl -s http://127.0.0.1:8000/api/v1/health               # does it answer?
pkill -f "python webui.py"                                # stop
tail -f logs/web_server.nohup.log                         # logs
```

Restart = stop, then the start command again. After changing frontend code, run `cd apps/dsa-web && npm run build` first so `static/` is fresh.

**What it needs**

- `.env`: `ALPACA_<LABEL>_KEY_ID` / `_SECRET_KEY` (paper accounts only; `PAPER_TRADING_ACCOUNT=auto` picks the reachable one with the most equity). Without them the Trading page shows a "not connected" state.
- `WEBUI_HOST=127.0.0.1`, `WEBUI_PORT=8000` (defaults). Bound to localhost only.
- `ADMIN_AUTH_ENABLED=false` since 2026-09-04 (no login). Set it back to `true` before ever setting `WEBUI_HOST=0.0.0.0`; the Settings page can change every secret and the Trading page can flatten the account.

`webui.py` runs only the web server and API, not the in-app scheduler, so it never doubles up with the `dsa-daily` / `dsa-manage` systemd timers.

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

## 2026-08-25 — Schedule moved to pre-market 08:00 ET

- User gave the go-ahead on the open run-timing decision: `dsa-daily.timer` moved from Mon–Fri 16:30 ET (post-close) to **Mon–Fri 08:00 ET (pre-market)**; unit descriptions updated to match. Rationale unchanged from the original discussion: same daily bars, fresher overnight/pre-market news, orders live minutes after analysis instead of 17–65 h later.
- Gotcha hit during the switch: with `Persistent=true`, restarting the timer at 10:14 PDT made systemd treat today's already-past 08:00 ET slot as missed and fire an immediate catch-up run mid-session. Stopped it ~20 s in (still in news search — no LLM calls, no orders, no notification). Consequence: no run at all on 2026-08-25; next run Wed 2026-08-26 08:00 ET.
- The three stale Friday bracket entries (AAPL/MSFT/GOOGL, signals since downgraded to Watch) remain open until a run cancels them via the 3-day TTL — expected at the Wednesday pre-market run, unless price touches a limit first.
- Verification: `systemctl --user list-timers` shows next trigger Wed 2026-08-26 05:01 PDT (08:01 ET incl. randomized delay); service confirmed stopped; today's log ends in news-search lines with no analysis output.

## 2026-08-27 — First fill; stale-order race fixed; entries now chase the market

- **First position**: Wednesday 09:40 ET the GOOGL entry from 2026-08-21 filled — 17 @ $343.61; bracket intact (target 373.51 `new`, stop 330.47 `held` — normal Alpaca OCO). Thursday's TTL sweep correctly cancelled the stale AAPL/MSFT entries.
- **Bug found in Thursday's run**: the fresh AAPL (Buy 68) / MSFT (Buy 65) signals were skipped as "open order already pending" by the very orders that run had just cancelled — `open_symbols` was built from the pre-cancellation snapshot. Fixed in `run()`: symbols of TTL-cancelled entries (whose bracket legs die with them) are excluded; held positions stay protected by the earlier "already holding" check.
- **Entry style change (user request: more trades, no below-market limits that never fill)**: `_decide` now fetches a live quote (`AlpacaPaperBroker.latest_price`, Alpaca data API, same keys). If price > planned entry, the bracket entry becomes a marketable limit at latest price + `PAPER_TRADING_CHASE_PCT` % (new setting, default 0.3, in `.env.example` + docs), accepted only while R:R recomputed at the chased price ≥ `PAPER_TRADING_MIN_R_MULTIPLE`; sizing uses the chased entry. Quote unavailable → old behavior. Structure stop/target are never moved — the chase window is bounded by the R:R floor, so it cannot chase into bad geometry.
- Verification: 23/23 unit tests (5 new: same-run unblock, chase pricing, chase R:R refusal, at/below-entry unchanged, missing-quote fallback); `py_compile` clean. One-off live re-run of the paper step at 12:40 ET placed nothing — by then AAPL/MSFT had run to R:R 0.75/0.49 at market, correctly refused; the pre-market run operates minutes after quotes, where the chase actually helps.
- Known limitation: on strong gap-up days the R:R floor still blocks entry (that is the anti-chasing discipline working); the lever for more aggression is `PAPER_TRADING_MIN_R_MULTIPLE`, deliberately left at 1.5 pending user choice.
- **Live chase test (user-requested, before commit)**: fabricated in-memory 1-share signal against the real account — chased limit $315.40 (quote $314.455 + 0.3 %) accepted by Alpaca, filled in <2 s @ $314.46, target leg `new` / stop leg `held` as expected. The cleanup step then exposed a latent **sell-path bug**: `close_position` cancelled the visible leg and immediately tried to close while the OCO stop (status `held`, invisible to the open-orders query) still held the shares → 403 "insufficient qty available". Every real sell signal on a bracket position would have failed. Fixed with a bounded retry loop in `AlpacaPaperBroker.close_position` waiting for the hold to release; verified live by closing the test position (clean: no position, no dangling orders). Net cost of the round trip: a few cents of paper spread.

## 2026-08-27 — Profit replay of the week's signals; split R:R floor (chase ≥ 1.3)

- User asked for a profit test of the chase behavior. Replayed all 9 real buy signals (8/21–8/27) against actual IEX daily bars with real sizing and one-position-per-symbol semantics, marked to market 8/27 midday:
  - Reality (race bug + passive limits): **-$45**. Old policy bug-free: +$202 (but its MSFT winner required a lucky same-day dip to the limit). Chase with 1.5 floor as deployed: +$146 — refused Fri AAPL (R:R 1.39 at Mon open) and Fri MSFT (1.40), the week's two biggest winners. Chase with 1.3 floor: **+$273** (MSFT from Mon open +$251). Floor 1.0 added nothing over 1.3.
  - Pattern: strong signals opened above their planned entries and never pulled back; passive limits missed them entirely, and a 1.5 floor at the *chased* price re-blocked them by margins (0.10–0.11 R:R) inside target-estimation noise. Caveats recorded: 4 trading days, 3 correlated mega-caps, an up week — directional evidence only.
- User decision: **split floor**. Plan-quality gate stays `PAPER_TRADING_MIN_R_MULTIPLE=1.5` at the planned entry; execution may chase while R:R at the chased price ≥ new `PAPER_TRADING_CHASE_MIN_R` (default 1.3). Implemented in `_decide` (plan gate now explicitly evaluated at planned entry, chase gate at chased price), `.env.example`, docs, changelog; new unit test covers the 1.3–1.5 band.

## 2026-08-31 — Open positions are now managed: break-even / trailing stop ratchet

- **Problem (user)**: after a fill the system ignored the position — the bracket's stop and target were static, so a trade that went well into profit could still round-trip back to the original stop. User rule: once a trade is positive, it must not be allowed to hit the initial stop; move the stop to where the position is profitable.
- **Verified first**: Alpaca accepts an order *replace* on the OCO stop leg even though it sits in status `held` (invisible to the open-orders query). Tested on the live GOOGL bracket: 330.47 → 330.48 → back to 330.47; new leg id each time, take-profit leg untouched, bracket intact.
- **Implementation** (`src/services/paper_trading_service.py`):
  - `AlpacaPaperBroker.bracket_stop(symbol)` finds the live stop leg through the filled parent (nested order query); `replace_stop(order_id, new_stop)` replaces it in place.
  - `PaperTradingService.manage_positions()` — for every open position: `R = entry − initial stop` (initial stop from the recorded entry order via new `DatabaseManager.get_latest_paper_entry`, so a ratcheted stop does not shrink R; falls back to the current stop distance). Below `PAPER_TRADING_BREAKEVEN_TRIGGER_R` (1.0R) nothing happens. At/above it the stop becomes `max(entry × (1 + PAPER_TRADING_BREAKEVEN_BUFFER_PCT 0.2 %), price − PAPER_TRADING_TRAIL_R 1.0 × R)`, capped `PAPER_TRADING_STOP_MIN_GAP_PCT` 0.5 % below the latest price. **Never lowered.** Only replaces when the new stop is higher; runs only while the market is open (quotes are stale otherwise); respects the kill switch and `PAPER_TRADING_DRY_RUN`; positions without a live bracket stop are flagged ⚠️ unprotected. Moves/errors are persisted to `paper_trades` as `action = trail_stop`; no-op checks are only logged.
  - Effect for GOOGL (17 @ 343.61, R = 13.14): at +1R (356.75) stop → 344.30 (locks ≈ $12); at +2R (369.89) stop → 356.75 (locks ≈ $223); target 373.51 unchanged.
- **Where it runs**: (a) at the end of the daily run after entries (`_run_paper_trading`, summary appended to the Telegram "Paper Orders" message as "Stop Management"); (b) new `python main.py --manage-positions` — broker only, no data/LLM, notifies only on a move/unprotected/error — driven by new systemd user timer `dsa-manage.timer` (Mon–Fri 09:45 then every 15 min 10:00–15:45 ET, `Persistent=false`). Enabled 2026-08-31; first trigger Tue 2026-09-01 09:45 ET.
- **Design choice**: trigger at 1R rather than "any positive" — a stop parked at entry while the trade is up 0.3 % is just a noise stop-out; 1R is where the trade has proven itself. Lever: `PAPER_TRADING_BREAKEVEN_TRIGGER_R` (all four knobs in `.env.example`, docs/paper-trading.md, changelog).
- **Verification**: 35/35 unit tests (11 new: below trigger no-op, break-even+buffer at 1R, trailing at 2.5R, never-lower, cap below price, R anchored from DB after a ratchet, dry run, market closed, unprotected flag, quiet summary, kill switch). Live dry run against the real account: found held leg `e4848d84…`, R = 13.14 from the DB, decision `skipped: −0.34R below trigger`; +1R/+2R what-ifs produced the numbers above. `main.py --manage-positions --no-notify` end-to-end after hours → `market_closed`, exit 0. `py_compile` clean (flake8 not installed in the venv).
- **Not yet observed**: a real intraday replacement by the timer (no position is above +1R today). Watch Telegram for the first "🛡️ Stop Management ⬆️" message.

## 2026-09-02 — News-driven discovery, short selling, and gap-proof entries

User direction: stop trading only a fixed 5-ticker list — trade whatever the news says is moving, in **both directions** (user explicitly approved shorts). Also raised the idea of the system learning from its mistakes (deferred for now; the outcome/skill-weight loops already cover part of it, a trade post-mortem loop is a candidate next step).

- **News discovery** (`src/services/news_discovery_service.py`, opt-in `DISCOVERY_ENABLED`): pre-market scan of Alpaca news (Benzinga, 18 h lookback) + top-20 movers + most-actives → one cheap Haiku triage call picks up to 6 symbols with a concrete catalyst (direction hint, conviction 1–5, floor 3) → tradability vetting (active US equity, ≥ $5) → merged into the daily run's stock list (config-list runs only; any failure leaves the configured list untouched). Runs persisted to new `news_discovery_runs` table. Cost: triage ~cents; each candidate one full analysis (~$0.16–0.23) → ≈ $1–1.50/day at the cap.
- **Short selling** (opt-in `PAPER_TRADING_ALLOW_SHORT`, enabled in `.env` per user decision): rather than a 9th action (the extractor canonically aligns action to score bands — a "short" action would be rewritten), a bearish `sell`/`avoid` conclusion may carry `ev_contract.short_plan` (entry ≈ current, stop ABOVE at invalidation, target at support). Prompt teaches R_short = (entry−target)/(stop−entry) with p_down = 1−p_up; extractor persists only geometrically valid plans (target < entry < stop) to `metadata.short_plan`; paper layer executes them as short brackets with every long gate mirrored (plan floor 1.5, chase-*down* floor 1.3, notional/risk sizing, earnings veto, shortable + easy-to-borrow check). Bullish signal on an open short covers it; bearish signal on an open short holds. Stop ratchet mirrored (buy-stop ratchets down, never loosened). Outcome scoring rates short-plan signals against direction "down" (engine's `_classify_signal_outcome` extended; "down" already existed in cash-stance stats).
- **Gap-proof entries** (root-caused from NVDA 2026-09-01: pre-market chase quote ~220.8, marketable limit 221.46, opening gap filled it at 216.08 — *below* its own 216.70 stop → insta-stop 28 s later): new entries are **never submitted while the market is closed**. The pre-market run defers them (status `deferred`); the intraday pass executes fresh (≤36 h) signals against live quotes, so a gap re-runs the R:R gates instead of filling blind. Max one entry per symbol per day (`has_paper_entry_today`) — also kills post-stop-out re-entry loops. Exits remain allowed while closed (risk-reducing). TTL cancellation of short entries matches recorded order ids so protective sell legs are never cancelled.
- **Plumbing**: `--manage-positions` is now the full intraday trading pass (entries + ratchet; broker + local DB only, no LLM); `dsa-manage.timer` gained a 09:32 ET slot (then 09:45, every 15 min to 15:45). Storage helpers: `get_latest_paper_entry(side)`, `has_paper_entry_today`, `get_recent_paper_entry_order_ids`, `list_recent_signal_symbols`. Repeating intraday passes persist only actions/errors, not identical skips.
- **Verification**: 226 tests green across paper trading (51), discovery (7, new file), extractor (20, incl. 2 new short_plan cases), outcome service, backtest engine, scorecard — run with `.env` moved aside. Live: discovery end-to-end against real Alpaca data (48 headlines → 6 candidates incl. a NIO short on a JPM downgrade; tradability checks pass; triage on Haiku); intraday pass live at 15:21 ET — all five symbols correctly evaluated, MSFT blocked by the same-day guard after its morning stop-out, GOOGL ratchet check ran. Timer was **paused during the edit window** (12:16–12:21 PDT) so no partially-edited code could submit orders; the 12:15 run pre-dated the entry wiring and only checked stops.
- **Risk notes**: exposure can now grow to `max_positions` (10) × $500 risk much faster than with 5 tickers; per-name cap 20 % of equity unchanged. Short losses are theoretically unbounded above the stop on halts/news gaps — paper account, and every short carries a bracket stop from birth. Display layers (WebUI/zh label tables) don't know about `short_plan`; it lives in metadata only.

## 2026-09-02 — Learning loop: trade post-mortems feed lessons back into the analyst

User green-lit the learning idea ("this is where we can afford to fail and kill strategies"). Built as a bounded loop, not self-modification:

- **`src/services/trade_postmortem_service.py`** (+ `trade_postmortems` table, broker helpers `order_with_legs` / `first_flattening_fill` / `daily_bars`): reconstructs every finished round trip from recorded entry orders + broker fills (which bracket leg fired, realized P&L/R, holding time; direct closes matched by the first flattening fill; unfilled entries recorded without an LLM call). One Sonnet review per closed trade classifies the *process* — bad_thesis / bad_entry / bad_stop_placement / bad_target / execution_flaw / variance / good_process — and may distill at most one generalizable lesson (told to prefer none over weak).
- **Feedback path**: `build_lessons_context` → pipeline context (`trade_lessons`, both classic and agent paths) → analyst prompt section "Lessons from this system's own closed trades" (annotate-not-veto wording, capped at TRADE_LESSONS_IN_PROMPT=5). `execution_flaw` lessons are NEVER injected — they get a 🔧 escalation line in the digest for engineering. Trades tagged by source (watchlist vs discovery, via shortlist lookback) and direction; digest gains a strategy scorecard by bucket (wins, net P&L, avg R) so weak strategies can be killed by the operator with evidence.
- **First live run reviewed our 4 real order chains** and was uncannily on target: NVDA 9/1 → `execution_flaw`, lesson "validate the fill price is on the correct side of the stop before submission" — the exact bug root-caused and fixed yesterday, found independently from the trade record alone; MSFT 9/2 stop-out → `variance`, no lesson invented (the discipline working); the two stale 8/21 entries → `no_fill`, zero LLM cost. Scorecard: watchlist/long 1/2 wins, −$44, avg −0.51R.
- **Verification**: 14 unit tests (round-trip reconstruction incl. short P&L sign, open-position skip, direct-close matching, cancelled-entry no-LLM path, review parsing, injection gating incl. execution_flaw exclusion, digest formatting); 240 tests green across the whole touched surface; live run above. Enabled in `.env` (`TRADE_POSTMORTEM_ENABLED=true`).
- **Deliberate bounds**: one review per entry order ever; lessons capped and recency-based, no ever-growing rulebook; variance produces nothing; the system never adjusts its own parameters — parameter/code changes remain human decisions fed by the scorecard.

## 2026-09-04 — Trading desk: Web dashboard with P&L, positions, Close and Close-all

User asked for a bot dashboard in the style of a professional finance system: total P&L, open positions, a per-position close button and a "nuke all" button.

- **Backend** (`src/services/paper_dashboard_service.py`, `api/v1/endpoints/paper_trading.py`, `api/v1/schemas/paper_trading.py`): `GET /api/v1/paper-trading/dashboard` returns status (bot enabled / dry run / kill switch / shorts / market clock), account balances, P&L, exposure, positions, open orders and recent `paper_trades` rows. `POST …/positions/{symbol}/close` and `POST …/positions/close-all` reuse the broker's proven `close_position` path (legs cancelled first, retry while the OCO hold releases) and return per-symbol results; every manual close is persisted as `action=manual_close` so the post-mortem loop reviews it. Broker wrapper gained `position_details` (unrealized P&L etc.), `account_snapshot`, `clock`, `equity_at_start_of` (portfolio history `base_value`) and richer `OpenOrder` fields; storage gained `get_first_paper_entry_at`, `list_recent_paper_trades`, `get_realized_paper_pnl`. Service is cached on `app.state` via `api/deps.get_paper_dashboard_service`.
- **P&L definition** (decision): "Total P&L" = live equity − equity at the start of the day of the bot's first real entry (2026-08-22), anchored through Alpaca portfolio history. Probed live: since-inception history says −$2,336 (the account traded before the bot), since 8/22 says −$44.16, matching post-mortem realized −$44.22 + GOOGL unrealized +$0.34. Fallback to realized + unrealized with `total_pnl_basis` labelling. Day P&L = equity − last_equity.
- **Web** (`apps/dsa-web/src/pages/PaperTradingPage.tsx`, `/trading`, sidebar "Trading" after AI signals): KPI row (Total / Day / Unrealized P&L, Equity, Gross exposure), status badges, positions table (side, qty, avg entry, last, market value, unrealized $ and %, today, live stop with shield / "No stop" warning, target, R), open orders, recent activity, market-open pill, auto-refresh 15 s (pausable, visibility-aware). Close = confirm dialog; Close all = red dialog requiring the typed phrase `CLOSE ALL`, per-symbol results shown afterwards. zh + en strings.
- **Safety**: closing is allowed while the kill switch is set (risk-reducing); dry run records without sending; admin auth covers the routes; paper-only guard unchanged.
- **Verification**: 20 new backend tests (P&L anchoring + fallback, stop/target/R, short handling, kill switch, close/close-all incl. partial failure, dry run, 404/503 mapping) — 72 green with the existing paper-trading suite (`.env` moved aside). Web: eslint clean, `tsc -b` clean, 6 new page tests, full vitest 1105 passed / 1 pre-existing unrelated failure (`AlertRuleForm` JP/KR zh-mode test, fails on the untouched file in isolation), `npm run build` OK (static/ refreshed). Live read-only snapshot against account B validated against the Pydantic schema (GOOGL 17 @ 343.61, stop leg 330.47 / target 373.51 found). flake8 not installed in the venv; `py_compile` clean. Not exercised live: the actual close buttons (would flatten the real GOOGL position).

## 2026-09-04 — Login off, secrets masked in the web UI

User reaction to the login prompt on first web visit ("just visuals for this bot"): admin auth was on since the 2026-08-20 hardening because the Settings API returned every API key in plaintext. Decision: turn the login off (server binds to 127.0.0.1 only) and close the actual hole instead.

- `.env`: `ADMIN_AUTH_ENABLED=false`. Web server started with `nohup .venv/bin/python webui.py` (localhost:8000, web + API only, no in-app scheduler so it does not double the systemd timers). Not yet a systemd unit.
- `src/services/system_config_service.py` `get_config`: every field whose schema says `is_sensitive` (43 registered keys plus any unregistered KEY/TOKEN/SECRET/PASSWORD key) is now returned as the mask token with `is_masked=true`, not just the four Hermes/telemetry keys. No write-path change was needed: update, validate, notification test and backend-status preview already treat the mask token as "unchanged" for sensitive fields.
- Verification: 279 system-config service/API tests green after retargeting the three upstream tests that asserted plaintext secrets, plus a new round-trip test (save with the mask token keeps the stored secret). Live scan of 13 GET endpoints on the running server for the real Anthropic / Tavily / Telegram / Alpaca values: none present; `/config/export` returns 403 while auth is off (upstream gate unchanged).
- Trade-off: with auth off, anything running on this machine can change settings or press Close-all. Re-enable `ADMIN_AUTH_ENABLED` before ever exposing the port on the LAN.

## 2026-09-04 — Trading page: numbers only, plus a bot P&L curve

User: "get rid of all the fluff … I only need the numbers" and "a pnl graph of the bot, not of the entire account".

- Page stripped to a status chip row (chips only when something needs attention), seven KPI tiles (Total / Day / Unrealized / Realized P&L, Equity, Cash, Exposure) with a single numeric sub-line each, the P&L chart, and the three tables. Eyebrow, descriptions, subtitles, hints, icons and the account/bot/shorts badges are gone; the shell header shows "Trading — Paper account".
- **Bot P&L curve**: broker `portfolio_history_since(day)` returns Alpaca daily equity from the anchor day plus `base_value`; the service emits `pnl_history` = `equity − base_value` per trading day with today's point replaced by live equity. Same anchor as the Total P&L tile, so the curve is strictly the bot's result (the account's −$2.3k pre-bot drawdown is excluded). Rendered with recharts (single series, zero reference line, crosshair tooltip with date / P&L / equity, colour by sign of the latest value).
- Verification: 73 backend tests (2 new: curve points and live-point replacement), eslint / tsc clean, page tests updated (chart presence, KPI values, empty-history state), build OK, server restarted; live page rendered with real data (curve from 2026-08-22).

## 2026-09-04 — Dashboard restarted; runbook added

- Web server restarted by hand (stop + `nohup .venv/bin/python webui.py`), health and `/api/v1/paper-trading/dashboard` verified. Startup / stop / restart instructions now live in the **Runbook** section at the top of this log so they are not buried in dated entries.

## 2026-09-07 — Why the account is down $214, and the ATR stop floor

User: "how have we lost over 200 bucks?" — then "from what you've learnt, can you improve the system".

**Diagnosis** (Alpaca fills + `paper_trades` + the stored report context): −$213.83 since the 8/20 anchor = GOOGL −$93 open, BKR stop −$76 (9/4), MSFT stop −$45 (9/2), NVDA +$0.38 (the 9/1 gap flaw), AAPL +$0.06 (manual test). Both real losses were stops placed *inside one day's average range*: BKR's stop sat 0.56 ATR below entry and was hit 90 min after the fill; MSFT's 0.61 ATR, hit 2 h later. Root cause is a semantic mismatch, not a direction call: the analyst writes stops as closing conditions ("a confirmed close below MA10") while the bracket leg fires on a tick — and in every one of those reports the system's own ATR reference levels had already rated the plan `poor_risk_reward`; the analyst tightened the stop to manufacture an R:R above the gate.

- **`PAPER_TRADING_STOP_MIN_ATR_MULT`** (default 1.0, `paper_trading_service.py`): a new entry's stop is pushed out to at least that many ATR(14) from the planned entry before the plan gate, chase gate and sizing run (shorts mirrored, buy-stop above). Dollar risk unchanged, share count drops; plans whose target cannot pay for a survivable stop now fail the existing R:R gates instead of executing on a paper-thin stop. ATR from the signal's report (`context_snapshot.enhanced_context.computed_trade_levels.atr`, i.e. what the analyst was shown), else Wilder ATR14 from broker daily bars (`_wilder_atr_from_bars`, same recursion as the analyzer); neither available → plan stop used, `extra.atr=null`. Widened entries carry `extra.stop_widened`, the reason text and the Paper Orders line say so. Chase re-flooring was considered and dropped: chasing only moves the entry away from the stop.
- **Replay against the real entries with their stored ATR**: NVDA 0.36 ATR → rejected (R 0.96 at the chased price), MSFT 0.61 → rejected (plan R 1.46), BKR 0.56 → rejected (R 1.09), LPG 0.67 → rejected at the chase (R 1.12; the resting order is still unfilled), GOOGL 1.38 ATR (`good`) → untouched. The floor would have blocked every trade that lost money and kept the only one the system rated good.
- **Learning loop cadence**: post-mortems now also run in the intraday `--manage-positions` pass while the market is open (`_run_trade_postmortems` shared with the daily run). Motivation: BKR was stopped Friday and still had no review Monday night — the daily run only reviews at its *end*, and on Labor Day it skipped entirely, so the lesson would have missed Tuesday's analysis too.
- **Verification**: 98 tests green across the paper-trading, post-mortem, dashboard and API suites (10 new: Wilder ATR, BKR replay rejection, widening + sizing, untouched when beyond the floor, chase gated on the widened stop, chase rejection reason, report ATR preferred over bars, no-ATR passthrough, `0` disables, short mirror, env parsing); main-related suites green; `py_compile` clean. Not exercised live: the next real entry decision (first chance is the 9/8 09:32 ET pass).
- **Still open**: GOOGL is 1.6 % underwater for 12 sessions with the analyst downgraded to `watch` since 9/4 — there is no time stop and a downgrade does nothing to a held position; a user decision. LPG's resting limit will be TTL-cancelled on the 9/8 run.

## Open decisions

- **Real watchlist** — `STOCK_LIST` is still the temporary sample (matters less now that discovery feeds the run).
- **Strategy kill decisions** — the scorecard (watchlist/discovery × long/short) now accumulates; killing a bucket stays a user decision once samples are meaningful.
- **Sizing** — raise `PAPER_TRADING_MAX_POSITION_PCT` if the full $500 risk per trade is wanted on a $30k account (implies ~40 % of equity in one name at current prices).
- **Account C** — regenerate keys if it should be eligible.
