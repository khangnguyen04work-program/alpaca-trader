#!/usr/bin/env python3
"""
Alpaca Autonomous Trader — Candlestick Trading Bible strategies.
Runs as a scheduled job during US market hours (9:30 AM - 4:00 PM ET).
Supports Alpaca CLI (local) or REST API (GitHub Actions).
"""

import json
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(os.environ.get("ALPACA_BOT_DIR", str(Path.home() / "Projects/alpaca-trader")))
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "runs"

ALPACA_CLI = os.environ.get("ALPACA_CLI_PATH", str(Path.home() / "Projects/alpaca-trader/bin/alpaca"))

# ─── REST API fallback ───────────────────────────────────────────────────────
_REST = None

def _rest_call(args, quiet=True):
    global _REST
    if _REST is None:
        try:
            from bot import rest_client
            _REST = rest_client
        except ImportError:
            import rest_client as _REST
    return _REST.rest_call(args, quiet)


def run_alpaca(args, quiet=True):
    """Run an alpaca command. CLI binary preferred, falls back to REST API."""
    cmd = [ALPACA_CLI] + args
    if quiet:
        cmd.append("--quiet")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
        log(f"CLI err (code {r.returncode})", "CLI")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    except json.JSONDecodeError as e:
        log(f"JSON parse error: {e}", "CLI")

    # Fall back to REST API
    # NOTE: an empty list/dict is a VALID API response (e.g. /v2/positions on a
    # fresh account returns []). Only a dict carrying "_error" is a failure —
    # `if result` would treat [] as failure and then crash on result.get().
    result = _rest_call(args, quiet)
    if result is None:
        log("REST also failed: empty response", "CLI")
        return None
    if isinstance(result, dict) and "_error" in result:
        log(f"REST also failed: {str(result.get('_error', '?'))[:120]}", "CLI")
        return None
    return result


# ─── Config ──────────────────────────────────────────────────────────────────
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

SYMBOLS = CONFIG["symbols"]
MA_PERIODS = CONFIG["moving_averages"]
MIN_RRR = CONFIG["strategy"]["min_risk_reward_ratio"]
MAX_RISK_PCT = CONFIG["strategy"]["max_risk_per_trade_pct"]
MIN_CONFLUENCE = CONFIG["strategy"]["required_confluence_factors"]
TIF = CONFIG["strategy"]["time_in_force"]
ORDER_TYPE = CONFIG["strategy"]["order_type"]
TF_STRUCTURE = CONFIG["strategy"]["timeframes"]["structure"]
TF_CONTEXT = CONFIG["strategy"]["timeframes"]["context"]
TF_ENTRY = CONFIG["strategy"]["timeframes"]["entry"]
CONFIDENCE_MULT_MAX = CONFIG["strategy"].get("confidence_mult_max", 2.0)
MAX_TRADES_PER_SYMBOL = CONFIG["strategy"].get("max_trades_per_symbol_day", 3)
CRYPTO_SYMBOLS = CONFIG.get("crypto_symbols", [])
CRYPTO_RISK_PCT = CONFIG["strategy"].get("crypto_risk_pct", 3.0)
CRYPTO_TIF = CONFIG["strategy"].get("crypto_time_in_force", "gtc")
CRYPTO_ENTRY_MODE = CONFIG["strategy"].get("crypto_entry_mode", "market_complement")
CRYPTO_FEE_RATE = CONFIG["strategy"].get("crypto_fee_rate", 0.0025)    # taker (observed 0.2499%/side)
CRYPTO_MAKER_FEE_RATE = CONFIG["strategy"].get("crypto_maker_fee_rate", 0.0015)  # resting limits
CRYPTO_ENTRY_ORDER_TYPE = CONFIG["strategy"].get("crypto_entry_order_type", "market")
CRYPTO_EXIT_ORDER_TYPE = CONFIG["strategy"].get("crypto_signal_exit_order_type", "market")
CRYPTO_MAKER_OFFSET_PCT = CONFIG["strategy"].get("crypto_maker_offset_pct", 0.05)
CRYPTO_LIMIT_TTL_HOURS = CONFIG["strategy"].get("crypto_limit_ttl_hours", 24)
CRYPTO_ENTRY_START = CONFIG["strategy"].get("crypto_entry_start_hour", 0)
CRYPTO_ENTRY_START_MIN = CONFIG["strategy"].get("crypto_entry_start_minute", 0)
CRYPTO_ENTRY_END = CONFIG["strategy"].get("crypto_entry_end_hour", 24)
CRYPTO_ENTRY_END_MIN = CONFIG["strategy"].get("crypto_entry_end_minute", 0)
CRYPTO_WEEKEND_ALL_DAY = CONFIG["strategy"].get("crypto_weekend_all_day", True)
EXIT_SL_PCT = CONFIG["strategy"].get("stop_loss_pct", 2.5)
EXIT_TP_PCT = CONFIG["strategy"].get("take_profit_pct", 4.0)
EXIT_SL_CRYPTO_PCT = CONFIG["strategy"].get("crypto_stop_loss_pct", 3.5)
EXIT_TP_CRYPTO_PCT = CONFIG["strategy"].get("crypto_take_profit_pct", 6.0)
EXIT_SIGNAL_GATE_PCT = CONFIG["strategy"].get("exit_signal_gate_pct", 1.5)
ENTRY_COOLDOWN_HOURS = CONFIG["strategy"].get("entry_cooldown_hours", 4)
MAX_POSITION_VALUE_PCT = CONFIG["strategy"].get("max_position_value_pct_per_symbol", 10.0)
REQUIRE_HTF_ALIGNMENT = CONFIG["strategy"].get("require_htf_alignment", False)
CRYPTO_MIN_RRR = CONFIG["strategy"].get("crypto_min_risk_reward_ratio")
CRYPTO_MIN_CONFLUENCE = CONFIG["strategy"].get("crypto_required_confluence_factors")

# ─── Helpers ─────────────────────────────────────────────────────────────────

def log(msg, section="TRADER"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{section}] {msg}", flush=True)


# ─── Data Fetching ───────────────────────────────────────────────────────────

