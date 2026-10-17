# Alpaca Autonomous Trader — Complete System Handbook

*For 3-year unattended paper trading on GitHub Actions*
*Based on The Candlestick Trading Bible by Sanyog Raut*

---

## 1. System Overview

**What this is:** A fully autonomous paper-trading bot that detects Japanese candlestick patterns, applies top-down multi-timeframe confluence analysis, and places buy-only cash trades during US equity market hours.

**Total files:** 4 code files, 34 documentation files
**Graph analysis:** 61 nodes, 114 edges, 10 functional communities

### Architecture — SINGLE BRAIN (GitHub Actions only)

> **Note:** Laptop systemd timers were disabled on 2026-10-16 to prevent
> duplicate orders. GitHub Actions is the single source of truth.
> The laptop retains `bin/alpaca` CLI + project folder for manual
> debugging, config edits, and pushing code updates only.

```
GITHUB ACTIONS (cloud) — the ONLY trading brain
    │  cron workflows:
    │  • trader (every 2 min, market hours)
    │  • journal (4:30 PM ET daily)
    │  • weekend analysis (every 15 min, Sat-Sun)
    │
    │  Runs 24/7 regardless of laptop state
    │  Uses: bot/rest_client.py (REST API)
    ▼
ALPACA PAPER API
    Account: $5,000 cash · Buy-only · No margin
```

### Decision Pipeline (inside trader.py)

```
trader.py ─── main() ─── analyze_and_trade(symbol)
    │                         │
    │                    ┌────┴────┐
    │                    │         │
    │              weekly bars  daily bars
    │              (1Week,     (1Day,
    │               90 days)    30 days)
    │                    │         │
    │                    └────┬────┘
    │                         │
    │              determine_market_regime()
    │              → trending/ranging/choppy
    │              → S/R levels
    │              → HTF trend direction
    │                         │
    │              15Min bars (entry TF)
    │                         │
    │              detect_pin_bar()
    │              detect_engulfing_bar()
    │              detect_inside_bar()
    │              detect_morning_evening_star()
    │              detect_doji()
    │                         │
    │              check_confluence()
    │              → HTF alignment (weekly trend)
    │              → S/R proximity
    │              → 8/21 MA proximity
    │              → Bollinger Bands
    │              → Trend alignment
    │                         │
    │              calc_confidence_mult()
    │              → pattern strength + confluence + RRR + HTF
    │              → 0.25x – 3.0x multiplier
    │                         │
    │              place_order() → Alpaca API (buy only, cash)
    │
    run_journal() ─── every 4:30 PM ET (journal-YYYY-MM-DD.md)
```

---

## 2. Strategies Implemented (from the book)

### 2.1 Candlestick Patterns

| Pattern | Detection Rule | Trade |
|---------|---------------|-------|
| **Bullish Pin Bar (Hammer)** | Body < 40% of range, lower shadow ≥ 2× body, in downtrend | Buy |
| **Bearish Pin Bar (Shooting Star)** | Body < 40% of range, upper shadow ≥ 2× body, in uptrend | Sell (blocked) |
| **Bullish Engulfing** | Bearish candle fully engulfed by larger bullish candle | Buy |
| **Bearish Engulfing** | Bullish candle fully engulfed by larger bearish candle | Sell (blocked) |
| **Morning Star** | Bearish → indecision → bullish (closes above first midpoint) | Buy |
| **Evening Star** | Bullish → indecision → bearish (closes below first midpoint) | Sell (blocked) |
| **Bullish Inside Bar (Harami)** | Baby candle inside mother range, breakout up | Buy |
| **Bearish Inside Bar (Harami)** | Baby inside mother, breakout down | Sell (blocked) |
| **Doji** | Open ≈ close within 5% of range | Analysis only |

⭐ **Cash-only rule:** The bot only places **BUY** orders. SELL orders are allowed only to exit positions already owned. Short selling is blocked.

