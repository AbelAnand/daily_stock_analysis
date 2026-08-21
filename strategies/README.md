# Trading Strategies

This directory holds **natural-language trading strategy files** (YAML format). The system automatically loads every `.yaml` file in this directory on startup.

In user-facing docs we still call these capabilities "strategies"; in code, config, and API fields they're uniformly named `skill` — think of it as a "reusable strategy capability bundle."

## Writing a custom strategy (Strategy Skill)

Just create a `.yaml` file describing your trading strategy in natural language — **no code required**.

### Minimal template

```yaml
name: my_strategy          # Unique id (English, underscore-separated)
display_name: My Strategy  # Display name
description: A short description of what the strategy is for

instructions: |
  Your strategy description...
  Describe entry/exit criteria and other rules in natural language.
  You can reference tool names (e.g. get_daily_history, analyze_trend) to guide which data the AI uses.
```

### Full template

```yaml
name: my_strategy
display_name: My Strategy
description: A short description of the market scenario this strategy targets

# Strategy category: trend, pattern, reversal, framework
category: trend

# Related core trading rule numbers (1-7), optional
core_rules: [1, 2]

# List of tools this strategy needs, optional
# Available tools: get_daily_history, analyze_trend, get_realtime_quote,
#                   get_sector_rankings, search_stock_news, get_stock_info
required_tools:
  - get_daily_history
  - analyze_trend

# Optional aliases (used for natural-language skill selection, e.g. via /ask)
aliases: [my playbook, my model]

# The metadata below drives default behavior (optional)
# default_active: whether this belongs to the default-active skill set
# default_router: whether this belongs to the router fallback skill set
# default_priority: default display/sort priority — lower numbers come first
# market_regimes: market-regime tags this skill is best suited for
default_active: true
default_router: false
default_priority: 100
market_regimes: [trending_up]

# Detailed strategy instructions (natural language, Markdown supported)
instructions: |
  **My Strategy Name**

  Criteria:

  1. **Condition one**:
     - Use `analyze_trend` to check the moving-average alignment.
     - Describe the trend characteristics you expect to see...

  2. **Condition two**:
     - Describe the volume requirements...

  Suggested score adjustments:
  - The suggested sentiment_score adjustment when conditions are met
  - Note the strategy name in `buy_reason`
```

### Core trading rules reference

| # | Rule |
|---|------|
| 1 | Strict entry: only consider entering when bias < 5% |
| 2 | Trend trading: bullish alignment, MA5 > MA10 > MA20 |
| 3 | Efficiency first: volume confirms trend validity |
| 4 | Buy-point preference: prefer pullbacks that hold at moving-average support |
| 5 | Risk screening: bad news is a hard veto |
| 6 | Volume/price alignment: volume confirms price movement |
| 7 | Relax for strong trending stocks: dragon-head stocks can tolerate a somewhat looser standard |

## Custom strategy directory

Besides this directory (the built-in strategies), you can also point to an additional custom strategy directory via an environment variable:

```env
AGENT_SKILL_DIR=./my_skills
```

The system loads both the built-in and custom strategies. If names collide, the custom strategy overrides the built-in one.

The environment variable name is still `AGENT_SKILL_DIR` — that's the unified internal config entry point; at the product level it still means "custom strategy directory."
