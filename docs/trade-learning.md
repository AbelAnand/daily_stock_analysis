# Trade Learning Loop (post-mortems)

Human traders review their trades and adjust; autonomous systems usually don't. This opt-in loop (`TRADE_POSTMORTEM_ENABLED`) closes that gap — deliberately bounded so it cannot spiral.

## The loop

1. **Reconstruct** — every recorded entry order is tracked to its conclusion (bracket stop fired / target hit / direct close / never filled) via the broker's fill history: entry & exit fills, realized P&L, realized R against the planned risk. One review per entry order, ever.
2. **Review** — an LLM (`TRADE_POSTMORTEM_MODEL`) sees the original thesis + plan, the fills, and the daily bars after entry, and classifies the **process** (not the outcome): `bad_thesis` / `bad_entry` / `bad_stop_placement` / `bad_target` / `execution_flaw` / `variance` / `good_process`. It may distill at most ONE generalizable lesson — and is told to prefer no lesson over a weak one (a clean stop-out is `variance`, not a lesson).
3. **Feed back** — the newest `TRADE_LESSONS_IN_PROMPT` lessons are injected into the analyst prompt ("Lessons from this system's own closed trades"), explicitly as calibration, never as a direction veto.
4. **Escalate, don't self-modify** — `execution_flaw` findings (the system did something mechanically wrong) are flagged 🔧 in the Telegram digest for an engineering fix and are **never** injected into the prompt. The system does not tune its own parameters.

Results land in the `trade_postmortems` table and in the daily "🧠 Trade Review" digest section, together with a **strategy scorecard** by source × direction (watchlist/discovery × long/short: wins, net P&L, avg R) — the evidence for killing a strategy that doesn't earn its keep. Killing one is the operator's call.

## Cadence and cost

Runs at the end of the daily pre-market run, so a trade closed today is reviewed tomorrow morning and its lesson applies from the next analysis onward. One LLM call per newly closed trade (unfilled entries are recorded without a call); at a handful of trades a week this is cents.

## Honest limits

Small samples make plausible-sounding lessons out of noise — that is why lessons carry their category and the trade's realized R, why `variance` produces nothing, and why injection is capped at a handful of recent lessons rather than an ever-growing rulebook. The scorecard is directional evidence, not statistics, until trade counts grow.