### 2.2 Top-Down Multi-Timeframe Analysis

Per the book: *"If trading 4H, check weekly first, then daily. Trade only if higher TFs align."*

```
1Week ──→ Market regime (trending/ranging/choppy)
            ↓ Skip choppy markets entirely
1Day  ──→ Confirm trend, compute 8/21 SMA, S/R levels
            ↓ Merge S/R from both higher TFs
15Min ──→ Entry signals at key levels
            ↓ Only trade if HTF trend aligns
```

### 2.3 Confluence Factors

A signal must pass at least **1 confluence factor** (configurable). Factors checked:

1. **HTF trend alignment** — weekly trend matches entry TF trend
2. **Support/resistance proximity** — close within 1% of major level
3. **8/21 MA proximity** — close within 1.5% of moving average
4. **Bollinger Band touch** — rejection from upper/lower band
5. **Trend direction** — aligned with recent bar momentum
6. **HTF/daily alignment** — `trends_aligned` when both TFs agree

### 2.4 Money Management

| Setting | Value | Max Risk |
|---------|-------|----------|
| Base risk | 5% of cash | $250 on $5k |
| Confidence multiplier | 0.25× – 3.0× | **$750 max** |
| RRR threshold | 0.50 (any profit) | Filters losing trades |
| Stop loss | 25% beyond candle extreme | Tight but fair |

**Confidence multiplier formula:**
- Pattern strength: +0 to +1.0
- Confluence depth: +0.3 (3), +0.8 (4), +1.5 (5)
- RRR bonus: +0.3 (≥2.0), +1.0 (≥3.0)
- HTF alignment: +0.5
- **Capped at 3.0× per config**

### 2.5 Position Sizing

```
risk_amount = min(cash * 5% * confidence_mult, cash)
qty = max(1, int(risk_amount / current_price))
```

For a $350 stock with $5k cash:
- Weak signal (1×): risk $250 → 0 shares (min 1)
- Normal (2×): risk $500 → 1 share  
- Strong (3×): risk $750 → 2 shares

---

## 3. Target: $250/Day

With aggressive settings (5% base, 3.0× mult, 8 symbols, 15Min entry):

- **Scalp trades:** Multiple 1-2 share trades per day on 15Min signals
- **At 0.5 RRR:** Need ~$0.15 move on a $350 stock to profit
- **Per 15Min bar:** ~$0.90 avg range → stop at $0.23, profit target at $0.72
- **~40-80 signals/day across 8 symbols** → filtered by confluence → ~5-15 qualifying trades

**Risk:** Days with 5+ losing trades are possible. At 5% base risk, that's -12.5% day. The account has survived this — the confidence multiplier helps by betting bigger on stronger signals.

### 3.1 24/7 Crypto Trading (added 2026-10-16)

The bot also trades **BTC/USD and ETH/USD around the clock** — nights, weekends, holidays:

| Setting | Value |
|---------|-------|
| Symbols | BTC/USD, ETH/USD (config `crypto_symbols`) |
| Base risk | 3% (config `crypto_risk_pct`) — lower than equities due to volatility |
| Time-in-force | GTC (crypto never closes; `day` would kill overnight orders) |
| Quantity | **Fractional** (e.g. 0.002606 BTC — never force whole coins) |
| Schedule | `crypto.yml`: every 30 min, 24/7/365 |
| Data endpoint | `v1beta3/crypto/us/bars` (different from stocks) |
| Incomplete bars | Last bar dropped if still forming (crypto never closes, so the last bar is always live) |
| Market-hours gate | **Bypassed** — the `is_market_session()` check applies to equities only |
| All guards | Same: cash-only, daily cap 3/symbol, idempotent order IDs, confluence, RRR |

Run manually: `python3 bot/trader.py --crypto`

### 3.2 Position Exit Management (stop-loss / take-profit)

