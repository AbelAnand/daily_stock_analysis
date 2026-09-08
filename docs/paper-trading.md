# Paper Trading (Alpaca)

Opt-in execution layer that turns the daily decision signals into orders on an **Alpaca paper account**. It runs automatically at the end of `python main.py` (after outcome scoring) when `PAPER_TRADING_ENABLED=true`.

## What it does

| Signal action | Order |
|---|---|
| `buy` / `add` | GTC **bracket** order: limit entry with attached stop-loss and take-profit. The limit sits at the top of the entry range; if the market already trades above it, the entry **chases** — a marketable limit at the latest price + `PAPER_TRADING_CHASE_PCT` % — as long as the reward:risk recomputed at that price still clears `PAPER_TRADING_CHASE_MIN_R` (the plan itself must clear `PAPER_TRADING_MIN_R_MULTIPLE` at the planned entry). Skipped if already holding the symbol or an order is pending. |
| `reduce` | Market sell of half the position. |
| `sell` | Close the position (cancels bracket legs first). |
| `sell` / `avoid` with an `ev_contract.short_plan` and no position | **Short** GTC bracket (requires `PAPER_TRADING_ALLOW_SHORT=true`): sell-short limit entry with a buy-stop *above* and a cover target *below*. Same gates mirrored: plan must clear `PAPER_TRADING_MIN_R_MULTIPLE` at the planned entry (R = (entry − target) / (stop − entry)), chases *down* with a marketable limit within `PAPER_TRADING_CHASE_MIN_R`, and the asset must be shortable + easy-to-borrow at Alpaca. |
| `buy` / `add` while short | Cover (close the short position). |
| `hold` / `watch` / `avoid` (no short plan) | No order. |

Unfilled limit entries older than `PAPER_TRADING_ENTRY_TTL_DAYS` are cancelled on the next run (short entries are matched by recorded order id, so protective sell legs are never touched); an entry cancelled this way no longer blocks a fresh signal for the same symbol in the same run. Sizing and the R:R filter always use the effective (possibly chased) entry price; if the latest quote is unavailable the planned limit is used unchanged.

## When entries are placed (gap protection)

New entries (long and short) are **never submitted while the market is closed**. A marketable order priced off a pre-market quote can fill through its own stop on an opening gap (seen live: NVDA 2026-09-01 filled below its stop at the open and was stopped out 28 s later). The pre-market run only *defers* entries; the intraday pass (`--manage-positions`, first slot 09:32 ET) re-decides them against live prices, so a gap re-runs the R:R gates instead of filling blind. Exits are always allowed — they reduce risk wherever they queue. At most **one entry per symbol per day**: after a same-day stop-out or TTL cancel the symbol is not re-entered until the next session.

## Managing open positions (stop ratchet)

Once filled, a position is not left to its static bracket. `PaperTradingService.manage_positions()` re-checks every open bracket position and **raises the stop-loss leg in place** (Alpaca order replace; the take-profit leg is untouched). With `R = entry − initial stop` (from the recorded entry order, so a ratcheted stop does not shrink R):

| Unrealized gain | Stop |
|---|---|
| `< PAPER_TRADING_BREAKEVEN_TRIGGER_R` R (default 1R) | untouched — the original plan stop |
| `≥` trigger | `max(entry × (1 + PAPER_TRADING_BREAKEVEN_BUFFER_PCT %), price − PAPER_TRADING_TRAIL_R × R)`, capped `PAPER_TRADING_STOP_MIN_GAP_PCT` % below the latest price |

So at +1R the stop moves to just above entry (a winner can no longer turn into a loser), at +2R it sits at +1R, and so on. The stop is **never lowered**. Positions without a live bracket stop are reported as ⚠️ unprotected.

Where it runs:

- at the end of every daily run, and
- standalone via `python main.py --manage-positions` — broker + local signal store only, no data fetch; the only LLM call is a post-mortem of a trade that just closed. Meant for an intraday timer (e.g. every 15 minutes during US market hours). Each pass first executes any fresh (≤36 h) decision signals that are still unexecuted — this is where deferred entries are placed — then ratchets stops, then runs the [trade learning loop](trade-learning.md) over trades closed since the last pass. It does nothing while the market is closed and only notifies when something actually happened.

Short positions are managed with the mirror-image ratchet: the buy-stop above the price ratchets **down** (break-even = entry × (1 − buffer), trail = price + `PAPER_TRADING_TRAIL_R` × R, floor `PAPER_TRADING_STOP_MIN_GAP_PCT` % above the latest price) and is never loosened.

Each stop move is persisted to `paper_trades` with `action = trail_stop` (old/new stop, price, R gain, locked-in $ in `raw_json`).

## Sizing

`qty = floor(PAPER_TRADING_RISK_PER_TRADE_USD / (entry − stop))`, further capped by `PAPER_TRADING_MAX_POSITION_PCT` of equity and by `PAPER_TRADING_MAX_POSITIONS`. Whole shares only (brackets require it).

## Entry filters

A new entry is skipped when the plan failed geometry validation (`plan_check.valid == false`), reward:risk is below `PAPER_TRADING_MIN_R_MULTIPLE`, earnings fall within the signal horizon, the signal is older than 36 hours, or the symbol is not a US listing.

