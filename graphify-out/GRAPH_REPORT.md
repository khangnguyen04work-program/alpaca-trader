# Graph Report - alpaca-trader  (2026-10-16)

## Corpus Check
- 39 files · ~63,602 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 2 file(s) not represented in the graph (top: (none) 2)

## Summary
- 61 nodes · 114 edges · 10 communities (6 shown, 4 thin omitted)
- Extraction: 100% EXTRACTED · 0% INFERRED · 0% AMBIGUOUS
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9

## God Nodes (most connected - your core abstractions)
1. `analyze_and_trade()` - 23 edges
2. `_request()` - 10 edges
3. `rest_call()` - 9 edges
4. `run_alpaca()` - 8 edges
5. `log()` - 6 edges
6. `main()` - 6 edges
7. `determine_market_regime()` - 5 edges
8. `get_account()` - 4 edges
9. `fetch_bars()` - 4 edges
10. `place_order()` - 4 edges

## Surprising Connections (you probably didn't know these)
- `cancel_order()` --calls--> `_request()`  [EXTRACTED]
  bot/rest_client.py → bot/rest_client.py  _Bridges community 5 → community 4_
- `get_account()` --calls--> `_request()`  [EXTRACTED]
  bot/rest_client.py → bot/rest_client.py  _Bridges community 5 → community 7_
- `rest_call()` --calls--> `get_account()`  [EXTRACTED]
  bot/rest_client.py → bot/rest_client.py  _Bridges community 7 → community 4_
- `analyze_and_trade()` --calls--> `run_alpaca()`  [EXTRACTED]
  bot/trader.py → bot/trader.py  _Bridges community 1 → community 2_
- `analyze_and_trade()` --calls--> `classify_candle()`  [EXTRACTED]
  bot/trader.py → bot/trader.py  _Bridges community 6 → community 2_

## Import Cycles
- None detected.

## Communities (10 total, 4 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.18
Nodes (3): check_confluence(), compute_bollinger_bands(), estimate_stop_loss()

### Community 1 - "Community 1"
Cohesion: 0.24
Nodes (9): fetch_bars(), is_after_market_close(), is_market_session(), log(), main(), place_order(), _rest_call(), run_alpaca() (+1 more)

### Community 2 - "Community 2"
Cohesion: 0.20
Nodes (9): analyze_and_trade(), compute_sma(), detect_doji(), detect_engulfing_bar(), detect_inside_bar(), detect_morning_evening_star(), detect_pin_bar(), estimate_profit_target() (+1 more)

### Community 4 - "Community 4"
Cohesion: 0.33
Nodes (5): cancel_order(), get_bars(), get_orders(), rest_call(), submit_order()

### Community 5 - "Community 5"
Cohesion: 0.40
Nodes (4): get_clock(), get_positions(), _headers(), _request()

### Community 6 - "Community 6"
Cohesion: 0.50
Nodes (3): classify_candle(), determine_market_regime(), identify_support_resistance_levels()

## Knowledge Gaps
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `analyze_and_trade()` connect `Community 2` to `Community 0`, `Community 1`, `Community 6`, `Community 8`, `Community 9`?**
  _High betweenness centrality (0.119) - this node is a cross-community bridge._
- **Why does `rest_call()` connect `Community 4` to `Community 3`, `Community 5`, `Community 7`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Why does `run_alpaca()` connect `Community 1` to `Community 0`, `Community 2`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._