def fetch_bars(symbol, days=15, timeframe="1Day", include_today=False):
    """Fetch OHLC bars for given timeframe.
    include_today=False → end at yesterday (correct for 1Week/1Day: today's bar incomplete)
    include_today=True  → include today (correct for 15Min: bars close every 15 min)
    Crypto symbols (contain "/") always include today — crypto trades 24/7."""
    is_crypto = "/" in symbol
    now_utc = datetime.now(timezone.utc)
    if is_crypto:
        # Crypto never closes. A DATE-ONLY end clips at UTC midnight (the crypto
        # API treats it as 00:00 of that day) → data freezes until the next UTC
        # day and stays 0-24h stale. Use a full ISO timestamp instead.
        end = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        end = now_utc.strftime("%Y-%m-%d") if include_today else (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (now_utc - timedelta(days=days)).strftime("%Y-%m-%d")

    if is_crypto:
        args = ["data", "crypto", "bars", "--symbols", symbol,
                "--start", start, "--end", end, "--timeframe", timeframe]
    else:
        args = ["data", "bars", "--symbol", symbol,
                "--start", start, "--end", end, "--timeframe", timeframe,
                "--feed", "iex"]  # free-plan entitlement; SIP is 403 with API keys

    result = run_alpaca(args)
    if result and "bars" in result:
        bars = result["bars"]
        # Crypto CLI/REST return {"bars": {"BTC/USD": [...]}} — normalize to a flat list
        if isinstance(bars, dict):
            bars = next(iter(bars.values()), [])
        if len(bars) >= 2:
            if is_crypto:
                bars = _drop_incomplete_crypto_bars(bars, timeframe)
            return bars

    # Shorter range fallback for higher timeframes
    for d in [min(days, 10), 7, 5, 3]:
        s = (now_utc - timedelta(days=d)).strftime("%Y-%m-%d")
        if is_crypto:
            args_fb = ["data", "crypto", "bars", "--symbols", symbol,
                       "--start", s, "--end", end, "--timeframe", timeframe]
        else:
            args_fb = ["data", "bars", "--symbol", symbol,
                       "--start", s, "--end", end, "--timeframe", timeframe,
                       "--feed", "iex"]
        result = run_alpaca(args_fb)
        if result and "bars" in result:
            bars = result["bars"]
            if isinstance(bars, dict):
                bars = next(iter(bars.values()), [])
            if len(bars) >= 2:
                if is_crypto:
                    bars = _drop_incomplete_crypto_bars(bars, timeframe)
                return bars
    return []


_TF_MINUTES = {"1Min": 1, "5Min": 5, "15Min": 15, "30Min": 30, "1Hour": 60,
               "2Hour": 120, "4Hour": 240, "1Day": 1440, "1Week": 10080}


def _drop_incomplete_crypto_bars(bars, timeframe):
    """Crypto market never closes, so the LAST bar is usually still forming.
    A pattern on an incomplete bar is unreliable — drop it. The bar's `t` is
    its OPEN time; it is complete once t + timeframe has passed."""
    mins = _TF_MINUTES.get(timeframe)
    if not mins or not bars:
        return bars
    try:
        last_open = datetime.fromisoformat(bars[-1]["t"].replace("Z", "+00:00"))
        if last_open + timedelta(minutes=mins) > datetime.now(timezone.utc):
            return bars[:-1]
    except Exception:
        pass
    return bars


# ─── Technical Analysis ──────────────────────────────────────────────────────

def classify_candle(bar):
    o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
    body = abs(c - o)
    upper_shadow = h - max(o, c)
    lower_shadow = min(o, c) - l
    total_range = h - l
    bullish = c > o
    body_pct = (body / total_range * 100) if total_range > 0 else 0
    return {
        "time": bar["t"], "open": o, "high": h, "low": l, "close": c,
        "volume": bar.get("v", 0), "vwap": bar.get("vw", (o + c) / 2),
        "bullish": bullish, "body": body,
        "upper_shadow": upper_shadow, "lower_shadow": lower_shadow,
        "total_range": total_range, "body_pct": body_pct,
        "color": "bullish" if bullish else "bearish",
    }


def detect_pin_bar(candle):
    if candle["total_range"] == 0:
        return None
    ratio = max(candle["upper_shadow"], candle["lower_shadow"]) / max(candle["body"], 0.01)
    if candle["body_pct"] < 40 and ratio >= 2.0:
        if candle["lower_shadow"] > candle["upper_shadow"]:
            return {"type": "bullish_pin_bar", "strength": min(ratio / 3, 1.0), "candle": candle}
        elif candle["upper_shadow"] > candle["lower_shadow"]:
            return {"type": "bearish_pin_bar", "strength": min(ratio / 3, 1.0), "candle": candle}
    return None


def detect_engulfing_bar(c1, c2):
    if c2["low"] <= c1["low"] and c2["high"] >= c1["high"]:
        if c2["bullish"] and not c1["bullish"]:
            return {"type": "bullish_engulfing", "strength": c2["body"] / c1["body"] if c1["body"] > 0 else 0.5,
                    "candle": c2, "prev_candle": c1}
        elif not c2["bullish"] and c1["bullish"]:
            return {"type": "bearish_engulfing", "strength": c2["body"] / c1["body"] if c1["body"] > 0 else 0.5,
                    "candle": c2, "prev_candle": c1}
    return None


def detect_inside_bar(c1, c2):
    if c2["low"] >= c1["low"] and c2["high"] <= c1["high"] and c2["body"] < c1["body"]:
        if not c2["bullish"] and c1["bullish"]:
            return {"type": "bearish_inside_bar", "strength": 1 - (c2["body"] / c1["body"]),
                    "candle": c2, "prev_candle": c1}
        elif c2["bullish"] and not c1["bullish"]:
            return {"type": "bullish_inside_bar", "strength": 1 - (c2["body"] / c1["body"]),
                    "candle": c2, "prev_candle": c1}
    return None


def detect_doji(candle, threshold_pct=5):
    if candle["total_range"] == 0:
        return {"type": "doji", "strength": 1.0, "candle": candle}
    oc_diff_pct = abs(candle["close"] - candle["open"]) / candle["total_range"] * 100
    if oc_diff_pct < threshold_pct:
        return {"type": "doji", "strength": 1 - (oc_diff_pct / threshold_pct), "candle": candle}
    return None


def detect_morning_evening_star(bars, i):
    if i < 2 or i >= len(bars):
        return None
    c1, c2, c3 = bars[i-2], bars[i-1], bars[i]
    if (not c1["bullish"] and c3["bullish"] and c3["close"] > c1["high"] and
        c2["body"] < max(c1["body"], c3["body"])):
        return {"type": "morning_star", "strength": min(c3["body"] / c1["body"], 2.0) if c1["body"] > 0 else 0.5,
                "candle": c3, "prev": [c1, c2]}
    if (c1["bullish"] and not c3["bullish"] and c3["close"] < c1["low"] and
        c2["body"] < max(c1["body"], c3["body"])):
        return {"type": "evening_star", "strength": min(c1["body"] / c3["body"], 2.0) if c3["body"] > 0 else 0.5,
                "candle": c3, "prev": [c1, c2]}
    return None


# ─── Confluence Factors ──────────────────────────────────────────────────────

def compute_sma(bars, period):
    closes = [b["close"] for b in bars]
    if len(closes) < period:
        return None
    sma = []
    for i in range(len(closes)):
        if i < period - 1:
            sma.append(None)
        else:
            sma.append(sum(closes[i - period + 1:i + 1]) / period)
    return sma


def compute_bollinger_bands(bars, period=20):
    closes = [b["close"] for b in bars]
    if len(closes) < period:
        return None, None, None
    sma_vals, upper, lower = [], [], []
    for i in range(len(closes)):
        if i < period - 1:
            sma_vals.append(None)
            upper.append(None)
            lower.append(None)
        else:
            window = closes[i - period + 1:i + 1]
            m = sum(window) / period
            std = math.sqrt(sum((x - m) ** 2 for x in window) / period)
            sma_vals.append(m)
            upper.append(m + 2 * std)
            lower.append(m - 2 * std)
    return sma_vals, upper, lower


def identify_support_resistance_levels(bars, num_levels=3):
    """Bucket price touches into levels. Bucket size scales with price:
    $5 for stocks, $100 for BTC-scale prices — a fixed $5 bucket shatters
    an $80k price into meaningless singletons."""
    touches = defaultdict(int)
    for bar in bars:
        for val in [bar["high"], bar["low"]]:
            step = 5
            if val > 0:
                step = max(5, 10 ** (len(str(int(val))) - 3))
            touches[round(val / step) * step] += 1
    return [lvl for lvl, _ in sorted(touches.items(), key=lambda x: -x[1])[:num_levels]]


def check_confluence(candle, bars, idx, sr_levels, sma_vals, htf_trend=None):
    factors = []

    # Trend direction from last 3-5 bars (works with as few as 3 bars)
    recent = bars[max(0, idx - 5):idx + 1]
    if len(recent) >= 3:
        up_trend = sum(1 for i in range(1, len(recent)) if recent[i]["close"] > recent[i-1]["close"])
        down_trend = len(recent) - 1 - up_trend
        trend_dir = "uptrend" if up_trend > down_trend else ("downtrend" if down_trend > up_trend else "ranging")
    else:
        trend_dir = "unknown"

    # Higher-TF alignment (weekly/daily context)
    if htf_trend and htf_trend != "unknown" and htf_trend != "ranging":
        factors.append(f"htf_{htf_trend}")
        if htf_trend == trend_dir:
            factors.append("trends_aligned")

    # Support/Resistance level proximity
    if any(abs(candle["close"] - lvl) / lvl * 100 < 1.0 for lvl in sr_levels if lvl > 0):
        factors.append("near_support_resistance")

    # Moving average proximity (use whichever MAs we have data for)
    for ma_period in MA_PERIODS:
        if sma_vals and len(sma_vals) > ma_period and sma_vals[idx] is not None:
            ma = sma_vals[idx]
            if ma > 0 and abs(candle["close"] - ma) / ma * 100 < 1.5:
                factors.append(f"near_{ma_period}ma")
                break
        elif ma_period <= len(bars):
            # Try computing SMA on the fly with available data
            closes = [b["close"] for b in bars]
            if len(closes) >= ma_period:
                ma = sum(closes[-ma_period:]) / ma_period
                if ma > 0 and abs(candle["close"] - ma) / ma * 100 < 1.5:
                    factors.append(f"near_{ma_period}ma")
                    break

    # Trend alignment (works with any bars)
    if len(recent) >= 3:
        if candle["bullish"] and trend_dir == "downtrend":
            factors.append("countertrend_reversal")
        elif not candle["bullish"] and trend_dir == "uptrend":
            factors.append("countertrend_reversal")
        else:
            factors.append("trend_aligned")
    else:
        # Single-candle: strong body indicates direction
        if candle["body_pct"] > 60:
            factors.append("strong_body")

    # Bollinger Bands (only if enough data)
    bb_sma, bb_up, bb_low = compute_bollinger_bands(bars)
    if bb_up and idx >= min(19, len(bars) - 2) and bb_up[idx] is not None:
        if abs(candle["high"] - bb_up[idx]) / bb_up[idx] * 100 < 1.5:
            factors.append("touching_upper_band")
        elif abs(candle["low"] - bb_low[idx]) / bb_low[idx] * 100 < 1.5:
            factors.append("touching_lower_band")

    return factors, trend_dir


# ─── Risk & Trade Logic ──────────────────────────────────────────────────────

def calc_confidence_mult(pattern_type, strength, confluence_count, rrr, htf_trend, trend_dir):
    """Calculate position-size multiplier based on signal confidence.
    
    Base 1.0 = standard 2% risk.
    Weak signals (< 2 confluence, low RRR) → 0.5x
    Normal (2-3 confluence, RRR 1.5-2) → 1.0x
    Strong (4+ confluence, high RRR, HTF aligned) → 3.0x
    Very strong (5 confluence, RRR > 3, trends aligned) → 5.0x
    """
    mult = 1.0

    # Pattern strength
    mult += strength * 1.0  # 0.0-1.0 contribution

    # Confluence depth
    if confluence_count >= 5:
        mult += 1.5
    elif confluence_count >= 4:
        mult += 0.8
    elif confluence_count >= 3:
        mult += 0.3

    # RRR bonus
    if rrr >= 3.0:
        mult += 1.0
    elif rrr >= 2.0:
        mult += 0.3

    # HTF trend alignment
    if htf_trend and htf_trend != "unknown" and htf_trend == trend_dir:
        mult += 0.5

    return max(0.25, min(mult, CONFIDENCE_MULT_MAX))  # cash-only: max 2x base risk


def calc_position_size(account, risk_pct, confidence_mult=1.0):
    """Cash-only position sizing. Never exceeds available cash."""
    cash = float(account.get("cash", 5000))
    max_risk = cash * (risk_pct / 100) * confidence_mult
    return min(max_risk, cash), max_risk


def estimate_stop_loss(candle, pattern_type, trend_dir, bars=None, idx=None):
    if "bullish" in pattern_type or pattern_type in ("morning_star", "bullish_inside_bar"):
        stop = candle["low"] - (candle["total_range"] * 0.25)
        risk = candle["close"] - stop
    else:
        stop = candle["high"] + (candle["total_range"] * 0.25)
        risk = stop - candle["close"]
    return round(stop, 2), round(max(abs(risk), 0.01), 2)


def estimate_profit_target(candle, pattern_type, bars=None, idx=None, sr_levels=None, rrr=1.5):
    dist = max(candle["total_range"] * 0.8, 0.01)
    if "bullish" in pattern_type or pattern_type in ("morning_star", "bullish_inside_bar"):
        return round(candle["close"] + dist, 2)
    return round(candle["close"] - dist, 2)


_CYCLE_SIDES = {}  # symbol -> sides placed this cycle (whipsaw / wash-trade guard)


def _maker_limit_price(close, side):
    """Resting (non-marketable) limit: buys slightly BELOW the last price, sells
    slightly ABOVE. A marketable limit crosses the book and pays the TAKER fee —
    pointless. Offsetting by CRYPTO_MAKER_OFFSET_PCT keeps the order resting so
    it earns the MAKER fee (0.15% vs 0.25%)."""
    off = (CRYPTO_MAKER_OFFSET_PCT / 100.0) * close
    return round(close - off if side == "buy" else close + off, 2)


def _cancel_open_sell_limits(sym):
    """Cancel resting SELL limits for a symbol before a MARKET close — otherwise
    a resting exit + a stop-loss market close could double-sell the position."""
    if _norm_sym(sym) not in {_norm_sym(s) for s in CRYPTO_SYMBOLS}:
        return
    opens = run_alpaca(["order", "list", "--status", "open", "--limit", "50"])
    if not isinstance(opens, list):
        return
    for o in opens:
        if (_norm_sym(o.get("symbol", "")) == _norm_sym(sym)
                and o.get("side") == "sell" and o.get("type") == "limit" and o.get("id")):
            log(f"  cancelling resting sell limit {str(o.get('id'))[:12]} before market close", "EXIT")
            run_alpaca(["order", "cancel", "--order-id", o.get("id")])


def _cancel_stale_crypto_limits():
    """TTL sweep: cancel resting crypto LIMIT orders older than the TTL — a stale
    entry limit filling days later could stack exposure past the caps."""
    if CRYPTO_LIMIT_TTL_HOURS <= 0:
        return
    opens = run_alpaca(["order", "list", "--status", "open", "--limit", "50"])
    if not isinstance(opens, list):
        return
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=CRYPTO_LIMIT_TTL_HOURS)
    for o in opens:
        sym = o.get("symbol", "")
        if _norm_sym(sym) not in {_norm_sym(s) for s in CRYPTO_SYMBOLS} or o.get("type") != "limit":
            continue
        try:
            ts = datetime.fromisoformat((o.get("created_at") or "")[:19])
        except Exception:
            continue
        if ts < cutoff and o.get("id"):
            log(f"  TTL cancel: resting crypto limit from {ts:%m-%d %H:%M} ({o.get('side')} {o.get('qty')})", "ORDER")
            run_alpaca(["order", "cancel", "--order-id", o.get("id")])


