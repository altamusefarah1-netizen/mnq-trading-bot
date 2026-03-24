"""
MNQ Trading Bot - v6.1 (ULTRA-LEAN)
====================================
✅ Entry: Sell Stop (2 ticks offset)
✅ Cancel: After 2 bars (Via TV Alert)
✅ SL: 15 ticks | TP: 120 ticks
✅ Trailing: Start 1 tick profit | Dist 10 ticks
✅ Removed: Filters, Break Even, Session Close
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re, os, time, threading, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET", "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"

BASE_URL = "https://demo.tradovateapi.com/v1" if USE_DEMO else "https://live.tradovateapi.com/v1"

# ─── AUTH & STATE ────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

_state_lock = threading.Lock()
trades      = {}
signal_log  = []

# ─── CORE FUNCTIONS ──────────────────────────────────────────────────────────
def get_access_token():
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        if _access_token and now < _token_expiry - 120:
            return _access_token
        
        # Login Logic
        try:
            r = requests.post(f"{BASE_URL}/auth/accesstokenrequest", json={
                "name": TRADOVATE_USERNAME, "password": TRADOVATE_PASSWORD,
                "appId": TRADOVATE_APP_ID, "appVersion": "1.0",
                "cid": TRADOVATE_APP_ID, "sec": TRADOVATE_APP_SECRET,
            }, timeout=12)
            d = r.json()
            if "accessToken" in d:
                _access_token = d["accessToken"]
                _token_expiry = now + d.get("expirationTime", 4500)
                return _access_token
        except Exception as e:
            log.error(f"Auth Error: {e}")
        return _access_token

def get_account_id():
    global _account_id
    if _account_id: return _account_id
    tok = get_access_token()
    try:
        r = requests.get(f"{BASE_URL}/account/list", headers={"Authorization": f"Bearer {tok}"})
        accs = r.json()
        if accs: _account_id = accs[0]["id"]; return _account_id
    except: return None

def _api_post(endpoint, body):
    url = f"{BASE_URL}/{endpoint}"
    headers = {"Authorization": f"Bearer {get_access_token()}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=body, timeout=10)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ─── TRADING ACTIONS ──────────────────────────────────────────────────────────
def _place_order(sym, side, qty, order_type, stop_price=None):
    acc = get_account_id()
    body = {
        "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
        "action": side, "symbol": sym, "orderQty": qty,
        "orderType": order_type, "isAutomated": True
    }
    if stop_price: body["stopPrice"] = round(stop_price, 2)
    return _api_post("order/placeorder", body)

def _bracket_parallel(sym, side, qty, entry_price, tp_ticks, sl_ticks, ts):
    acc = get_account_id()
    close_side = "Buy" if side == "Sell" else "Sell"
    mult = -1 if side == "Sell" else 1
    
    tp_p = round(entry_price + mult * tp_ticks * ts, 2)
    sl_p = round(entry_price - mult * sl_ticks * ts, 2)

    tp_res = _api_post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME, "accountId": acc, "action": close_side,
        "symbol": sym, "orderQty": qty, "orderType": "Limit", "price": tp_p, "isAutomated": True
    })
    sl_res = _api_post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME, "accountId": acc, "action": close_side,
        "symbol": sym, "orderQty": qty, "orderType": "Stop", "stopPrice": sl_p, "isAutomated": True
    })
    return tp_res.get("orderId"), sl_res.get("orderId"), tp_p, sl_p

# ─── WEBHOOK ──────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if data.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "Unauthorized"}), 401

    action = data.get("action", "")
    sym = data.get("symbol", "MNQM6")
    qty = int(data.get("qty", 1))
    price = float(data.get("price", 0))
    ts = float(data.get("tickSize", 0.25))
    trade_id = data.get("tradeId", f"T_{int(time.time())}")

    # NEW DEFAULTS
    SL_TICKS = 15
    TP_TICKS = 120
    STOP_OFFSET = 2

    if action == "short_entry":
        # Force Sell Stop 2 ticks below
        stop_price = round(price - STOP_OFFSET * ts, 2)
        res = _place_order(sym, "Sell", qty, "Stop", stop_price=stop_price)
        
        if "orderId" in res:
            oid = res["orderId"]
            tp_id, sl_id, tp_p, sl_p = _bracket_parallel(sym, "Sell", qty, stop_price, TP_TICKS, SL_TICKS, ts)
            
            with _state_lock:
                trades[trade_id] = {
                    "entryOrderId": oid, "tpOrderId": tp_id, "slOrderId": sl_id,
                    "symbol": sym, "side": "Sell", "qty": qty, "status": "pending",
                    "entryPrice": stop_price, "tpPrice": tp_p, "slPrice": sl_p, "tickSize": ts
                }
            return jsonify({"status": "ok", "orderId": oid})

    elif action == "trail_armed":
        # Trailing: Distance 10 ticks
        TRAIL_DIST = 10
        curr_p = float(data.get("currentPrice", price))
        with _state_lock:
            t = trades.get(trade_id)
            if t and t.get("slOrderId"):
                new_sl = round(curr_p + TRAIL_DIST * ts, 2) if t["side"] == "Sell" else round(curr_p - TRAIL_DIST * ts, 2)
                _api_post("order/modifyorder", {"orderId": t["slOrderId"], "orderType": "Stop", "stopPrice": new_sl})
                t["slPrice"] = new_sl
                return jsonify({"status": "trail_updated", "new_sl": new_sl})

    elif action == "cancel_pending":
        with _state_lock:
            t = trades.get(trade_id)
            if t and t["status"] == "pending":
                _api_post("order/cancelorder", {"orderId": t["entryOrderId"]})
                t["status"] = "cancelled"
                return jsonify({"status": "cancelled"})

    return jsonify({"status": "ignored"})

@app.route("/")
def dashboard():
    return f"<h1>MNQ BOT v6.1 ACTIVE</h1><p>SL: 15 | TP: 120 | Trail: 10</p><p>Trades: {len(trades)}</p>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
