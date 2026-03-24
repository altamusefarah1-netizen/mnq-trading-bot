"""
MNQ Trading Bot - v6.1 (FIXED)
============================
✅ FIX: 'cid' hadda waa integer (Tradovate requirement)
✅ FIX: Full error logging (si loo ogaado sababta trade-ku u dhici la'yahay)
✅ Pyramiding 100+ trades (No limits)
✅ NO EMA Filters, NO Time Filters
✅ Sell Stop 2 ticks / Market Entry support
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re, os, time, threading, logging
from datetime import datetime

# Logging setup - Tani waxay kuu sheegaysaa dhibaatada meesha ay ku jirto
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ─── CONFIG (Hubi inaad Railway ku gasho xogtan) ─────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
# App ID waa inuu noqdaa midka rasmiga ah (Badanaa waa 4-5 digit)
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "4") 
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET", "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"

BASE_URL = "https://demo.tradovateapi.com/v1" if USE_DEMO else "https://live.tradovateapi.com/v1"

# ─── STATE ────────────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = None
_token_expiry = 0
_account_id   = None
trades        = {}  # { tradeId -> trade_info }
signal_log    = []

# ─── SYMBOL HELPER ───────────────────────────────────────────────────────────
def to_tradovate_sym(sym):
    """Waxay u beddelaysaa MNQM2026 -> MNQM6"""
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced: return forced
    m = re.match(r"([A-Z]+)(\d{4})$", sym)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return sym

# ═══════════════════════════════════════════════════════════════════════
# AUTH LOGIC (Auto-Login)
# ═══════════════════════════════════════════════════════════════════════

def get_access_token():
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        if _access_token and now < _token_expiry - 120:
            return _access_token
        
        log.info("🔐 Isku dayaya inaan galo Tradovate (Login)...")
        try:
            r = requests.post(f"{BASE_URL}/auth/accesstokenrequest", json={
                "name":       TRADOVATE_USERNAME,
                "password":   TRADOVATE_PASSWORD,
                "appId":      "PythonBot",
                "appVersion": "1.0",
                "cid":        int(TRADOVATE_APP_ID), # MUHIIM: Waa inuu integer yahay
                "sec":        TRADOVATE_APP_SECRET,
            }, timeout=15)
            d = r.json()
            if "accessToken" in d:
                _access_token = d["accessToken"]
                _token_expiry = now + d.get("expirationTime", 4500)
                log.info("✅ Login guul baa ku dhamaaday!")
                return _access_token
            else:
                log.error(f"❌ Login-ku waa fashilmay: {d}")
        except Exception as e:
            log.error(f"❌ Auth Error: {e}")
        return None

def get_account_id():
    global _account_id
    if _account_id: return _account_id
    tok = get_access_token()
    if not tok: return None
    try:
        r = requests.get(f"{BASE_URL}/account/list", headers={"Authorization": f"Bearer {tok}"})
        accs = r.json()
        if isinstance(accs, list) and len(accs) > 0:
            _account_id = accs[0]["id"]
            log.info(f"✅ Account-ka la helay: {_account_id}")
            return _account_id
    except Exception as e:
        log.error(f"❌ Account ID error: {e}")
    return None

# ═══════════════════════════════════════════════════════════════════════
# TRADING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════

def _place_order(sym, side, qty, order_type, price=None, stop_price=None):
    acc = get_account_id()
    tok = get_access_token()
    if not acc or not tok:
        return {"error": "Ma jiro Login ama Account ID"}

    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   int(acc),
        "action":      side,
        "symbol":      sym,
        "orderQty":    qty,
        "orderType":   order_type,
        "isAutomated": True
    }
    if order_type == "Stop" and stop_price: body["stopPrice"] = round(stop_price, 2)
    if order_type == "Limit" and price: body["price"] = round(price, 2)

    try:
        r = requests.post(f"{BASE_URL}/order/placeorder", 
                          headers={"Authorization": f"Bearer {tok}"}, 
                          json=body, timeout=10)
        res = r.json()
        if "orderId" not in res:
            log.error(f"❌ Tradovate Order Reject: {res}")
        return res
    except Exception as e:
        log.error(f"❌ Place Order Error: {e}")
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════════════════
# WEBHOOK ENDPOINT
# ═══════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Wrong Secret"}), 401

    action    = data.get("action", "")
    tv_sym    = data.get("symbol", "MNQM2026")
    sym       = to_tradovate_sym(tv_sym)
    qty       = int(data.get("qty", 1))
    trade_id  = data.get("tradeId", f"T_{int(time.time())}")
    price     = float(data.get("price", 0))
    tick_size = float(data.get("tickSize", 0.25))

    log.info(f"🔔 Signal: {action} | TradeID: {trade_id} | Symbol: {sym}")

    # 1. ENTRY LOGIC (SHORT ama LONG)
    if action in ["short_entry", "long_entry"]:
        side = "Sell" if action == "short_entry" else "Buy"
        order_type = data.get("orderType", "Market")
        
        # Haddii uu yahay Stop Order (Sell Stop 2 ticks)
        stop_p = None
        if order_type == "Stop":
            offset = int(data.get("stopOffset", 2))
            stop_p = price - (offset * tick_size) if side == "Sell" else price + (offset * tick_size)

        res = _place_order(sym, side, qty, order_type, stop_price=stop_p)
        
        if "orderId" in res:
            trades[trade_id] = {"orderId": res["orderId"], "status": "OPEN", "side": side, "sym": sym}
            log.info(f"✅ Trade {trade_id} is live on Tradovate!")
        return jsonify(res)

    # 2. CLOSE ALL
    elif action == "close_all":
        log.info("Closing all positions...")
        # (Halkan ku dar koodhka xiritaanka haddii loo baahdo)
        return jsonify({"status": "closed_all_received"})

    return jsonify({"status": "ignored"})

@app.route("/")
def index():
    return f"Bot is Online | Mode: {'DEMO' if USE_DEMO else 'LIVE'} | Active Trades: {len(trades)}"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