Every cycle (equity and crypto), BEFORE scanning for new trades, the bot checks all
open positions and closes any that breach the configured thresholds:

| Setting | Equities | Crypto |
|---------|----------|--------|
| Stop-loss | −2.5% (`stop_loss_pct`) | −3.5% (`crypto_stop_loss_pct`) |
| Take-profit | +4.0% (`take_profit_pct`) | +6.0% (`crypto_take_profit_pct`) |

- Crypto positions: evaluated 24/7. Equity positions: only while the market is open.
- Close = market order of the full position, verified by re-reading the position list
  (an API 204 is not proof of a fill).
- Signal-exits still work: a confirmed reverse pattern on an owned position closes it too.

**Concurrency:** all trading workflows share one concurrency group with
`cancel-in-progress: false` — runs QUEUE instead of cancelling. This stops a 2-minute
equity run from killing a crypto cycle mid-order, and vice versa.

**Signal-instance dedup:** each (symbol, pattern, bar) instance is traded at most once —
checked against both the daily signals file and Alpaca's live order history
(`client_order_id` match). Bars stay in the scan window for ~5 candles, so without this
the bot would re-attempt the same order every cycle (Alpaca 422 spam).

**Server-truth guards:** the daily entry cap (3 buys/symbol/rolling-24h) and instance
dedup read Alpaca's order history, not just repo files — GitHub runners only see
COMMITTED files, so file-only guards would silently reset between runs.

---

## 4. GitHub Actions Deployment

### Schedule

| Workflow | Trigger | What it does |
|----------|---------|-------------|
| `trader.yml` | Every 2 min during market hours (9:30AM–4PM ET), Mon–Fri + 10-min pre/after-hours | Analyzes + trades |
| `journal.yml` | 4:30PM ET, Mon–Fri | Writes daily retrospective |
| `weekend.yml` | Every 15 min, Sat–Sun 6AM–6PM ET | Market structure analysis |

### Secrets Required

| Name | Value | Where to get it |
|------|-------|-----------------|
| `APCA_ACCESS_TOKEN` | Bearer token | `grep access_token ~/.config/alpaca/profiles/paper.yaml` |

### Artifacts

- `runs/journal-YYYY-MM-DD.md` — daily post-market report
- `runs/signals-YYYY-MM-DD.json` — every signal detected (structured)
- **Committed back to the repo automatically** at 4:30 PM — permanent in git history
- Artifact copies also uploaded per-run (but expire after 90 days — repo copy is the permanent one)

**In 3 years, read journals from the repo:**
```bash
cd ~/Projects/alpaca-trader && git pull
ls runs/journal-*.md
```

**⚠️ Before pushing local changes:** the bot commits journals daily from the cloud, so your local repo is always behind. Always `git pull` first (`pull.rebase` is pre-configured on this repo). If a pull conflicts on today's `journal-*.md`, the **cloud version is canonical** — resolve with:
```bash
git checkout --theirs runs/journal-YYYY-MM-DD.md && git add runs/ && git rebase --continue && git push
```

### Cost

GitHub Actions free tier: **2,000 minutes/month** for private repos. This bot uses ~1,596 minutes/month (80%), with 404 minutes of headroom at the conservative 15s/run estimate.

---

## 5. Files (The 4 Code Files + Config)

| File | Role | Lines |
|------|------|-------|
| `bot/trader.py` | **Main engine** — analysis, trading, journal | 760+ |
| `bot/rest_client.py` | **REST API fallback** (no CLI binary needed) | 200+ |
| `config.json` | **Configuration** — symbols, risk, timeframes | 25 |
| `.github/workflows/trader.yml` | CI cron — trades every 2 min market hours + 10-min pre/after | 26 |
| `.github/workflows/journal.yml` | CI cron — journal at 4:30 PM | 24 |
| `.github/workflows/weekend.yml` | CI cron — weekend analysis every 15 min | 22 |
| `README.md` | Setup guide | — |