### Stop distance floor (ATR)

The analyst's stop is often a *closing* condition ("a confirmed close below MA10"), but the bracket stop leg fires on a tick. Executed literally, a stop inside one day's average range is stopped out by noise: BKR 2026-09-04 (stop 0.56 ATR below the fill, hit 90 minutes later, −$76) and MSFT 2026-09-02 (0.7 ATR, −$45) were the account's only real losses.

So before the R:R gates run, the stop of a new entry is pushed out to at least `PAPER_TRADING_STOP_MIN_ATR_MULT` × ATR(14) (default 1.0) below the planned entry (above it for shorts). A stop already beyond the floor is untouched, and chasing only moves the entry further from the stop, so the plan gate, the chase gate and sizing all run on the widened geometry. Consequences:

- **Dollar risk is unchanged, share count drops**: sizing uses the widened stop, so the same `PAPER_TRADING_RISK_PER_TRADE_USD` buys fewer shares.
- **Plans whose target cannot pay for a survivable stop are rejected** by the existing R:R gates instead of being executed on a paper-thin stop. BKR's plan (target +2.7 %, stop 1.4 % away, R:R 1.93) becomes R:R 1.09 at a one-ATR stop and is skipped, with the widening spelled out in the skip reason.
- Short entries mirror it: the buy-stop sits at least the floor *above* the entry.

ATR comes from the signal's source report (the same `computed_trade_levels.atr` the analyst was shown), else it is recomputed from the broker's daily bars (Wilder ATR14, same formula). When neither is available the plan stop is used as-is and `atr` is recorded as null on the decision. Widened entries carry `extra.stop_widened` (`plan_stop`, `stop`, `atr`, `min_atr_mult`) in `paper_trades.raw_json`, the reason text notes it, and the Paper Orders line shows "stop widened from <plan stop>". `PAPER_TRADING_STOP_MIN_ATR_MULT=0` disables the floor.

## Safety

- Every client refuses account numbers that do not start with `PA` (paper).
- Kill switch: create the file at `PAPER_TRADING_KILL_SWITCH` (default `data/paper_trading.STOP`) to stop all submissions, including stop replacements.
- `PAPER_TRADING_DRY_RUN=true` logs and records intended orders without calling the API.
- Every decision (submitted, skipped with reason, error) is written to the `paper_trades` table, linked to the decision-signal id, and summarised in the notification channel as a "Paper Orders" message.

## Web dashboard (Trading desk)

The Web UI has a **Trading** page (`/trading`) for the paper account, backed by `GET /api/v1/paper-trading/dashboard` (admin-auth protected like every `/api/v1` route; the broker connection is cached on the app state for the polling UI, which refreshes every 15 s while the tab is visible).

- **Total P&L** is the *bot's* P&L, not the account's lifetime P&L: `live equity − equity at the start of the day of the bot's first real entry` (Alpaca portfolio-history `base_value`). Anything the account did before the bot took over is excluded, and manual closes move the number immediately. If the history call fails it degrades to `realized (post-mortem loop) + unrealized (open positions)`; the payload's `total_pnl_basis` says which one is shown and the tile hint labels it.
- **Day P&L** is `equity − last_equity` (previous close). Unrealized P&L, cash, buying power and gross exposure come straight from the broker.
- **Bot P&L chart**: `pnl_history` is the same anchor applied per trading day (`daily equity − baseline`, from Alpaca portfolio history), with the last point replaced by live equity, so the curve shows only what the bot has made or lost since its first trade.
- The page is deliberately numbers-only: KPI tiles, the P&L curve, and the positions / open orders / activity tables, with status chips shown only when something needs attention (market closed, dry run, kill switch, bot off).
- **Positions** show the live protective stop and target legs (the bracket legs the ratchet moves), the stop recorded at entry, and the current R multiple against that initial risk. A position without a live stop leg is flagged.
- **Close** (per position) and **Close all positions** call `POST /api/v1/paper-trading/positions/{symbol}/close` and `POST /api/v1/paper-trading/positions/close-all`. Both use the same broker path as a `sell` signal (bracket legs cancelled first, retry while the OCO hold releases) and return per-symbol results, so a partial failure of close-all is reported instead of hidden. Close-all requires typing `CLOSE ALL` in the dialog.
- Every manual close is written to `paper_trades` as `action=manual_close` (`side=sell` for longs, `buy_to_cover` for shorts) with status `submitted` / `error` / `dry_run`, and the post-mortem loop reviews it like any other exit. Closing is risk-reducing, so it stays available while the kill switch is set; `PAPER_TRADING_DRY_RUN=true` records the close without sending it.
- Without `ALPACA_<LABEL>_*` credentials (or when no account is reachable) the endpoint returns `503 paper_trading_unavailable` and the page shows a setup hint.

## Configuration

See the `Paper trading` block in `.env.example`. Credentials use the `ALPACA_<LABEL>_KEY_ID` / `ALPACA_<LABEL>_SECRET_KEY` convention so several paper accounts can coexist; `PAPER_TRADING_ACCOUNT=auto` picks the reachable one with the most equity.
