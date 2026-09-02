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
- standalone via `python main.py --manage-positions` — broker + local signal store only, no data fetch or LLM. Meant for an intraday timer (e.g. every 15 minutes during US market hours). Each pass first executes any fresh (≤36 h) decision signals that are still unexecuted — this is where deferred entries are placed — then ratchets stops. It does nothing while the market is closed and only notifies when something actually happened.

Short positions are managed with the mirror-image ratchet: the buy-stop above the price ratchets **down** (break-even = entry × (1 − buffer), trail = price + `PAPER_TRADING_TRAIL_R` × R, floor `PAPER_TRADING_STOP_MIN_GAP_PCT` % above the latest price) and is never loosened.

Each stop move is persisted to `paper_trades` with `action = trail_stop` (old/new stop, price, R gain, locked-in $ in `raw_json`).

## Sizing

`qty = floor(PAPER_TRADING_RISK_PER_TRADE_USD / (entry − stop))`, further capped by `PAPER_TRADING_MAX_POSITION_PCT` of equity and by `PAPER_TRADING_MAX_POSITIONS`. Whole shares only (brackets require it).

## Entry filters

A new entry is skipped when the plan failed geometry validation (`plan_check.valid == false`), reward:risk is below `PAPER_TRADING_MIN_R_MULTIPLE`, earnings fall within the signal horizon, the signal is older than 36 hours, or the symbol is not a US listing.

## Safety

- Every client refuses account numbers that do not start with `PA` (paper).
- Kill switch: create the file at `PAPER_TRADING_KILL_SWITCH` (default `data/paper_trading.STOP`) to stop all submissions, including stop replacements.
- `PAPER_TRADING_DRY_RUN=true` logs and records intended orders without calling the API.
- Every decision (submitted, skipped with reason, error) is written to the `paper_trades` table, linked to the decision-signal id, and summarised in the notification channel as a "Paper Orders" message.

## Configuration

See the `Paper trading` block in `.env.example`. Credentials use the `ALPACA_<LABEL>_KEY_ID` / `ALPACA_<LABEL>_SECRET_KEY` convention so several paper accounts can coexist; `PAPER_TRADING_ACCOUNT=auto` picks the reachable one with the most equity.
