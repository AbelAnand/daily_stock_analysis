# Paper Trading (Alpaca)

Opt-in execution layer that turns the daily decision signals into orders on an **Alpaca paper account**. It runs automatically at the end of `python main.py` (after outcome scoring) when `PAPER_TRADING_ENABLED=true`.

## What it does

| Signal action | Order |
|---|---|
| `buy` / `add` | GTC **bracket** order: limit entry at the top of the entry range, attached stop-loss and take-profit. Skipped if already holding the symbol or an order is pending. |
| `reduce` | Market sell of half the position. |
| `sell` | Close the position (cancels bracket legs first). |
| `hold` / `watch` / `avoid` | No order. |

Unfilled limit entries older than `PAPER_TRADING_ENTRY_TTL_DAYS` are cancelled on the next run.

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
