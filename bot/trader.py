#!/usr/bin/env python3
"""
Alpaca Autonomous Trader — Candlestick Trading Bible strategies.
Runs as a scheduled job during US market hours (9:30 AM - 4:00 PM ET).
Supports Alpaca CLI (local) or REST API (GitHub Actions).
"""

import json
import math
import os
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
    result = _rest_call(args, quiet)
    if result and "_error" not in result:
        return result
    log(f"REST also failed: {result.get('_error', '?')[:120]}", "CLI")
    return None


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

# ─── Helpers ─────────────────────────────────────────────────────────────────

def log(msg, section="TRADER"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [{section}] {msg}", flush=True)


# ─── Data Fetching ───────────────────────────────────────────────────────────

def fetch_bars(symbol, days=15, timeframe="1Day"):
    """Fetch OHLC bars for given timeframe. Returns raw API bars list."""
    end = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    result = run_alpaca(["data", "bars", "--symbol", symbol,
                         "--start", start, "--end", end,
                         "--timeframe", timeframe])
    if result and "bars" in result and len(result["bars"]) >= 2:
        return result["bars"]

    # Shorter range fallback for higher timeframes
    for d in [min(days, 10), 7, 5, 3]:
        s = (datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y-%m-%d")
        result = run_alpaca(["data", "bars", "--symbol", symbol,
                             "--start", s, "--end", end,
                             "--timeframe", timeframe])
        if result and "bars" in result and len(result["bars"]) >= 2:
            return result["bars"]
    return []


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
    touches = defaultdict(int)
    for bar in bars:
        for val in [bar["high"], bar["low"]]:
            touches[round(val / 5) * 5] += 1
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

    return max(0.25, min(mult, 5.0))  # clamp 0.25x - 5.0x


def calc_position_size(account, risk_pct, confidence_mult=1.0):
    equity = float(account.get("equity", 5000))
    cash = float(account.get("cash", 5000))
    max_risk = equity * (risk_pct / 100) * confidence_mult
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


def place_order(symbol, side, qty, order_type="market", tif="day", limit_price=None, stop_price=None):
    args = ["order", "submit", "--symbol", symbol, "--side", side,
            "--qty", str(qty), "--type", order_type, "--time-in-force", tif]
    if limit_price:
        args.extend(["--limit-price", str(limit_price)])
    if stop_price:
        args.extend(["--stop-price", str(stop_price)])
    log(f"Order: --symbol {symbol} --side {side} --qty {qty} --type {order_type}", "ORDER")
    result = run_alpaca(args)
    if result:
        log(f"Placed: ID={result.get('id','?')} Status={result.get('status','?')}", "ORDER")
    else:
        log("Order failed", "ORDER")
    return result


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

def analyze_and_trade(symbol):
    """Full top-down multi-timeframe analysis per the book.
    1. Weekly/daily: market structure, regime, S/R levels
    2. 4H: entry signals at key levels
    3. Trade only when all timeframes align."""
    log(f"Top-down analysis of {symbol}...", "ANALYSIS")

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
    entry_bars_raw = fetch_bars(symbol, days=entry_days, timeframe=TF_ENTRY)

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
            "price": candle["close"], "traded": False
        }

        log(f"  {pattern_type} | conf {cc} [{','.join(factors)}] | trend {trend_dir}", "SIGNAL")

        if cc < MIN_CONFLUENCE:
            log(f"  skip (need {MIN_CONFLUENCE} conf)", "SIGNAL")
            _save_signal(signals_path, sig_record)
            continue

        if pattern_type in ("bullish_pin_bar", "bullish_engulfing", "morning_star", "bullish_inside_bar"):
            side = "buy"
        elif pattern_type in ("bearish_pin_bar", "bearish_engulfing", "evening_star", "bearish_inside_bar"):
            side = "sell"
        else:
            _save_signal(signals_path, sig_record)
            continue

        if not is_market_session():
            log("  market closed — analysis only", "MARKET")
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
        pos_value, _ = calc_position_size(account, MAX_RISK_PCT, confidence_mult)
        qty = max(1, int(pos_value / candle["close"]))

        log(f"  Conf mult: {confidence_mult:.1f}x | Risk: ${pos_value:.0f} | {side.upper()} {qty} @ ${candle['close']:.2f} stop ${stop:.2f} target ${target:.2f} RRR {rrr:.2f}", "TRADE")

        # Store confidence info in signal record
        sig_record["confidence_mult"] = round(confidence_mult, 2)
        sig_record["risk_amount"] = round(pos_value, 2)
        sig_record["rrr"] = round(rrr, 2)
        sig_record["qty"] = qty

        if qty < 1:
            _save_signal(signals_path, sig_record)
            continue

        if rrr < MIN_RRR:
            log(f"  skip RRR {rrr:.2f} < {MIN_RRR}", "TRADE")
            _save_signal(signals_path, sig_record)
            continue

        result = place_order(symbol, side, qty, ORDER_TYPE, TIF)
        if result:
            sig_record["traded"] = True
            sig_record["order_id"] = result.get("id", "")
            sig_record["order_status"] = result.get("status", "")
            log(f"  done ID={result.get('id','?')[:12]}", "TRADE")
        else:
            sig_record["error"] = "order_failed"
        _save_signal(signals_path, sig_record)


# ─── Daily Journal ───────────────────────────────────────────────────────────

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
        with open(signals_path) as f:
            signals_today = json.load(f)

    orders_raw = run_alpaca(["order", "list"])
    orders = (orders_raw.get("orders", orders_raw) if isinstance(orders_raw, dict)
              else (orders_raw if isinstance(orders_raw, list) else []))
    today_orders = [o for o in orders if today in o.get("created_at", "")]

    account = run_alpaca(["account", "get"]) or {}
    positions = run_alpaca(["position", "list"]) or []
    if not isinstance(positions, list):
        positions = []

    filled = [o for o in today_orders if o.get("status") == "filled"]
    rejected = [o for o in today_orders if o.get("status") == "rejected"]
    open_ords = [o for o in today_orders if o.get("status") in ("new", "accepted", "open", "held")]
    expired = [o for o in today_orders if o.get("status") == "expired"]

    total_pl = 0.0
    trade_details = []
    for o in filled:
        side = o.get("side", "")
        qty = float(o.get("filled_qty", 0) or 0)
        fp = float(o.get("filled_avg_price", 0) or float(o.get("filled_price", 0) or 0))
        pl = (fp - fp) * qty  # paper: entry = fill for simplicity
        total_pl += pl
        trade_details.append({"id": o.get("id","?")[:12], "symbol": o.get("symbol","?"),
                              "side": side, "qty": qty, "pl": round(pl,2), "status": "filled"})
    for o in expired:
        trade_details.append({"id": o.get("id","?")[:12], "symbol": o.get("symbol","?"),
                              "side": o.get("side","?"), "qty": o.get("qty",0),
                              "status": "expired", "pl": 0.0, "note": "expired"})
    for o in rejected:
        trade_details.append({"id": o.get("id","?")[:12], "symbol": o.get("symbol","?"),
                              "side": o.get("side","?"), "qty": o.get("qty",0),
                              "status": "rejected", "pl": 0.0,
                              "note": o.get("rejection_reason", "rejected")})

    mistakes, lessons = [], []
    sides_traded = [o.get("side","") for o in today_orders]

    if not today_orders:
        m = "Signals detected but no trades" if signals_today else "No signals detected"
        mistakes.append(m)
        lessons.append("Review thresholds and symbol selection" if signals_today else "Check data feed or add symbols")

    if all(s == "sell" for s in sides_traded):
        mistakes.append("Sell-only bias today")
        lessons.append("Watch for bullish reversals to balance detection")

    for o in rejected:
        if "wash" in (o.get("rejection_reason", "") or "").lower():
            mistakes.append(f"Wash trade on {o['symbol']}")
            lessons.append("Check pending orders before opposite-direction trades")

    if expired:
        mistakes.append(f"{len(expired)} order(s) expired")
        lessons.append("Consider GTC time-in-force")

    if not lessons:
        lessons.append("One day not enough — keep monitoring")

    lines = []
    lines.append(f"# Daily Trading Journal — {today}\n\n")
    lines.append(f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_\n\n")
    lines.append("## Account\n\n| Metric | Value |\n|--------|-------|\n")
    lines.append(f"| Equity | ${account.get('equity','?')} |\n")
    lines.append(f"| Cash | ${account.get('cash','?')} |\n")
    lines.append(f"| Buying Power | ${account.get('buying_power','?')} |\n")
    lines.append(f"| Day P&L | ${total_pl:.2f} |\n\n")

    lines.append("## Trades\n\n")
    if trade_details:
        lines.append("| Symbol | Side | Qty | P&L | Status |\n|--------|------|-----|-----|--------|\n")
        for t in trade_details:
            note = f" ({t.get('note','')})" if t.get('note') else ""
            lines.append(f"| {t['symbol']} | {t['side']} | {t['qty']} | ${t.get('pl',0):.2f} | {t['status']}{note} |\n")
    else:
        lines.append("*No trades today.*\n")

    if open_ords:
        lines.append("\n## Open Orders\n\n| Symbol | Side | Qty | Status |\n|--------|------|-----|--------|\n")
        for o in open_ords:
            lines.append(f"| {o['symbol']} | {o['side']} | {o['qty']} | {o['status']} |\n")

    if signals_today:
        lines.append("\n## Patterns Detected\n\n| Symbol | Pattern | Conf | Traded |\n|--------|---------|------|--------|\n")
        for s in signals_today:
            lines.append(f"| {s['symbol']} | {s['pattern']} | {s['confluence']} | {'Yes' if s.get('traded') else 'No'} |\n")

    lines.append("\n## Mistakes\n\n")
    for m in mistakes:
        lines.append(f"- {m}\n")
    lines.append("\n## Lessons\n\n")
    for l in lessons:
        lines.append(f"- {l}\n")
    lines.append("\n---\n_Bot-generated._\n")

    with open(journal_path, 'w') as f:
        f.writelines(lines)
    log(f"Journal: {journal_path}", "JOURNAL")
    pl_e = "Profit" if total_pl >= 0 else "Loss"
    log(f"{pl_e}: ${total_pl:.2f} | {len(trade_details)} trades | {len(mistakes)} mistakes", "JOURNAL")


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

    if is_market_session():
        log("Market OPEN — trading", "MARKET")
    elif is_after_market_close():
        log("Market CLOSED — journal", "MARKET")
        run_journal()
        return
    else:
        log("Outside hours — analysis only", "MARKET")

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