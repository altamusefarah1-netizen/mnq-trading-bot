"""
MNQ Trading Bot - v6 Final (REPAIRED)
====================================
✅ Sell Stop: 2 ticks offset
✅ SL: 15 ticks | TP: 120 ticks
✅ Trailing: Start 1 tick | Dist 10 ticks
✅ Filters: DHAMMAAN WAA LAGA SAARAY
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

# ─── CONFIG (Ha taaban kuwan, Railway ka buuxi) ──────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET", "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"

BASE_URL = "https://demo.tradovateapi.com/v1" if USE_DEMO else "https://live.tradovateapi.com/v1"

# ─── STATE ──────────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

_state_lock = threading.Lock()
trades      = {}   
signal_log  = []   

# ─── AUTH LOGIC (Kii hore oo aan waxba laga beddelin) ────────────────────────
def get_access_token():
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        if _access_token and now < _token_expiry - 120:
            return _access_token
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
            log.error(f"Login error: {e}")
        return _access_token

def get_account_id():
    global _account_id
    if _account_id: return _account_id
    tok = get_access_token()
    try:
        r = requests.get(f"{BASE_URL}/account/list", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        accs = r.json()
        if isinstance(accs, list) and accs:
            _account_id = accs[0]["id"]
            return _account_id
    except: return None

# ─── API HELPERS ─────────────────────────────────────────────────────────────
def _post(ep, body):
    url = f"{BASE_URL}/{ep}"
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
        "orderType": order_type, "isAutomated": True,
    }
    if stop_price: body["stopPrice"] = round(stop_price, 2)
    return _post("order/placeorder", body)

def _attach_bracket(trade_id, sym, side, qty, entry_price, ts):
    # Fixed SL/TP as per request
    TP_TICKS = 120
    SL_TICKS = 15
    acc = get_account_id()
    close_side = "Buy" if side == "Sell" else "Sell"
    mult = -1 if side == "Sell" else 1
    
    tp_price = round(entry_price + mult * TP_TICKS * ts, 2)
    sl_price = round(entry_price - mult * SL_TICKS * ts, 2)

    tp_res = _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME, "accountId": acc, "action": close_side,
        "symbol": sym, "orderQty": qty, "orderType": "Limit", "price": tp_price, "isAutomated": True
    })
    sl_res = _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME, "accountId": acc, "action": close_side,
        "symbol": sym, "orderQty": qty, "orderType": "Stop", "stopPrice": sl_price, "isAutomated": True
    })

    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "tpOrderId": tp_res.get("orderId"), "slOrderId": sl_res.get("orderId"),
                "tpPrice": tp_price, "slPrice": sl_price, "status": "open"
            })

# ─── WEBHOOK (Halkaan baa isbeddelka rasmiga ah laga sameeyay) ───────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "unauthorized"}), 401

        action = data.get("action", "")
        sym = data.get("symbol", "MNQM6")
        qty = int(data.get("qty", 1))
        price = float(data.get("price", 0))
        ts = float(data.get("tickSize", 0.25))
        trade_id = data.get("tradeId", f"T_{int(time.time())}")

        # 1. SHORT ENTRY (Fixed Sell Stop 2 Ticks)
        if action == "short_entry":
            sp = round(price - 2 * ts, 2) # 2 ticks below price
            res = _place_order(sym, "Sell", qty, "Stop", stop_price=sp)
            if "orderId" in res:
                with _state_lock:
                    trades[trade_id] = {
                        "entryOrderId": res["orderId"], "symbol": sym, "side": "Sell",
                        "qty": qty, "entryPrice": sp, "tickSize": ts, "status": "pending",
                        "openTime": datetime.now().isoformat()
                    }
                threading.Thread(target=_attach_bracket, args=(trade_id, sym, "Sell", qty, sp, ts), daemon=True).start()
                return jsonify({"status": "ok", "orderId": res["orderId"]})

        # 2. LONG ENTRY (Fixed Buy Stop 2 Ticks)
        elif action == "long_entry":
            sp = round(price + 2 * ts, 2) # 2 ticks above price
            res = _place_order(sym, "Buy", qty, "Stop", stop_price=sp)
            if "orderId" in res:
                with _state_lock:
                    trades[trade_id] = {
                        "entryOrderId": res["orderId"], "symbol": sym, "side": "Buy",
                        "qty": qty, "entryPrice": sp, "tickSize": ts, "status": "pending",
                        "openTime": datetime.now().isoformat()
                    }
                threading.Thread(target=_attach_bracket, args=(trade_id, sym, "Buy", qty, sp, ts), daemon=True).start()
                return jsonify({"status": "ok", "orderId": res["orderId"]})

        # 3. TRAILING (Start 1 tick, Dist 10 ticks)
        elif action == "trail_armed":
            with _state_lock:
                t = trades.get(trade_id)
            if t and t.get("slOrderId"):
                curr_p = float(data.get("currentPrice", price))
                # New SL distance 10 ticks
                new_sl = round(curr_p + 10 * ts, 2) if t["side"] == "Sell" else round(curr_p - 10 * ts, 2)
                _post("order/modifyorder", {"orderId": t["slOrderId"], "orderType": "Stop", "stopPrice": new_sl})
                with _state_lock: trades[trade_id]["slPrice"] = new_sl
                return jsonify({"status": "trailing_updated"})

        # 4. CANCEL (After 2 bars logic)
        elif action == "cancel_pending":
            with _state_lock:
                t = trades.get(trade_id)
                if t and t["status"] == "pending":
                    _post("order/cancelorder", {"orderId": t["entryOrderId"]})
                    trades[trade_id]["status"] = "cancelled"
            return jsonify({"status": "cancelled"})

        return jsonify({"status": "ignored"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── DASHBOARD (Fixed HTML) ──────────────────────────────────────────────────
@app.route("/")
def dashboard():
    with _state_lock:
        snap = dict(trades)
    return f"""
    <html><body style="font-family:sans-serif; background:#07070f; color:#fff; padding:20px;">
        <h1>MNQ Bot v6.1 <span style="color:green">ACTIVE</span></h1>
        <div style="background:#111; padding:15px; border-radius:10px;">
            <p><b>Settings:</b> SL 15t | TP 120t | Trail Dist 10t | Entry: Stop 2t</p>
            <p><b>Status:</b> No Session Filters | No Date Filters</p>
        </div>
        <h3>Active Trades: {len([t for t in snap.values() if t['status'] in ('open','pending')])}</h3>
        <table border="1" style="width:100%; text-align:left; border-collapse:collapse;">
            <tr style="background:#222;"><th>ID</th><th>Side</th><th>Entry</th><th>SL</th><th>TP</th><th>Status</th></tr>
            {"".join([f"<tr><td>{k}</td><td>{v['side']}</td><td>{v['entryPrice']}</td><td>{v.get('slPrice')}</td><td>{v.get('tpPrice')}</td><td>{v['status']}</td></tr>" for k,v in snap.items()])}
        </table>
    </body></html>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
