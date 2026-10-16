"""
REST API client for Alpaca Trading API.
Fallback when the Alpaca CLI is not available (e.g. GitHub Actions).

Supports two auth methods (checked in order):
  1. Bearer token from APCA_ACCESS_TOKEN env var
  2. Basic auth from APCA_API_KEY_ID + APCA_API_SECRET_KEY env vars
"""

import base64
import json
import os
from urllib.request import Request, urlopen, HTTPError
from urllib.parse import urlencode

PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

# ─── Auth ────────────────────────────────────────────────────────────────────

ACCESS_TOKEN = os.environ.get("APCA_ACCESS_TOKEN", "")
API_KEY = os.environ.get("APCA_API_KEY_ID", "") or os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY", "") or os.environ.get("ALPACA_SECRET_KEY", "")

def _headers():
    h = {"Content-Type": "application/json"}
    if ACCESS_TOKEN:
        h["Authorization"] = f"Bearer {ACCESS_TOKEN}"
    elif API_KEY and SECRET_KEY:
        # Alpaca API uses custom headers: APCA-API-KEY-ID / APCA-API-SECRET-KEY
        h["APCA-API-KEY-ID"] = API_KEY
        h["APCA-API-SECRET-KEY"] = SECRET_KEY
    return h


def has_credentials():
    return bool(ACCESS_TOKEN or (API_KEY and SECRET_KEY))


def _request(method, base, path, query=None, body=None):
    url = f"{base}{path}"
    if query:
        query = {k: v for k, v in query.items() if v is not None}
        url += "?" + urlencode(query)

    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, headers=_headers(), method=method)

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            if raw:
                return json.loads(raw)
            return {}
    except HTTPError as e:
        err = e.read().decode()[:400]
        return {"_error": f"HTTP {e.code}: {err}", "_http_code": e.code}
    except Exception as e:
        return {"_error": str(e)}


# ─── API Methods ─────────────────────────────────────────────────────────────

def get_account():
    return _request("GET", PAPER_BASE, "/v2/account")


def get_clock():
    return _request("GET", PAPER_BASE, "/v2/clock")


def get_positions():
    r = _request("GET", PAPER_BASE, "/v2/positions")
    if isinstance(r, dict) and "_error" in r:
        return []
    return r if isinstance(r, list) else []


def get_bars(symbol, start, end, timeframe="1Day"):
    path = f"/v2/stocks/{symbol}/bars"
    q = {"start": start, "end": end, "timeframe": timeframe}
    r = _request("GET", DATA_BASE, path, query=q)
    if isinstance(r, dict) and "_error" not in r:
        return r
    return None


def get_orders(params=None):
    r = _request("GET", PAPER_BASE, "/v2/orders", query=params or {})
    if isinstance(r, dict) and "_error" in r:
        return []
    if isinstance(r, dict) and "orders" in r:
        return r["orders"]
    return r if isinstance(r, list) else []


def submit_order(symbol, side, qty, order_type="market", tif="day", **kwargs):
    body = {
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "type": order_type,
        "time_in_force": tif,
    }
    body["client_order_id"] = kwargs.get("client_order_id")
    for key in ("limit_price", "stop_price", "extended_hours"):
        val = kwargs.get(key)
        if val is not None:
            body[key] = str(val)
    body = {k: v for k, v in body.items() if v is not None}
    return _request("POST", PAPER_BASE, "/v2/orders", body=body)


def cancel_order(order_id):
    return _request("DELETE", PAPER_BASE, f"/v2/orders/{order_id}")


def doctor_check():
    acct = get_account()
    if "_error" in acct:
        return False, acct.get("_error", "unknown")
    status = acct.get("status", "")
    return status in ("ACTIVE", "PAPER_ONLY"), acct


def rest_call(args, quiet=True):
    """
    CLI-style dispatcher for REST API.
    Accepts same arg list as CLI (e.g. ["account", "get"]) and returns parsed JSON.
    """
    import sys
    cmd = ' '.join(args) if isinstance(args, list) else str(args)
    cmd_lower = cmd.lower()

    try:
        if 'account' in cmd_lower and 'get' in cmd_lower:
            return get_account()
        if 'clock' in cmd_lower:
            return get_clock()
        if 'position' in cmd_lower and 'list' in cmd_lower:
            return get_positions()
        if 'order' in cmd_lower and 'list' in cmd_lower:
            return get_orders()
        if 'order' in cmd_lower and 'cancel' in cmd_lower:
            oid = None
            for i, a in enumerate(args):
                if a == '--order-id' and i + 1 < len(args):
                    oid = args[i + 1]
            if oid:
                return cancel_order(oid)
            return {"_error": "missing order-id"}
        if 'order' in cmd_lower and 'submit' in cmd_lower:
            kwargs = {}
            symbol, side, qty = None, None, None
            order_type, tif = "market", "day"
            for i, a in enumerate(args):
                if a == '--symbol' and i+1 < len(args): symbol = args[i+1]
                elif a == '--side' and i+1 < len(args): side = args[i+1]
                elif a == '--qty' and i+1 < len(args): qty = args[i+1]
                elif a == '--type' and i+1 < len(args): order_type = args[i+1]
                elif a == '--time-in-force' and i+1 < len(args): tif = args[i+1]
                elif a == '--limit-price' and i+1 < len(args): kwargs['limit_price'] = args[i+1]
                elif a == '--stop-price' and i+1 < len(args): kwargs['stop_price'] = args[i+1]
            if symbol and side and qty:
                return submit_order(symbol, side, qty, order_type, tif, **kwargs)
            return {"_error": "missing required order params"}
        if 'data' in cmd_lower and 'bars' in cmd_lower:
            symbol, start, end, timeframe = None, None, None, "1Day"
            for i, a in enumerate(args):
                if a == '--symbol' and i+1 < len(args): symbol = args[i+1]
                elif a == '--start' and i+1 < len(args): start = args[i+1]
                elif a == '--end' and i+1 < len(args): end = args[i+1]
                elif a == '--timeframe' and i+1 < len(args): timeframe = args[i+1]
            if symbol and start and end:
                return get_bars(symbol, start, end, timeframe)
            return {"_error": "missing required bar params"}

        return {"_error": f"unknown command: {cmd[:80]}"}

    except Exception as e:
        return {"_error": str(e)}