def place_order(symbol, side, qty, order_type="market", tif="day",
                limit_price=None, stop_price=None, client_order_id=None):
    """Place a paper order. client_order_id gives Alpaca-side idempotency:
    a duplicate order with the same id is rejected (422) — prevents runaway
    re-buys when the same 15Min signal is re-scanned every 2 minutes."""
    args = ["order", "submit", "--symbol", symbol, "--side", side,
            "--qty", str(qty), "--type", order_type, "--time-in-force", tif]
    if limit_price:
        args.extend(["--limit-price", str(limit_price)])
    if stop_price:
        args.extend(["--stop-price", str(stop_price)])
    if client_order_id:
        args.extend(["--client-order-id", client_order_id])
    log(f"Order: --symbol {symbol} --side {side} --qty {qty} --type {order_type}", "ORDER")
    result = run_alpaca(args)
    if result:
        log(f"Placed: ID={result.get('id','?')} Status={result.get('status','?')}", "ORDER")
    else:
        log("Order failed", "ORDER")
    return result


# ─── Position Exit Management (stop-loss / take-profit) ─────────────────────

def _norm_sym(symbol):
    """Normalize symbols for comparison: positions come back as BTCUSD,
    orders use BTC/USD."""
    return symbol.replace("/", "")


def _asset_class(sym):
    """'crypto' or 'equity' — works for BTC/USD (orders) and BTCUSD (positions)."""
    s = str(sym)
    if s in CRYPTO_SYMBOLS or s.replace("/", "") in {x.replace("/", "") for x in CRYPTO_SYMBOLS}:
        return "crypto"
    return "equity"