### Data Flow (from graph analysis)

**God nodes** (most connected — the core of the system):

1. **`analyze_and_trade()`** (23 edges) — Central orchestrator. Bridges 5 communities.
2. **`_request()`** (10 edges) — HTTP layer. All API calls pass through this.
3. **`rest_call()`** (9 edges) — CLI-style REST dispatcher.
4. **`run_alpaca()`** (8 edges) — CLI fallback → REST fallback.
5. **`main()`** (6 edges) — Entry point, decides market vs journal mode.
6. **`determine_market_regime()`** (5 edges) — Top-down weekly/daily structure.

---

## 6. Daily Journal Format

Every day at 4:30 PM ET, `run_journal()` writes:
```
runs/journal-YYYY-MM-DD.md

# Daily Trading Journal — 2026-10-16

## Account
  | Equity | $X,XXX | Cash | $X,XXX | Day P&L | $+XX.XX |

## Trades
  | Symbol | Side | Qty | P&L | Status |
  | NVDA   | buy  | 2   | $+12.34 | filled |
  | TSLA   | buy  | 1   | $-8.50  | filled |

## Open Orders (if any)
  | SPY | buy | 1 | accepted |

## Patterns Detected
  | Symbol | Pattern | Conf | Traded |
  | NVDA   | bullish_pin_bar | 3 | Yes |

## Mistakes
  - Signal skipped: bearish_inside_bar (cash-only: no shorts)

## Lessons
  - Watch for bullish reversals to balance detection
```

---

## 7. Accounting Rules

- **No margin.** No short selling. Buy only.
- **$5,000 initial cash.** All positions purchased with cash.
- Starting equity was $5,033 (includes +$33 short profit from setup — one-time artifact).
- All future P&L comes from cash-only long trades.

---

## 8. What Happens Over 3 Years

### Continuous operation
- GitHub Actions runs every 15 min on trading days (~252 days/year)
- ~40,000 runs over 3 years
- All journals accumulate in `runs/journal-*.md`
- All signals accumulate in `runs/signals-*.json`

### What the journal will show
- Daily P&L for every single day
- Every trade placed with entry price, exit, profit/loss
- Every pattern detected (even skipped ones)
- Mistakes identified and lessons learned
- Account growth trajectory ($5,000 → ?)

### Failure modes (automatic recovery)
- **GitHub Actions runner image updates** — warnings logged but bot continues
- **Alpaca API changes** — REST endpoint changes would cause 404s, logged in stdout, no trades placed
- **Rate limiting** — bot respects `X-RateLimit-Remaining` headers
- **Market closures** — holidays handled automatically (bot checks market session)
- **Token expiration** — if APCA_ACCESS_TOKEN expires, trades stop and errors are logged

### What to check when you return in 3 years
1. Open the repo on GitHub → **Actions** tab → all runs are preserved
2. Download the last journal: `runs/journal-*.md` 
3. Check `graphify-out/graph.html` for the full system map
4. Run a quick manual workflow from Actions tab to verify it still works

---

## 9. Quick Reference

```bash
# When you come back in 3 years:

# Check if the bot is still running
open https://github.com/khangnguyen04work-program/alpaca-trader/actions

# Read the latest journal
cat ~/Projects/alpaca-trader/runs/journal-$(date +%Y-%m-%d).md

# Check paper account
~/Projects/alpaca-trader/bin/alpaca account get

# View the system graph
open ~/Projects/alpaca-trader/graphify-out/graph.html

# Count total profit across all journals
grep "Day P&L" ~/Projects/alpaca-trader/runs/journal-*.md

# Trigger a manual trade check
# → GitHub → Actions → Alpaca Paper Trader → Run workflow
```

---

*Generated 2026-10-16. Powered by Hermes Agent (Nous Research), Alpaca Trading API, and The Candlestick Trading Bible by Sanyog Raut.*
