# Reading Guide — how to understand the bot's reports

*For a plain read of the trading system's performance. The numbers live in
`runs/journal-*.md` (daily) and `runs/PERFORMANCE.md` (cumulative).
Both files now open with a "plain English" summary — if you only have 30 seconds,
read that part and stop.*

---

## The 3 files worth reading

| File | What it is | When to read |
|------|-----------|--------------|
| `runs/PERFORMANCE.md` | The running scoreboard since the beginning. **Read the "In plain terms" line first.** | Weekly, or whenever you're curious |
| `runs/journal-YYYY-MM-DD.md` | One day's story: bottom-line summary, trades, what it skipped and why | Any day you want detail |
| `runs/signals-*.json` | Raw machine log of every signal and decision | Never — it's for auditors and the bot itself |

## The plain-English summary (top of every file)

Every daily journal starts with "**Read this first (plain English)**". It answers, in
sentences:

- Did it trade today, and did it make or lose money (after real fees)?
- Which trade was best/worst, and *why it closed* (safety stop / profit target / reverse signal)
- Which opportunities it skipped, and the reason in words
- What it's still holding
- The running totals since the beginning and over the last 7 days

## Glossary — every term in one line

| Term | Plain meaning |
|------|---------------|
| **Equity** | Everything you own: cash + the current value of open positions |
| **Cash** | Money not currently invested |
| **Buying power** | How much it *could* spend — for us it equals cash (margin is switched off) |
| **Baseline (last equity)** | The value at the last official market close — "daily change" is measured from here |
| **Realized** | Profit/loss already **banked** by closing a position. This is real. |
| **Unrealized** | Profit/loss on positions **still open** — it can still move. Paper until closed. |
| **FIFO** | When several buys/sells overlap, the oldest buy is matched with the first sell. It's the standard way to compute honest profit. |
| **Gross vs Net** | Gross = raw price difference. Net = **after fees** (the number that matters) |
| **Fees** | Crypto costs ~0.25% per side (≈0.5% per round trip, charged in coin). Equities are commission-free on Alpaca. |
| **Win rate** | Of all closed trades, what % made money |
| **Expectancy** | The average result per trade, in dollars. Positive = the strategy earns per trade on average |
| **Profit factor** | For every $1 lost, how many dollars were won. Above 1.0 = profitable overall |
| **RRR (reward-to-risk)** | How much it can win compared to what it risks if the stop is hit. The bot requires at least 0.5. |
| **Confluence** | Confirmation from independent signals (trend, support/resistance, moving averages). More = higher quality setup |
| **Confidence multiplier** | How much bigger the bet is on a strong setup (up to 3× the base risk) |
| **Stop-loss / Take-profit** | Automatic exit if the trade goes −X% (damage control) or +Y% (locking in the win) |
| **Slippage** | The small difference between the price when the signal fired and the price the order actually got. Unavoidable; watch the trend. |
| **Daily cap** | A safety limit: at most 3 new positions per symbol per 24h — prevents over-betting on one hot streak |
| **Signal funnel** | The count of opportunities seen vs taken vs skipped, with the reason for each skip |
| **Shrinkage** | The tiny amount "lost" to a fee taken in coin form (e.g. you buy 0.00516 BTC, receive 0.00515) |

## How to read the scoreboard in 3 steps

1. Open `runs/PERFORMANCE.md` and read only the **"In plain terms"** line.
2. If the trade count is under ~30, remind yourself: **this is not a verdict yet** —
   small samples swing wildly. Judge nothing before a week of trades.
3. If something surprises you (a string of losses, fees eating profits, a symbol
   dominating), ask the bot's operator (me) to pull the data and explain the *why*.

## The honest expectations

- **Red days and green days are both normal.** A strategy can be profitable
  *overall* while losing on any given day — that's why we track cumulative numbers.
- The three numbers that decide "is this worth running": **net P&L**, **win rate**,
  and **expectancy per trade** — all in `PERFORMANCE.md`, all net of fees.
- The bot records everything deliberately. If a number ever looks wrong, we can
  trace it to the exact fill — that's what the detailed tables are for.