def _position_qty(sym):
    """Current held quantity for a symbol (0 if none)."""
    for p in (run_alpaca(["position", "list"]) or []):
        if isinstance(p, dict) and _norm_sym(p.get("symbol", "")) == _norm_sym(sym):
            try:
                return float(p.get("qty", 0) or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _close_position(sym, reason="manual", qty=None, plpc=None):
    """Exit a position with a full-size MARKET order (captures the order id so
    the journal can attribute realized P&L to its exit reason), records the exit
    in the signals file, then VERIFIES by re-reading the position list — an API
    204 is not proof of a fill."""
    is_crypto = _norm_sym(sym) in {_norm_sym(s) for s in CRYPTO_SYMBOLS}
    if qty is None:
        qty = _position_qty(sym)
    if not qty:
        log(f"  no position quantity for {sym} — nothing to close", "EXIT")
        return False, None
    qty_str = f"{abs(qty):.6f}".rstrip('0').rstrip('.') if is_crypto else str(int(abs(qty)))
    tif = CRYPTO_TIF if is_crypto else TIF
    cid = f"bot-exit-{reason}-{_norm_sym(sym)}-{datetime.now().strftime('%Y-%m-%dT%H%M')}"
    _cancel_open_sell_limits(sym)  # a resting exit limit + this market close = double sell
    order = place_order(sym, "sell", qty_str, ORDER_TYPE, tif, client_order_id=cid)
    order_id = (order or {}).get("id") if isinstance(order, dict) else None

    # Record the exit so the journal can attribute P&L to its exit reason
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        _save_signal(LOG_DIR / f"signals-{today}.json", {
            "symbol": sym, "pattern": f"EXIT_{reason.upper()}", "side": "sell",
            "traded": bool(order_id), "order_id": order_id or "",
            "price": None,
            "exit_pct": round(plpc * 100, 2) if plpc is not None else None,
            "qty": qty_str,
            "instance": f"EXIT|{reason}|{sym}|{datetime.now().strftime('%Y-%m-%dT%H:%M')}",
        })
    except Exception as e:
        log(f"  could not record exit record: {e}", "EXIT")

    # Verify the position actually went away
    time.sleep(2)
    still_open = _position_qty(sym) > 0
    if still_open:
        log(f"  close PENDING/FAILED for {sym} ({reason}) — will retry next cycle", "EXIT")
        return False, order_id
    log(f"  closed {sym} ({reason})", "EXIT")
    return True, order_id


def manage_open_positions():
    """Stop-loss / take-profit enforcement. Runs at the start of every cycle.
    - Crypto positions: evaluated 24/7
    - Equity positions: evaluated only while the market is open
    Thresholds come from config: stop_loss_pct/take_profit_pct (+ crypto variants)."""
    positions = run_alpaca(["position", "list"])
    if not isinstance(positions, list) or not positions:
        return
    crypto_set = {_norm_sym(s) for s in CRYPTO_SYMBOLS}
    for p in positions:
        sym = p.get("symbol", "")
        try:
            plpc = float(p.get("unrealized_plpc", 0) or 0)
        except (TypeError, ValueError):
            continue
        is_crypto = _norm_sym(sym) in crypto_set
        if not is_crypto and not is_market_session():
            continue  # equity positions can only be closed during market hours
        sl = EXIT_SL_CRYPTO_PCT if is_crypto else EXIT_SL_PCT
        tp = EXIT_TP_CRYPTO_PCT if is_crypto else EXIT_TP_PCT
        if plpc <= -(sl / 100.0):
            log(f"  ⛔ STOP-LOSS {sym}: {plpc*100:+.2f}% <= -{sl}%", "EXIT")
            _close_position(sym, reason="stop_loss", qty=p.get("qty"), plpc=plpc)
        elif plpc >= (tp / 100.0):
            log(f"  🎯 TAKE-PROFIT {sym}: {plpc*100:+.2f}% >= +{tp}%", "EXIT")
            _close_position(sym, reason="take_profit", qty=p.get("qty"), plpc=plpc)


# ─── Market Hours ────────────────────────────────────────────────────────────

def is_market_session():
    now = datetime.now()
    return now.replace(hour=9, minute=30) <= now <= now.replace(hour=16, minute=0)


def is_after_market_close():
    return datetime.now() >= datetime.now().replace(hour=16, minute=30)


# ─── Signal Logging ─────────────────────────────────────────────────────────

def _save_signal(path, record):
    path = Path(str(path))
    records = []
    if path.exists():
        try:
            with open(path) as f:
                records = json.load(f)
        except Exception:
            records = []
    records.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(records, f, indent=2)


# ─── Top-Down Timeframe Analysis ─────────────────────────────────────────────

def determine_market_regime(bars):
    """Analyze weekly/daily bars to determine market structure.
    Returns: regime (trending_up/trending_down/ranging/choppy), sr_levels, trend_dir"""
    if not bars or len(bars) < 2:
        return "unknown", [], "unknown"

    classified = [classify_candle(b) for b in bars]

    # Trend direction from closes
    closes = [b["close"] for b in classified]
    up = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
    down = len(closes) - 1 - up

    if up > down + 1:
        trend_dir = "uptrend"
    elif down > up + 1:
        trend_dir = "downtrend"
    else:
        trend_dir = "ranging"

    # Regime based on volatility vs trend consistency
    volatility = max(closes) - min(closes)
    avg_move = sum(abs(closes[i] - closes[i-1]) for i in range(1, len(closes))) / (len(closes)-1) if len(closes) > 1 else 0
    regime = "trending" if abs(up - down) >= 2 else ("ranging" if avg_move > 0 else "choppy")

    sr_levels = identify_support_resistance_levels(classified)

    return regime, sr_levels, trend_dir


# ─── Analysis & Trading ──────────────────────────────────────────────────────

_CLOCK_CACHE = {}

def equity_market_open_now():
    """True/False from Alpaca's market clock — holiday- and early-close-aware.
    None when the clock is unreachable. Cached once per process (run)."""
    if "open" in _CLOCK_CACHE:
        return _CLOCK_CACHE["open"]
    if "failed" in _CLOCK_CACHE:
        return None
    clk = run_alpaca(["clock"])
    if isinstance(clk, dict) and "is_open" in clk:
        _CLOCK_CACHE["open"] = bool(clk["is_open"])
        return _CLOCK_CACHE["open"]
    _CLOCK_CACHE["failed"] = True
    return None


def crypto_entry_allowed(now=None):
    """Gate for opening NEW crypto positions. Exits are NEVER gated — stop-loss,
    take-profit and sell-signal exits stay active around the clock.

    mode=market_complement (default): crypto entries are allowed exactly while
    the equity market is CLOSED per Alpaca's own clock — nights, weekends,
    holidays, early-close days. Seamless: crypto hands off to equities at
    9:30 ET, and takes over again the moment the equity session ends.

    Fallback (mode=fixed_window, or clock unreachable): weekdays within
    start→end ET (default 16:00→09:30, crosses midnight), all weekend."""
    now = now or datetime.now()
    if CRYPTO_ENTRY_MODE == "market_complement":
        open_now = equity_market_open_now()
        if open_now is not None:
            return not open_now
        # clock unreachable → fall through to the fixed window
    if now.weekday() >= 5 and CRYPTO_WEEKEND_ALL_DAY:
        return True
    start = CRYPTO_ENTRY_START * 60 + CRYPTO_ENTRY_START_MIN
    end = CRYPTO_ENTRY_END * 60 + CRYPTO_ENTRY_END_MIN
    if start == end:
        return True  # no restriction
    cur = now.hour * 60 + now.minute
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # crosses midnight


_TODAY_ORDERS_CACHE = None

def _today_orders_index():
    """Single fetch per run of today's orders from Alpaca — SERVER TRUTH.
    GitHub runners only see COMMITTED signals files, so the file-based cap and
    dedup silently reset between runs; Alpaca's order history never does.
    Returns {"buys": {symbol: count}, "client_ids": set} or None on failure."""
    global _TODAY_ORDERS_CACHE
    if _TODAY_ORDERS_CACHE is not None:
        return _TODAY_ORDERS_CACHE
    orders = run_alpaca(["order", "list", "--status", "all", "--limit", "500"])
    if not isinstance(orders, list):
        return None  # not cached — a transient failure retries next signal
    # Rolling 24h window in UTC — order timestamps are UTC while the bot's clock
    # is ET (TZ env). A calendar-date match silently drops every order created
    # after 20:00 ET (=00:00 UTC); 24h rolling is also correct for 24/7 crypto.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    buys, cids, last_sells = {}, set(), {}
    for o in orders:
        try:
            ts = datetime.fromisoformat((o.get("created_at") or "")[:19])
        except Exception:
            continue
        if ts < cutoff:
            continue
        cids.add(o.get("client_order_id") or "")
        if o.get("side") == "buy" and o.get("status") in (
                "filled", "accepted", "new", "pending_new", "partially_filled", "held"):
            sym = o.get("symbol") or ""
            buys[sym] = buys.get(sym, 0) + 1
        if o.get("side") == "sell" and o.get("status") == "filled":
            sym = o.get("symbol") or ""
            if ts > last_sells.get(sym, ts):
                last_sells[sym] = ts
    _TODAY_ORDERS_CACHE = {"buys": buys, "client_ids": cids, "last_sells": last_sells}
    return _TODAY_ORDERS_CACHE


def is_crypto_symbol(symbol):
    """Crypto symbols contain '/' (e.g. BTC/USD). They trade 24/7."""
    return "/" in symbol


def analyze_and_trade(symbol):
    """Full top-down multi-timeframe analysis per the book.
    1. Weekly/daily: market structure, regime, S/R levels
    2. 15Min: entry signals at key levels
    3. Trade only when all timeframes align."""
    log(f"Top-down analysis of {symbol}...", "ANALYSIS")
    crypto = is_crypto_symbol(symbol)
    # Crypto quality gates (config) fall back to the shared equity thresholds
    eff_min_rrr = CRYPTO_MIN_RRR if (crypto and CRYPTO_MIN_RRR is not None) else MIN_RRR
    eff_min_conf = (CRYPTO_MIN_CONFLUENCE if (crypto and CRYPTO_MIN_CONFLUENCE is not None)
                    else MIN_CONFLUENCE)

    # Phase 1: Higher TF — Market structure
    weekly = fetch_bars(symbol, days=90, timeframe=TF_STRUCTURE)
    daily = fetch_bars(symbol, days=30, timeframe=TF_CONTEXT)

    # Adaptive days for entry timeframe: 1Day needs fewer bars than 15Min
    if "15" in TF_ENTRY or "5" in TF_ENTRY:
        entry_days = 1
    elif "Hour" in TF_ENTRY or "60" in TF_ENTRY:
        entry_days = 3
    else:
        entry_days = 5
    # Include today: a 15Min bar closes every 15 min — yesterday-end would be 24h stale.
    # Pre-market fallback: if today has no bars yet, use yesterday's completed bars.
    entry_bars_raw = fetch_bars(symbol, days=entry_days, timeframe=TF_ENTRY, include_today=True)
    if not entry_bars_raw or len(entry_bars_raw) < 3:
        entry_bars_raw = fetch_bars(symbol, days=entry_days, timeframe=TF_ENTRY, include_today=False)

    if not entry_bars_raw or len(entry_bars_raw) < 3:
        log(f"Not enough {TF_ENTRY} data for {symbol}", "ANALYSIS")
        return

    # Determine market regime from weekly first, fall back to daily
    regime, htf_sr, htf_trend = determine_market_regime(weekly)
    if regime == "unknown" or not htf_sr:
        regime, htf_sr, htf_trend = determine_market_regime(daily)

    log(f"  Regime: {regime} | Higher-TF trend: {htf_trend} | S/R: {htf_sr}", "STRUCTURE")

    # Skip choppy markets (per the book: "never trade in choppy/unidentifiable markets")
    if regime == "choppy":
        log(f"  Choppy market — skipping {symbol}", "ANALYSIS")
        return

    # Phase 2: Entry TF — candlestick patterns
    bars = [classify_candle(b) for b in entry_bars_raw]

    sma_vals = {}
    for period in MA_PERIODS:
        sma_vals[period] = compute_sma(bars, period)

    sr_levels = identify_support_resistance_levels(bars)
    # Merge with higher-TF levels
    all_sr = list(set(sr_levels + htf_sr))

    account = run_alpaca(["account", "get"])
    if not account:
        log("Cannot get account status", "FATAL")
        return
    log(f"  Account: ${account['equity']} cash ${account['cash']} BP ${account['buying_power']}", "ACCOUNT")

    signals = []
    for i in range(max(0, len(bars) - 5), len(bars)):
        c = bars[i]
        for sig in [detect_pin_bar(c), detect_doji(c)]:
            if sig:
                signals.append((i, sig))
        if i > 0:
            for sig in [detect_engulfing_bar(bars[i-1], c), detect_inside_bar(bars[i-1], c)]:
                if sig:
                    signals.append((i, sig))
        sig = detect_morning_evening_star(bars, i)
        if sig:
            signals.append((i, sig))

    if not signals:
        log(f"  No patterns on {TF_ENTRY} for {symbol}", "ANALYSIS")
        return

    log(f"  Found {len(signals)} pattern(s) on {TF_ENTRY}", "ANALYSIS")

    for si, signal in signals:
        pattern_type = signal["type"]
        candle = signal["candle"]

        all_sma = sma_vals.get(MA_PERIODS[0], [None] * len(bars))
        factors, trend_dir = check_confluence(candle, bars, si, all_sr, all_sma, htf_trend)
        cc = len(factors)

        today = datetime.now().strftime("%Y-%m-%d")
        signals_path = LOG_DIR / f"signals-{today}.json"
        sig_record = {
            "symbol": symbol, "pattern": pattern_type,
            "strength": round(signal["strength"], 2), "confluence": cc,
            "factors": factors, "trend_dir": trend_dir,
            "htf_trend": htf_trend, "regime": regime,
            "price": candle["close"], "traded": False,
            "instance": f"{symbol}|{pattern_type}|{candle['time']}"
        }

        # 0) Instance dedup: this exact (symbol, pattern, bar) was already
        #    handled in an earlier cycle. Bars stay in the scan window for up
        #    to 5 more candles, so without this the bot re-attempts the same
        #    order every run → Alpaca 422s (duplicate client_order_id),
        #    misleading "Order failed" logs, and a bloated signals file.
        try:
            _seen = json.loads(signals_path.read_text()) if signals_path.exists() else []
        except Exception:
            _seen = []
        raw_id = f"bot-{symbol}-{pattern_type}-{candle['time']}"
        client_order_id = "".join(c if c.isalnum() or c in "-_." else "-" for c in raw_id)[:128]
        _idx = _today_orders_index()
        # Skip instances already handled — but a record that ERRORED (order failed,
        # e.g. insufficient balance) must NOT block the retry: the exit has to be
        # able to go through on the next cycle.
        if any(s.get("instance") == sig_record["instance"] and not s.get("error") for s in _seen) or \
           (_idx is not None and client_order_id in _idx["client_ids"]):
            continue  # silently skip — already handled (file record or live order)

        log(f"  {pattern_type} | conf {cc} [{','.join(factors)}] | trend {trend_dir}", "SIGNAL")

        if cc < eff_min_conf:
            log(f"  skip (need {eff_min_conf} conf)", "SIGNAL")
            sig_record["skipped"] = "confluence"
            _save_signal(signals_path, sig_record)
            continue

        if pattern_type in ("bullish_pin_bar", "bullish_engulfing", "morning_star", "bullish_inside_bar"):
            side = "buy"
        elif pattern_type in ("bearish_pin_bar", "bearish_engulfing", "evening_star", "bearish_inside_bar"):
            side = "sell"
        else:
            _save_signal(signals_path, sig_record)
            continue

        # HTF alignment (crypto): never buy against the higher-timeframe trend —
        # reversal patterns need a trend to work with; fighting a downtrend is
        # how accounts bleed in chop.
        if crypto and REQUIRE_HTF_ALIGNMENT and side == "buy" and htf_trend == "downtrend":
            log("  skip BUY — higher-timeframe trend is downtrend", "TREND")
            sig_record["skipped"] = "htf_misaligned"
            _save_signal(signals_path, sig_record)
            continue

        # Cash-only: allow sell only if we already own shares (exit a position)
        if side == "sell":
            # Check if we own this symbol
            pos_check = run_alpaca(["position", "list"])
            owned = False
            if isinstance(pos_check, list):
                for p in pos_check:
                    # positions for crypto come back as BTCUSD — normalize both sides
                    if _norm_sym(p.get("symbol", "")) == _norm_sym(symbol) \
                       and float(p.get("qty", 0) or 0) > 0:
                        owned = True
                        break
            if not owned:
                log("  skip SELL — don't own shares to sell", "CASH")
                sig_record["skipped"] = "no_position"
                _save_signal(signals_path, sig_record)
                continue

        # Profit-gated signal exits (crypto): ONE bearish candle is not a reason
        # to sell. Exit early only on a MEANINGFUL move; between the gates, hold
        # and let the stop-loss / take-profit manage. This removes chop-whipsaw
        # churn (15/15 historical exits were noise-driven signal sells).
        if side == "sell" and crypto and EXIT_SIGNAL_GATE_PCT > 0:
            plpc = 0.0
            for p in (pos_check if isinstance(pos_check, list) else []):
                if _norm_sym(p.get("symbol", "")) == _norm_sym(symbol):
                    try:
                        plpc = float(p.get("unrealized_plpc", 0) or 0)
                    except (TypeError, ValueError):
                        plpc = 0.0
                    break
            gate = EXIT_SIGNAL_GATE_PCT / 100.0
            if -gate < plpc < gate:
                log(f"  skip SELL — bearish signal but move only {plpc*100:+.2f}% "
                    f"(gate ±{EXIT_SIGNAL_GATE_PCT:g}%); holding for stop/target", "EXIT")
                sig_record["skipped"] = "exit_not_significant"
                _save_signal(signals_path, sig_record)
                continue
            sig_record["exit_plpc"] = round(plpc * 100, 2)  # used to pick the exit order type

        # Crypto trades 24/7 — the equity market-hours gate does not apply
        if not crypto and not is_market_session():
            log("  market closed — analysis only", "MARKET")
            sig_record["skipped"] = "market_closed"
            _save_signal(signals_path, sig_record)
            continue

        # Crypto entry window: NEW buys only inside the configured ET hours
        # (weekdays 17:00→08:00, all weekend). Exits — stop-loss/take-profit
        # and sell signals — are never gated; they stay active 24/7.
        if crypto and side == "buy" and not crypto_entry_allowed():
            if CRYPTO_ENTRY_MODE == "market_complement" and equity_market_open_now() is not None:
                why = "equity market open — crypto hands off to equities"
            else:
                why = f"outside {CRYPTO_ENTRY_START:02d}:{CRYPTO_ENTRY_START_MIN:02d}–{CRYPTO_ENTRY_END:02d}:{CRYPTO_ENTRY_END_MIN:02d} ET window"
            log(f"  crypto entries paused ({why})", "WINDOW")
            sig_record["skipped"] = "outside_entry_window"
            _save_signal(signals_path, sig_record)
            continue

        # Risk management (needed for confidence multiplier)
        stop, _ = estimate_stop_loss(candle, pattern_type, trend_dir, bars, si)
        target = estimate_profit_target(candle, pattern_type, bars, si, all_sr, MIN_RRR)

        risk_ps = (candle["close"] - stop) if side == "buy" else (stop - candle["close"])
        reward_ps = (target - candle["close"]) if side == "buy" else (candle["close"] - target)
        rrr = reward_ps / risk_ps if risk_ps > 0 else 0

        # Confidence-based position sizing
        confidence_mult = calc_confidence_mult(
            pattern_type, signal["strength"], cc, rrr, htf_trend, trend_dir
        )
        if crypto:
            risk_pct = CRYPTO_RISK_PCT
        else:
            risk_pct = MAX_RISK_PCT
        pos_value, _ = calc_position_size(account, risk_pct, confidence_mult)
        if crypto:
            # Crypto supports fractional quantities — never force integer shares
            # (1 BTC ≈ $82k would dwarf the whole account)
            qty = round(pos_value / candle["close"], 6)
            notional = qty * candle["close"]
            if notional < 10:  # Alpaca crypto minimum order size
                log(f"  skip — notional ${notional:.2f} below crypto minimum", "RISK")
                _save_signal(signals_path, sig_record)
                continue
        else:
            qty = max(1, int(pos_value / candle["close"]))

        qty_str = f"{qty:.6f}".rstrip('0').rstrip('.') if crypto else str(qty)

        if side == "sell":
            # Exits close what we actually OWN (server truth) — a risk-sized qty
            # can exceed the balance once crypto fees shrink the position, which
            # Alpaca rejects (40310000 insufficient balance) and leaves the exit
            # stuck retrying forever.
            owned_qty = _position_qty(symbol)
            if owned_qty <= 0:
                log("  skip SELL — position already flat", "CASH")
                sig_record["skipped"] = "no_position"
                _save_signal(signals_path, sig_record)
                continue
            if crypto:
                # Floor to 6dp: "{x:.6f}" ROUNDS and can exceed the real balance by
                # fractions of a satoshi — Alpaca rejects the exit entirely
                # (40310000 insufficient balance) and it retries forever.
                qty_str = f"{math.floor(owned_qty * 1e6) / 1e6:.6f}".rstrip('0').rstrip('.')
                if qty_str in ("", "0"):
                    log("  skip SELL — nothing sellable after truncation", "CASH")
                    sig_record["skipped"] = "no_position"
                    _save_signal(signals_path, sig_record)
                    continue
            else:
                qty_str = str(int(owned_qty))

        log(f"  Conf mult: {confidence_mult:.1f}x | Risk: ${pos_value:.0f} | {side.upper()} {qty_str} @ ${candle['close']:.2f} stop ${stop:.2f} target ${target:.2f} RRR {rrr:.2f}", "TRADE")

        # Store confidence info in signal record
        sig_record["confidence_mult"] = round(confidence_mult, 2)
        sig_record["risk_amount"] = round(pos_value, 2)
        sig_record["rrr"] = round(rrr, 2)
        sig_record["qty"] = qty_str

        if not crypto and qty < 1:
            _save_signal(signals_path, sig_record)
            continue

        if side != "sell" and rrr < eff_min_rrr:
            # RRR is an ENTRY-quality bar. Exits of owned positions are risk
            # management — the profit-gate above decides if a signal exit is
            # meaningful; blocking a profit-lock because the bearish setup's
            # own RRR is low would trap winners.
            log(f"  skip RRR {rrr:.2f} < {eff_min_rrr}", "TRADE")
            sig_record["skipped"] = "rrr"
            _save_signal(signals_path, sig_record)
            continue

        # ── Anti-runaway guards ─────────────────────────────────────────
        # 1) Daily ENTRY cap per symbol — orders-based (server truth) so it
        #    holds across workflow runs, not just within one process.
        if _idx is not None:
            traded_today = _idx["buys"].get(symbol, 0)
        else:
            traded_today = sum(1 for s in _seen
                               if s.get("symbol") == symbol and s.get("traded"))
        if traded_today >= MAX_TRADES_PER_SYMBOL:
            log(f"  skip — daily cap {traded_today}/{MAX_TRADES_PER_SYMBOL} trades for {symbol}", "RISK")
            sig_record["skipped"] = "daily_cap"
            _save_signal(signals_path, sig_record)
            continue

        # 4) Re-entry cooldown (crypto): after a full exit, wait before buying
        #    the same symbol again — kills the sell-low / rebuy-high loop.
        if crypto and side == "buy" and ENTRY_COOLDOWN_HOURS > 0 and _idx is not None:
            last_sell = (_idx.get("last_sells") or {}).get(symbol)
            if last_sell:
                mins_ago = (datetime.now(timezone.utc).replace(tzinfo=None) - last_sell).total_seconds() / 60.0
                if mins_ago < ENTRY_COOLDOWN_HOURS * 60:
                    log(f"  skip BUY — cooldown: exited {mins_ago/60:.1f}h ago (< {ENTRY_COOLDOWN_HOURS:g}h)", "RISK")
                    sig_record["skipped"] = "cooldown"
                    _save_signal(signals_path, sig_record)
                    continue

        # 5) Total exposure cap per symbol (crypto): per-entry risk sizing lets a
        #    position silently stack across days — cap what ONE symbol may hold.
        if crypto and side == "buy" and MAX_POSITION_VALUE_PCT > 0:
            owned_qty = _position_qty(symbol)
            cur_val = owned_qty * candle["close"]
            cap = float(account.get("equity", 0) or 0) * MAX_POSITION_VALUE_PCT / 100.0
            if cur_val + notional > cap:
                log(f"  skip BUY — exposure cap: ${cur_val:.0f} held + ${notional:.0f} new exceeds "
                    f"{MAX_POSITION_VALUE_PCT:g}% of equity (${cap:.0f})", "RISK")
                sig_record["skipped"] = "exposure_cap"
                _save_signal(signals_path, sig_record)
                continue

        # 2) Idempotent client_order_id (built above, near the dedup check):
        #    same (symbol, pattern, bar) re-scanned gets REJECTED by Alpaca (422).
        #
        # 3) One DIRECTION per symbol per cycle: a bullish entry followed by a
        #    bearish exit in the same scan is churn — it pays two fee legs for a
        #    round trip inside one run. Skip the opposite side until next cycle.
        if _CYCLE_SIDES.get(symbol) and side not in _CYCLE_SIDES[symbol]:
            log(f"  skip {side.upper()} — opposite-direction trade already placed on {symbol} this cycle", "RISK")
            sig_record["skipped"] = "opposite_side_same_cycle"
            _save_signal(signals_path, sig_record)
            continue

        order_tif = CRYPTO_TIF if crypto else TIF
        if crypto:
            # Maker orders (crypto) — rest slightly off the last price to earn the
            # 0.15% maker fee instead of the 0.25% taker. ASYMMETRY on exits:
            # profit-locks rest as limits (a better fill is a bonus; the +6% TP
            # backstops), loss-cuts stay MARKET (never wait for a fill on a loser).
            pl100 = sig_record.get("exit_plpc") or 0.0
            otype = (CRYPTO_ENTRY_ORDER_TYPE if side == "buy"
                     else (CRYPTO_EXIT_ORDER_TYPE if pl100 > 0 else "market"))
        else:
            otype = ORDER_TYPE
        limit_price = (_maker_limit_price(candle["close"], side)
                       if (crypto and otype == "limit") else None)
        result = place_order(symbol, side, qty_str, otype, order_tif,
                             limit_price=limit_price, client_order_id=client_order_id)
        if limit_price:
            sig_record["limit_price"] = limit_price
        if result:
            _CYCLE_SIDES.setdefault(symbol, set()).add(side)
            # Keep the in-run orders index current: the cached snapshot predates
            # this fill, and a stale count let same-run signals bypass the daily
            # cap (4 NVDA buys vs cap 3 on 2026-10-19).
            if _TODAY_ORDERS_CACHE is not None and side == "buy":
                _TODAY_ORDERS_CACHE["buys"][symbol] = _TODAY_ORDERS_CACHE["buys"].get(symbol, 0) + 1
                _TODAY_ORDERS_CACHE["client_ids"].add(client_order_id)
            sig_record["traded"] = True
            sig_record["order_id"] = result.get("id", "")
            sig_record["order_status"] = result.get("status", "")
            log(f"  done ID={result.get('id','?')[:12]}", "TRADE")
        else:
            sig_record["error"] = "order_failed"
        _save_signal(signals_path, sig_record)


# ─── Daily Journal ───────────────────────────────────────────────────────────

# ─── Daily Journal (statistical) ────────────────────────────────────────────

def _et_date(ts_utc):
    """Bucket a UTC API timestamp by its ET calendar date (the trading day).
    Naive string-prefix matching against ET 'today' mis-buckets every order
    created between 20:00 ET and midnight (that timestamp already carries the
    NEXT UTC date) — a silent statistics killer for the 24/7 crypto loop."""
    try:
        return datetime.fromisoformat(str(ts_utc)[:19]).replace(
            tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return str(ts_utc)[:10]


def _fifo_round_trips(orders):
    """FIFO-match FILLED orders per symbol into closed round trips + open lots.
    Quantities come from filled_qty (partial fills handled by quantity, never by
    order status alone). Returns (trips, open_lots, warnings)."""
    from collections import defaultdict, deque
    warnings, fills = [], defaultdict(list)
    for o in orders:
        if o.get("status") != "filled":
            continue
        try:
            qty = float(o.get("filled_qty") or 0)
            px = float(o.get("filled_avg_price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or px <= 0:
            warnings.append(f"fill missing qty/price: {o.get('symbol')} {str(o.get('id',''))[:12]}")
            continue
        try:
            if float(o.get("qty") or 0) > qty + 1e-9:
                warnings.append(f"partial fill: {o.get('symbol')} {str(o.get('id',''))[:12]} {qty:g}/{o.get('qty')}")
        except (TypeError, ValueError):
            pass
        fills[o.get("symbol", "?")].append({
            "side": o.get("side"), "qty": qty, "px": px,
            "t": o.get("filled_at") or o.get("created_at") or "",
            "id": o.get("id", ""),
            "cid": str(o.get("client_order_id") or ""),
            "otype": o.get("type", "market"),
        })
    trips, open_lots = [], []
    for sym, fs in fills.items():
        fs.sort(key=lambda x: x["t"])
        longs = deque()
        for f in fs:
            if f["side"] == "buy":
                longs.append(dict(f))
                continue
            remaining = f["qty"]
            while remaining > 1e-9 and longs:
                lot = longs[0]
                take = min(remaining, lot["qty"])
                gross = (f["px"] - lot["px"]) * take
                # Crypto taker fee is charged IN ASSET terms (~0.25% per side): the
                # position receives less than the order's filled_qty. Fill-to-fill
                # P&L therefore OVERSTATES results unless the fee is subtracted.
                r_in = CRYPTO_MAKER_FEE_RATE if lot.get("otype") == "limit" else CRYPTO_FEE_RATE
                r_out = CRYPTO_MAKER_FEE_RATE if f.get("otype") == "limit" else CRYPTO_FEE_RATE
                fee = (r_in * lot["px"] + r_out * f["px"]) * take if "/" in sym else 0.0
                trips.append({
                    "symbol": sym, "qty": take,
                    "entry_t": lot["t"], "entry_px": lot["px"], "entry_id": lot["id"],
                    "exit_t": f["t"], "exit_px": f["px"], "exit_id": f["id"],
                    "entry_cid": lot.get("cid", ""), "exit_cid": f.get("cid", ""),
                    "pnl": gross, "fee_est": fee, "pnl_net": gross - fee,
                    "pnl_pct": ((f["px"] - lot["px"]) / lot["px"] * 100.0) if lot["px"] else 0.0,
                })
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    longs.popleft()
            if remaining > 1e-9:
                warnings.append(f"SELL exceeds long position: {sym} {remaining:g} (short?)")
        for lot in longs:
            open_lots.append({"symbol": sym, "qty": lot["qty"], "px": lot["px"],
                              "t": lot["t"], "id": lot["id"]})
    return trips, open_lots, warnings


def _held_span(t0, t1):
    try:
        a = datetime.fromisoformat(str(t0)[:19]); b = datetime.fromisoformat(str(t1)[:19])
        mins = max(0, int((b - a).total_seconds() // 60))
        return f"{mins // 60}h{mins % 60:02d}m" if mins >= 60 else f"{mins}m"
    except Exception:
        return "?"


_PATTERN_TOKENS = ("bullish_pin_bar", "bearish_pin_bar", "bullish_engulfing", "bearish_engulfing",
                   "morning_star", "evening_star", "bullish_inside_bar", "bearish_inside_bar", "doji")


def _attr_from_cid(cid):
    """Attribute a trade leg from the order's client_order_id. This is SERVER
    TRUTH: GitHub runners are ephemeral, so signals files can be lost — but the
    order history (and its ids) always carries the pattern / exit reason.
    Returns (pattern_or_empty, exit_label_or_None)."""
    cid = str(cid or "")
    if not cid:
        return "", None
    if cid.startswith("bot-exit-"):
        reason = cid[len("bot-exit-"):].split("-")[0]
        label = {"stop_loss": "STOP-LOSS", "take_profit": "TAKE-PROFIT"}.get(reason, reason or "?")
        return "", label
    for tok in _PATTERN_TOKENS:
        if tok in cid:
            return tok, None
    return "", None


PERF_PATH_NAME = "PERFORMANCE.md"
_PERF_RE = re.compile(r"<!-- STATS_JSON\n(.*?)\n-->", re.S)


def _load_perf():
    p = LOG_DIR / PERF_PATH_NAME
    if p.exists():
        m = _PERF_RE.search(p.read_text())
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return {"days": {}, "recent": []}


def _update_performance(trips_today, sig_idx, today):
    """Idempotently fold today's closed trips into the cumulative ledger.
    Day-level aggregates are REPLACED (not incremented) so re-running the
    journal the same day can never double-count."""
    perf = _load_perf()
    day = {"trips": 0, "wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0,
           "pnl": 0.0, "fees": 0.0, "pnl_net": 0.0, "combos": {}}
    for t in trips_today:
        rec = sig_idx.get(t["entry_id"]) or {}
        pattern = t.get("pattern") or rec.get("pattern") or "unknown"
        net = t.get("pnl_net", t["pnl"])
        fee = t.get("fee_est", 0.0)
        name = f"{t['symbol']}|{pattern}"
        c = day["combos"].get(name, {"trips": 0, "wins": 0, "losses": 0, "gross_win": 0.0,
                                     "gross_loss": 0.0, "pnl": 0.0, "fees": 0.0, "pnl_net": 0.0})
        for target in (c, day):
            target["trips"] += 1
            target["pnl"] = round(target["pnl"] + t["pnl"], 4)
            target["fees"] = round(target["fees"] + fee, 4)
            target["pnl_net"] = round(target["pnl_net"] + net, 4)
            if net > 0:
                target["wins"] += 1; target["gross_win"] = round(target["gross_win"] + net, 4)
            elif net < 0:
                target["losses"] += 1; target["gross_loss"] = round(target["gross_loss"] + net, 4)
        day["combos"][name] = c

    perf.setdefault("days", {})[today] = day

    perf["recent"] = [r for r in perf.get("recent", []) if r.get("date") != today]
    for t in trips_today:
        rec = sig_idx.get(t["entry_id"]) or {}
        perf["recent"].append({"date": today, "symbol": t["symbol"],
                               "pattern": t.get("pattern") or rec.get("pattern") or "unknown",
                               "qty": round(t["qty"], 8), "entry_px": t["entry_px"],
                               "exit_px": t["exit_px"], "pnl": round(t["pnl"], 4)})
    perf["recent"] = perf["recent"][-300:]

    trips = sum(d["trips"] for d in perf["days"].values())
    wins = sum(d["wins"] for d in perf["days"].values())
    losses = sum(d["losses"] for d in perf["days"].values())
    gross = round(sum(d["pnl"] for d in perf["days"].values()), 4)
    fees = round(sum(d.get("fees", 0.0) for d in perf["days"].values()), 4)
    net = round(sum(d.get("pnl_net", d["pnl"]) for d in perf["days"].values()), 4)
    gw = round(sum(d["gross_win"] for d in perf["days"].values()), 4)
    gl = round(sum(d["gross_loss"] for d in perf["days"].values()), 4)
    stats = {
        "trips": trips, "wins": wins, "losses": losses,
        "win_rate": round(100 * wins / trips, 1) if trips else 0.0,
        "gross_pnl": round(gross, 2), "fees_est": round(fees, 2), "net_pnl": round(net, 2),
        "avg_win": round(gw / wins, 2) if wins else 0.0,
        "avg_loss": round(gl / losses, 2) if losses else 0.0,
        "expectancy": round(net / trips, 4) if trips else 0.0,
        "profit_factor": round(gw / abs(gl), 3) if gl else None,
        "days_traded": len([d for d in perf["days"].values() if d["trips"] > 0]),
    }

    from datetime import timedelta as _td
    week_start = (datetime.now() - _td(days=6)).strftime("%Y-%m-%d")
    week = {"trips": 0, "wins": 0, "losses": 0, "net": 0.0}
    for dkey, d in perf["days"].items():
        if dkey >= week_start:
            week["trips"] += d["trips"]; week["wins"] += d["wins"]
            week["losses"] += d["losses"]
            week["net"] = round(week["net"] + d.get("pnl_net", d["pnl"]), 4)

    by_symbol, by_pattern = {}, {}
    for d in perf["days"].values():
        for combo, c in d.get("combos", {}).items():
            sym, _, pat = combo.partition("|")
            for bucket, name in ((by_symbol, sym or "?"), (by_pattern, pat or "?")):
                b = bucket.setdefault(name, {"trips": 0, "wins": 0, "losses": 0,
                                             "pnl": 0.0, "fees": 0.0, "pnl_net": 0.0})
                b["trips"] += c["trips"]; b["wins"] += c["wins"]
                b["losses"] += c["losses"]; b["pnl"] = round(b["pnl"] + c["pnl"], 4)
                b["fees"] = round(b["fees"] + c.get("fees", 0.0), 4)
                b["pnl_net"] = round(b["pnl_net"] + c.get("pnl_net", c["pnl"]), 4)

    by_class = {c: {"trips": 0, "wins": 0, "losses": 0, "pnl": 0.0, "fees": 0.0, "pnl_net": 0.0}
                for c in ("equity", "crypto")}
    for sym, b in by_symbol.items():
        t = by_class[_asset_class(sym)]
        t["trips"] += b["trips"]; t["wins"] += b["wins"]; t["losses"] += b["losses"]
        t["pnl"] = round(t["pnl"] + b["pnl"], 4)
        t["fees"] = round(t["fees"] + b.get("fees", 0.0), 4)
        t["pnl_net"] = round(t["pnl_net"] + b["pnl_net"], 4)

    lines = ["# Cumulative Performance — auto-maintained\n\n",
             f"_Updated {datetime.now().strftime('%Y-%m-%d %H:%M')} · closed round trips only "
             f"(FIFO realized P&L, ET trading days)_\n\n"]
    if stats["trips"]:
        pf = (f"For every $1 lost, it made ${stats['profit_factor']}." if stats["profit_factor"]
              else "Not enough losing trades yet to compute a win/loss money ratio.")
        lines.append(f"**In plain terms:** {stats['trips']} trades closed; {stats['wins']} made money "
                     f"({stats['win_rate']}%). Net result: {stats['net_pnl']:+,.2f} dollars after "
                     f"{stats['fees_est']:,.2f} in fees. On average each trade "
                     f"{'earned' if stats['expectancy'] >= 0 else 'lost'} ${abs(stats['expectancy']):,.2f}. "
                     f"{pf} Split: {by_class['equity']['trips']} stock trades and "
                     f"{by_class['crypto']['trips']} crypto trades (crypto pays ~0.25%/side in fees; "
                     f"stocks are commission-free). A verdict needs at least a week of trades — read the "
                     f"trend, not a single day.\n\n")
    else:
        lines.append("**In plain terms:** no trades have closed yet — the bot is still collecting its "
                     "first data. Check back after a few days.\n\n")
    lines.append("| Metric | Value |\n|--------|-------|\n")
    for k, label in (("days_traded", "Trading days recorded"), ("trips", "Closed round trips"),
                     ("wins", "Wins"), ("losses", "Losses"), ("win_rate", "Win rate % (net)"),
                     ("gross_pnl", "Gross P&L, fill-to-fill $"), ("fees_est", "Estimated fees $"),
                     ("net_pnl", "NET realized P&L $"), ("avg_win", "Avg win $ (net)"),
                     ("avg_loss", "Avg loss $ (net)"), ("expectancy", "Expectancy $/trade (net)"),
                     ("profit_factor", "Profit factor (net)")):
        lines.append(f"| {label} | {stats[k]} |\n")
    lines.append("\n## By asset class\n\n| Class | Trips | Wins | Losses | Win rate % | Gross $ | Fees $ | Net $ |\n"
                 "|-------|-------|------|--------|------------|---------|--------|-------|\n")
    for cls, label in (("equity", "📈 Equities (stocks)"), ("crypto", "₿ Crypto")):
        b = by_class[cls]
        wr = round(100 * b["wins"] / b["trips"], 1) if b["trips"] else 0.0
        lines.append(f"| {label} | {b['trips']} | {b['wins']} | {b['losses']} | {wr} | "
                     f"{b['pnl']:.2f} | {b.get('fees', 0):.2f} | {b['pnl_net']:.2f} |\n")

    lines.append("\n## By symbol\n\n| Symbol | Trips | Wins | Losses | Gross $ | Fees $ | Net $ |\n|--------|-------|------|--------|---------|--------|-------|\n")
    for sym, b in sorted(by_symbol.items(), key=lambda kv: kv[1]['pnl_net'], reverse=True):
        lines.append(f"| {sym} | {b['trips']} | {b['wins']} | {b['losses']} | {b['pnl']:.2f} | {b.get('fees',0):.2f} | {b['pnl_net']:.2f} |\n")
    lines.append("\n## By pattern\n\n| Pattern | Trips | Wins | Losses | Gross $ | Fees $ | Net $ |\n|---------|-------|------|--------|---------|--------|-------|\n")
    for pat, b in sorted(by_pattern.items(), key=lambda kv: kv[1]['pnl_net'], reverse=True):
        lines.append(f"| {pat} | {b['trips']} | {b['wins']} | {b['losses']} | {b['pnl']:.2f} | {b.get('fees',0):.2f} | {b['pnl_net']:.2f} |\n")
    lines.append("\n## Last 20 closed trips\n\n| Date | Symbol | Pattern | Qty | Entry | Exit | P&L $ |\n|------|--------|---------|-----|-------|------|-------|\n")
    for t in perf["recent"][-20:]:
        lines.append(f"| {t['date']} | {t['symbol']} | {t.get('pattern','?')} | {t['qty']:g} | "
                     f"{t['entry_px']:g} | {t['exit_px']:g} | {t['pnl']:.2f} |\n")
    lines.append(f"\n<!-- STATS_JSON\n{json.dumps(perf)}\n-->\n")
    (LOG_DIR / PERF_PATH_NAME).write_text("".join(lines))
    return stats, week, by_class


def run_journal():
    log("=" * 60, "JOURNAL")
    log("Post-Market Daily Journal", "JOURNAL")
    log("=" * 60, "JOURNAL")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    journal_path = LOG_DIR / f"journal-{today}.md"
    signals_path = LOG_DIR / f"signals-{today}.json"

    signals_today = []
    if signals_path.exists():
        try:
            with open(signals_path) as f:
                signals_today = json.load(f)
        except Exception:
            signals_today = []

    orders_raw = run_alpaca(["order", "list", "--status", "all", "--limit", "500"])
    orders = (orders_raw.get("orders", orders_raw) if isinstance(orders_raw, dict)
              else (orders_raw if isinstance(orders_raw, list) else []))
    truncated = len(orders) >= 500
    today_orders = [o for o in orders if _et_date(o.get("created_at")) == today]

    account = run_alpaca(["account", "get"]) or {}
    positions = run_alpaca(["position", "list"]) or []
    if not isinstance(positions, list):
        positions = []

    # Realized P&L: FIFO over ALL fetched fills; today's trips bucketed by EXIT date (ET)
    trips_all, open_lots, warnings = _fifo_round_trips(orders)
    trips_today = [t for t in trips_all if _et_date(t["exit_t"]) == today]

    # Attribute pattern + exit reason per trip: signal ledger first (richer record),
    # then straight from the order ids (survives lost signals files).
    sig_idx = {}
    for s in signals_today:
        if isinstance(s, dict) and s.get("order_id"):
            sig_idx[s["order_id"]] = s

    for t in trips_all:
        rec = sig_idx.get(t["entry_id"]) or {}
        pat, _ = _attr_from_cid(t.get("entry_cid", ""))
        t["pattern"] = rec.get("pattern") or pat or "unknown"
        erec = sig_idx.get(t["exit_id"]) or {}
        _, label = _attr_from_cid(t.get("exit_cid", ""))
        if not label:
            ep = erec.get("pattern") or _attr_from_cid(t.get("exit_cid", ""))[0] or ""
            label = f"signal: {ep}" if ep else "unknown"
        t["exit_reason"] = label

    def exit_reason(t):
        return t.get("exit_reason") or "unknown"

    realized = sum(t["pnl"] for t in trips_today)
    fees_today = sum(t.get("fee_est", 0.0) for t in trips_today)
    realized_net = sum(t.get("pnl_net", t["pnl"]) for t in trips_today)
    wins = [t for t in trips_today if t.get("pnl_net", t["pnl"]) > 0]
    losses = [t for t in trips_today if t.get("pnl_net", t["pnl"]) < 0]
    unreal = 0.0
    for p in positions:
        try:
            unreal += float(p.get("unrealized_pl", 0) or 0)
        except (TypeError, ValueError):
            pass

    try:
        cum, week, by_class = _update_performance(trips_today, sig_idx, today)
    except Exception as e:
        log(f"performance ledger error: {e}", "JOURNAL")
        cum, week, by_class = {}, {}, {}

    # ── signal funnel ──
    _FKEYS = ("entries_signalled", "entries_traded", "exits_traded", "skipped_rrr",
              "skipped_confluence", "skipped_cap", "skipped_window", "no_position_to_sell",
              "market_closed", "exits_monitor")
    f = {"equity": {k: 0 for k in _FKEYS}, "crypto": {k: 0 for k in _FKEYS}}
    entries_by_symbol = {}
    for s in signals_today:
        cls = _asset_class(s.get("symbol", ""))
        p = (s.get("pattern") or "")
        if p.startswith("EXIT_"):
            f[cls]["exits_monitor"] += 1
            continue
        if "bullish" in p:
            f[cls]["entries_signalled"] += 1
            if s.get("traded"):
                f[cls]["entries_traded"] += 1
                entries_by_symbol[s.get("symbol")] = entries_by_symbol.get(s.get("symbol"), 0) + 1
        elif s.get("traded"):
            f[cls]["exits_traded"] += 1
        sk = s.get("skipped")
        if sk == "rrr": f[cls]["skipped_rrr"] += 1
        elif sk == "confluence": f[cls]["skipped_confluence"] += 1
        elif sk == "daily_cap": f[cls]["skipped_cap"] += 1
        elif sk == "outside_entry_window": f[cls]["skipped_window"] += 1
        elif sk == "no_position": f[cls]["no_position_to_sell"] += 1
        elif sk == "market_closed": f[cls]["market_closed"] += 1
    ft = {k: f["equity"][k] + f["crypto"][k] for k in _FKEYS}

    # ── integrity checks ──
    try:
        sanity = float(account.get("cash", 0) or 0) + sum(
            float(p.get("market_value", 0) or 0) for p in positions if isinstance(p, dict))
        eq = float(account.get("equity", 0) or 0)
        if abs(sanity - eq) > 1.0:
            warnings.append(f"cash+positions ${sanity:.2f} != equity ${eq:.2f} (pending fills?)")
    except Exception:
        pass
    if truncated:
        warnings.append("orders fetch hit the 500-row limit — older history not scanned")
    try:
        pos_qty = {_norm_sym(p.get("symbol", "")): float(p.get("qty", 0) or 0)
                   for p in positions if isinstance(p, dict)}
        lot_qty = {}
        for l in open_lots:
            k = _norm_sym(l["symbol"])
            lot_qty[k] = lot_qty.get(k, 0.0) + l["qty"]
        for k, lq in lot_qty.items():
            got = pos_qty.get(k)
            if got is None or lq <= 0:
                continue
            gap = 100 * (got - lq) / lq
            if gap < -0.02:
                warnings.append(f"quantity gap {k}: ledger lots {lq:g} vs live position {got:g} "
                                f"({gap:+.3f}% — expected: crypto taker fee is charged in the asset)")
            elif gap > 0.02:
                warnings.append(f"quantity SURPLUS {k}: ledger lots {lq:g} vs live position {got:g} "
                                f"({gap:+.3f}% — a buy may be missing from the ledger)")
    except Exception:
        pass

    rejected = [o for o in today_orders if o.get("status") == "rejected"]
    expired = [o for o in today_orders if o.get("status") == "expired"]
    pending = [o for o in today_orders if o.get("status") in ("new", "accepted", "open", "held", "pending_new", "partially_filled")]

    # ── plain-English layer (for non-technical reading) ──
    def _m(x):
        return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"

    _why_map = {"STOP-LOSS": "it fell to the safety stop-loss",
                "TAKE-PROFIT": "it reached the profit target"}

    def _whyof(t):
        r = exit_reason(t)
        return _why_map.get(r, f"a reverse candlestick signal ended the move ({r})")

    plain = []
    if trips_today:
        n_eq = sum(1 for t in trips_today if _asset_class(t["symbol"]) == "equity")
        n_cr = len(trips_today) - n_eq
        plain.append(f"The bot closed {len(trips_today)} trade(s) today ({n_eq} stock, {n_cr} crypto): "
                     f"{len(wins)} made money, {len(losses)} lost money. Net result {_m(realized_net)} after "
                     f"about ${fees_today:,.2f} in fees.")
        best = max(trips_today, key=lambda t: t.get("pnl_net", t["pnl"]))
        plain.append(f"Best trade: {best['symbol']} {_m(best.get('pnl_net', best['pnl']))} — {_whyof(best)}.")
        if len(trips_today) > 1:
            worst = min(trips_today, key=lambda t: t.get("pnl_net", t["pnl"]))
            plain.append(f"Worst trade: {worst['symbol']} {_m(worst.get('pnl_net', worst['pnl']))} — {_whyof(worst)}.")
    else:
        tail = f" It is still holding {len(positions)} open position(s)." if positions else ""
        plain.append("The bot didn't close any trades today." + tail)

    if ft["entries_signalled"]:
        because = []
        if ft["skipped_rrr"]: because.append(f"{ft['skipped_rrr']} didn't promise enough profit for the risk")
        if ft["skipped_confluence"]: because.append(f"{ft['skipped_confluence']} lacked confirmation from other signals")
        if ft["skipped_cap"]: because.append(f"{ft['skipped_cap']} hit the 3-entries-per-symbol daily limit")
        if ft["skipped_window"]: because.append(f"{ft['skipped_window']} were outside the crypto trading hours")
        if ft["market_closed"]: because.append(f"{ft['market_closed']} appeared while the stock market was closed")
        extra = ("; it passed on the rest because " + "; ".join(because) + ".") if because else "."
        plain.append(f"It saw {ft['entries_signalled']} buy setup(s) — {f['equity']['entries_signalled']} on "
                     f"stocks, {f['crypto']['entries_signalled']} on crypto — and acted on "
                     f"{ft['entries_traded']}" + extra)

    if positions:
        try:
            mv = sum(float(p.get("market_value", 0) or 0) for p in positions)
        except Exception:
            mv = 0.0
        plain.append(f"At the end of the day it holds {len(positions)} position(s) worth about ${mv:,.0f}, "
                     f"currently {_m(unreal)} {'up' if unreal >= 0 else 'down'} on paper.")
    if cum and cum.get("trips"):
        plain.append(f"Since records began: {cum['trips']} trades closed, {cum['wins']} winners "
                     f"({cum['win_rate']}%), net {_m(cum['net_pnl'])} after fees.")
    else:
        plain.append("No trades have closed yet since records began — the first days are data collection, "
                     "not a verdict.")
    if week and week.get("trips"):
        plain.append(f"Last 7 days: {week['trips']} trades, {week['wins']} winners, net {_m(week['net'])}.")
    plain.append("Every technical term below is decoded in READING-GUIDE.md at the repo root.")

    # ── render ──
    L = []
    acct_no = account.get("account_number", "?")
    L.append(f"# Daily Trading Journal — {today}\n\n")
    L.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ET · account {acct_no} · "
             f"engine: candlestick top-down (1Week/1Day/15Min) · exits first, entries second_\n\n")
    L.append(f"_Scope: this account runs TWO loops in one journal — **equities** (8 stock symbols, market "
             f"hours, 5% risk, no fees) and **crypto** (BTC/ETH, non-market hours, 3% risk, ~0.25%/side fees). "
             f"Every table below is split or labelled by asset class._\n\n")

    L.append("## Read this first (plain English)\n\n")
    for p in plain:
        L.append(f"- {p}\n")
    L.append("\n")

    L.append("## 1. Account\n\n| Metric | Value |\n|--------|-------|\n")
    for label, key in (("Equity", "equity"), ("Cash", "cash"), ("Buying power", "buying_power"),
                       ("Previous close baseline", "last_equity")):
        L.append(f"| {label} | ${account.get(key, '?')} |\n")
    try:
        L.append(f"| Daily change vs baseline | ${float(account.get('equity',0)) - float(account.get('last_equity',0)):+.2f} |\n")
    except Exception:
        pass
    L.append(f"| Realized today, gross fill-to-fill | ${realized:+.2f} |\n")
    L.append(f"| Estimated fees today (crypto maker {CRYPTO_MAKER_FEE_RATE*100:.2f}% / taker {CRYPTO_FEE_RATE*100:.2f}%) | ${-fees_today:+.2f} |\n")
    L.append(f"| Realized today, NET of fees | ${realized_net:+.2f} |\n")
    L.append(f"| Unrealized on open positions | ${unreal:+.2f} |\n\n")

    L.append("## 2. Closed round trips today (realized)\n\n")
    if trips_today:
        L.append("| Symbol | Asset | Qty | Entry (time/px) | Exit (time/px) | Held | P&L gross $ | Fee $ | "
                 "P&L net $ | P&L % | Exit reason | Pattern | Conf | Mult | Risk $ | RRR | Slip % |\n")
        L.append("|--------|-------|-----|-----------------|----------------|------|-------------|-------|"
                 "-----------|-------|-------------|---------|------|------|--------|-----|--------|\n")
        for t in trips_today:
            er = sig_idx.get(t["entry_id"]) or {}
            sig_px = er.get("price")
            slip = f"{(t['entry_px'] - sig_px) / sig_px * 100:+.3f}" if sig_px else "?"
            L.append(f"| {t['symbol']} | {_asset_class(t['symbol'])} | {t['qty']:g} | {str(t['entry_t'])[11:16]}/{t['entry_px']:g} | "
                     f"{str(t['exit_t'])[11:16]}/{t['exit_px']:g} | {_held_span(t['entry_t'], t['exit_t'])} | "
                     f"{t['pnl']:+.2f} | {t.get('fee_est',0):.2f} | {t.get('pnl_net', t['pnl']):+.2f} | "
                     f"{t['pnl_pct']:+.2f} | {exit_reason(t)} | {t.get('pattern') or er.get('pattern','?')} | "
                     f"{er.get('confluence','?')} | {er.get('confidence_mult','?')} | "
                     f"{er.get('risk_amount','?')} | {er.get('rrr','?')} | {slip} |\n")
        wr = 100 * len(wins) / len(trips_today)
        L.append(f"\n**Today:** {len(trips_today)} closed · {len(wins)}W/{len(losses)}L · win rate {wr:.0f}% · "
                 f"gross ${realized:+.2f} · fees ${-fees_today:+.2f} · **net ${realized_net:+.2f}**\n\n")
    else:
        L.append("*No round trips closed today.*\n\n")

    L.append("## 3. Open positions at close\n\n")
    if positions:
        L.append("| Symbol | Asset | Qty | Entry px | Mark px | Unrealized $ | % | Market value |\n")
        L.append("|--------|-------|-----|----------|---------|--------------|---|--------------|\n")
        for p in positions:
            L.append(f"| {p.get('symbol')} | {_asset_class(p.get('symbol',''))} | {p.get('qty')} | {p.get('avg_entry_price')} | "
                     f"{p.get('current_price')} | {p.get('unrealized_pl')} | "
                     f"{float(p.get('unrealized_plpc',0))*100:+.2f} | {p.get('market_value')} |\n")
        L.append("\n")
    else:
        L.append("*No open positions.*\n\n")

    L.append("## 4. Signal funnel — equities vs crypto (why we did / didn't trade)\n\n")
    L.append("| Stage | 📈 Equities | ₿ Crypto | Combined |\n|-------|-------------|----------|----------|\n")
    for label, key in (("Entry signals detected", "entries_signalled"),
                       ("→ entries executed", "entries_traded"),
                       ("→ skipped: risk/reward below threshold", "skipped_rrr"),
                       ("→ skipped: insufficient confluence", "skipped_confluence"),
                       ("→ blocked: daily cap (3/symbol/24h)", "skipped_cap"),
                       ("→ blocked: outside crypto window", "skipped_window"),
                       ("Exit signals (bearish on owned)", "exits_traded"),
                       ("Monitor exits (stop-loss/take-profit)", "exits_monitor"),
                       ("Skipped: no position to sell", "no_position_to_sell"),
                       ("Skipped: market closed (equities)", "market_closed")):
        L.append(f"| {label} | {f['equity'][key]} | {f['crypto'][key]} | {ft[key]} |\n")
    if entries_by_symbol:
        L.append("\n**Entries today vs cap:** " + " · ".join(
            f"{k} {v}/{MAX_TRADES_PER_SYMBOL}" for k, v in sorted(entries_by_symbol.items())) + "\n")
    L.append("\n")

    L.append("## 5. Orders placed but not filled\n\n")
    if rejected or expired or pending:
        L.append("| Symbol | Side | Qty | Status | Reason |\n|--------|------|-----|--------|--------|\n")
        for o in rejected + expired + pending:
            L.append(f"| {o.get('symbol')} | {o.get('side')} | {o.get('qty')} | {o.get('status')} | "
                     f"{(o.get('rejection_reason') or '')[:80]} |\n")
        L.append("\n")
    else:
        L.append("*None — every submitted order filled.*\n\n")

    L.append("## 6. Cumulative performance (all closed trips, this account)\n\n")
    if cum:
        L.append(f"| Trips | Wins | Losses | Win rate % | Gross $ | Fees $ | NET $ | Avg win $ | "
                 f"Avg loss $ | Expectancy $/trade | Profit factor |\n")
        L.append("|-------|------|--------|------------|---------|--------|-------|-----------|"
                 "------------|-------------------|---------------|\n")
        L.append(f"| {cum.get('trips')} | {cum.get('wins')} | {cum.get('losses')} | {cum.get('win_rate')} | "
                 f"{cum.get('gross_pnl')} | {cum.get('fees_est')} | {cum.get('net_pnl')} | "
                 f"{cum.get('avg_win')} | {cum.get('avg_loss')} | "
                 f"{cum.get('expectancy')} | {cum.get('profit_factor')} |\n\n")
        L.append("_Full breakdown (by asset class, symbol, pattern, last 20 trips): see `runs/PERFORMANCE.md`._\n\n")
        if by_class:
            L.append("**By asset class (since records began)**\n\n")
            L.append("| Class | Trips | Wins | Losses | Win rate % | Gross $ | Fees $ | Net $ |\n")
            L.append("|-------|-------|------|--------|------------|---------|--------|-------|\n")
            for cls, label in (("equity", "📈 Equities (stocks)"), ("crypto", "₿ Crypto")):
                b = by_class.get(cls, {})
                if not b:
                    continue
                wr = round(100 * b.get("wins", 0) / b["trips"], 1) if b.get("trips") else 0.0
                L.append(f"| {label} | {b.get('trips', 0)} | {b.get('wins', 0)} | {b.get('losses', 0)} | "
                         f"{wr} | {b.get('pnl', 0):.2f} | {b.get('fees', 0):.2f} | {b.get('pnl_net', 0):.2f} |\n")
            L.append("\n")
    else:
        L.append("*Ledger unavailable this run.*\n\n")

    L.append("## 7. Data-integrity checks\n\n")
    if warnings:
        for w in warnings:
            L.append(f"- ⚠ {w}\n")
    else:
        L.append("- ✅ all fills matched, no partial fills, cash+positions == equity\n")
    if open_lots:
        L.append(f"- open lots carried: " + ", ".join(
            f"{l['symbol']} {l['qty']:g}@{l['px']:g}" for l in open_lots) + "\n")
    L.append("\n")

    mistakes, lessons = [], []
    if not today_orders:
        mistakes.append("No trades today" + (" (signals detected)" if signals_today else ""))
    if all(o.get("side") == "sell" for o in today_orders) and today_orders:
        mistakes.append("Sell-only day — no bullish entries executed")
    if ft["skipped_cap"]:
        mistakes.append(f"{ft['skipped_cap']} signal(s) blocked by the daily cap — candidate for a "
                        f"quality-scaled cap if cumulative expectancy stays positive")
    for o in rejected:
        mistakes.append(f"Rejected: {o.get('symbol')} {(o.get('rejection_reason') or '')[:70]}")
    if expired:
        mistakes.append(f"{len(expired)} order(s) expired")
    if not warnings and not mistakes:
        lessons.append("Clean day — no anomalies")
    lessons.append("Judge the strategy only from PERFORMANCE.md after >=1 week of data (frequency "
                   "decisions need win rate + expectancy, not single-day P&L)")

    L.append("## 8. Mistakes / flags\n\n")
    for m in mistakes or ["None detected."]:
        L.append(f"- {m}\n")
    L.append("\n## 9. Lessons / next focus\n\n")
    for x in lessons:
        L.append(f"- {x}\n")
    L.append("\n---\n_Bot-generated. Numbers are FIFO-realized from actual fills; unrealized marks are separate._\n")

    with open(journal_path, "w") as fh:
        fh.writelines(L)
    log(f"Journal: {journal_path}", "JOURNAL")
    log(f"Realized today: ${realized:+.2f} | {len(trips_today)} closed | "
        f"{len(warnings)} integrity flags", "JOURNAL")


# ─── Entry Points ────────────────────────────────────────────────────────────

def main():
    if "--journal" in sys.argv:
        run_journal()
        return

    log("=" * 60, "INIT")
    log("Alpaca Autonomous Trader — Candlestick Bible Strategies", "INIT")
    log("=" * 60, "INIT")

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Verify backend
    backend_ok = False
    try:
        r = subprocess.run([ALPACA_CLI, "doctor"], capture_output=True, text=True, timeout=10)
        backend_ok = r.returncode == 0
    except Exception:
        pass

    if not backend_ok:
        try:
            from bot import rest_client as _rc
        except ImportError:
            import rest_client as _rc
        if _rc.has_credentials():
            backend_ok = True
            log("Using REST API backend", "INIT")

    if not backend_ok:
        log("No backend available. Set APCA_ACCESS_TOKEN or install CLI.", "FATAL")
        sys.exit(1)

    log("Backend OK", "INIT")

    # ── Crypto mode: 24/7 trading, no market-hours gate, no journal ──
    if "--crypto" in sys.argv:
        log("CRYPTO MODE — 24/7 trading (BTC/ETH never close)", "MARKET")
        try:
            _cancel_stale_crypto_limits()  # resting limits older than TTL
        except Exception as e:
            log(f"Limit TTL sweep error: {e}", "ERROR")
        try:
            manage_open_positions()  # stop-loss / take-profit first
        except Exception as e:
            log(f"Exit monitor error: {e}", "ERROR")
        _CYCLE_SIDES.clear()
        for symbol in CRYPTO_SYMBOLS:
            try:
                analyze_and_trade(symbol)
            except Exception as e:
                log(f"Error on {symbol}: {e}", "ERROR")
                import traceback
                log(traceback.format_exc(), "ERROR")
        log("Crypto cycle complete.", "DONE")
        return

    if is_market_session():
        log("Market OPEN — trading", "MARKET")
    elif is_after_market_close():
        log("Market CLOSED — journal", "MARKET")
        run_journal()
        return
    else:
        log("Outside hours — analysis only", "MARKET")

    try:
        manage_open_positions()  # stop-loss / take-profit first
    except Exception as e:
        log(f"Exit monitor error: {e}", "ERROR")

    _CYCLE_SIDES.clear()
    for symbol in SYMBOLS:
        try:
            analyze_and_trade(symbol)
        except Exception as e:
            log(f"Error on {symbol}: {e}", "ERROR")
            import traceback
            log(traceback.format_exc(), "ERROR")

    log("Done.", "DONE")


if __name__ == "__main__":
    main()