# Paper Trading (Alpaca)

Opt-in execution layer that turns the daily decision signals into orders on an **Alpaca paper account**. It runs automatically at the end of `python main.py` (after outcome scoring) when `PAPER_TRADING_ENABLED=true`.

## What it does

| Signal action | Order |
|---|---|
| `buy` / `add` | GTC **bracket** order: limit entry with attached stop-loss and take-profit. The limit sits at the top of the entry range; if the market already trades above it, the entry **chases** — a marketable limit at the latest price + `PAPER_TRADING_CHASE_PCT` % — as long as the reward:risk recomputed at that price still clears `PAPER_TRADING_CHASE_MIN_R` (the plan itself must clear `PAPER_TRADING_MIN_R_MULTIPLE` at the planned entry). Skipped if already holding the symbol or an order is pending. |
| `reduce` | Market sell of half the position. |
| `sell` | Close the position (cancels bracket legs first). |
| `hold` / `watch` / `avoid` | No order. |

Unfilled limit entries older than `PAPER_TRADING_ENTRY_TTL_DAYS` are cancelled on the next run; an entry cancelled this way no longer blocks a fresh signal for the same symbol in the same run. Sizing and the R:R filter always use the effective (possibly chased) entry price; if the latest quote is unavailable the planned limit is used unchanged.

## Sizing

`qty = floor(PAPER_TRADING_RISK_PER_TRADE_USD / (entry − stop))`, further capped by `PAPER_TRADING_MAX_POSITION_PCT` of equity and by `PAPER_TRADING_MAX_POSITIONS`. Whole shares only (brackets require it).

## Entry filters

A new entry is skipped when the plan failed geometry validation (`plan_check.valid == false`), reward:risk is below `PAPER_TRADING_MIN_R_MULTIPLE`, earnings fall within the signal horizon, the signal is older than 36 hours, or the symbol is not a US listing.

## Safety

- Every client refuses account numbers that do not start with `PA` (paper).
- Kill switch: create the file at `PAPER_TRADING_KILL_SWITCH` (default `data/paper_trading.STOP`) to stop all submissions.
- `PAPER_TRADING_DRY_RUN=true` logs and records intended orders without calling the API.
- Every decision (submitted, skipped with reason, error) is written to the `paper_trades` table, linked to the decision-signal id, and summarised in the notification channel as a "Paper Orders" message.

## Configuration

See the `Paper trading` block in `.env.example`. Credentials use the `ALPACA_<LABEL>_KEY_ID` / `ALPACA_<LABEL>_SECRET_KEY` convention so several paper accounts can coexist; `PAPER_TRADING_ACCOUNT=auto` picks the reachable one with the most equity.
