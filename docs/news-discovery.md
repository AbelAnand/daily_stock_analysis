# News-Driven Discovery (US)

Opt-in front-end for the daily run: instead of analyzing only the fixed `STOCK_LIST`, the system scans the overnight tape and merges symbols with a **concrete, fresh catalyst** into the run. Discovery only nominates — every candidate then goes through the full analysis pipeline, signal extraction, R:R gates and paper execution exactly like a watchlist name.

## How it works

1. **Scan** (Alpaca data APIs, reusing the `ALPACA_<X>` paper credentials): news headlines from the last `DISCOVERY_NEWS_HOURS` (Benzinga feed, symbols attached), top-20 market movers (gainers + losers), top-20 most-active.
2. **Triage** (one cheap LLM call, `DISCOVERY_TRIAGE_MODEL`): pick up to `DISCOVERY_MAX_CANDIDATES` symbols with a tradeable catalyst, each with a direction hint (long/short), conviction 1-5 and a one-line catalyst. Conviction below `DISCOVERY_MIN_CONVICTION` is dropped.
3. **Vet**: each pick must be an active, tradable US equity on Alpaca priced above `DISCOVERY_MIN_PRICE`.
4. **Merge**: surviving symbols are appended to the configured stock list for that run (config-list runs only — never for explicit `--stocks` or portfolio runs). Discovery failure of any kind leaves the configured list untouched.

Every run is persisted to the `news_discovery_runs` table (raw inputs, shortlist, errors).

## Cost

One triage call (~50 headlines) is a fraction of a cent to a few cents; each merged candidate costs one full stock analysis (~$0.16-0.23 with Sonnet). With the default cap of 6: **roughly $1-1.50/day** on top of the watchlist.

## Notes

- The direction hint is for the log only; the analyst pipeline forms its own view (a bearish conclusion with a valid `short_plan` can become a short — see docs/paper-trading.md).
- The triage prompt demands specific, dated catalysts and allows an empty list; expect zero candidates on quiet days.
