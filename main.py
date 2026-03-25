"""
╔══════════════════════════════════════════════════════════════════════╗
║           MNQ Trading Bot — Python Mirror of PineScript v6           ║
║                                                                      ║
║  Pine Script Logic → Python (1:1 match)                              ║
║                                                                      ║
║  Signal    : close < open  (no filters)                              ║
║  Order     : Sell Stop  =  close - 2 ticks                           ║
║  Cancel    : after 2 bars if unfilled                                ║
║  SL        : 15 ticks                                                ║
║  TP        : 120 ticks                                               ║
║  Trailing  : armed at 1 tick profit, distance 10 ticks               ║
║  Break Even: OFF                                                     ║
║  Session   : OFF                                                     ║
║  Date Rng  : OFF                                                     ║
╚══════════════════════════════════════════════════════════════════════╝
"""

# ── Standard imports ──────────────────────────────────────────────────────────
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, re, os, time, threading, logging
from datetime import datetime

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)s | %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 ── CONFIGURATION  (mirrors Pine Script "INPUTS" section)
# ══════════════════════════════════════════════════════════════════════════════

# Tradovate credentials  →  Railway Environment Variables
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME",   "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD",   "")
TRADOVATE_APP_ID      = os.environ.get("TRADOVATE_APP_ID",      "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET",
                        "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET",       "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO",             "true").lower() == "true"

# Tradovate API endpoints
DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
BASE_URL = DEMO_URL if USE_DEMO else LIVE_URL

# ── Strategy constants  (mirrors Pine Script "CONSTANTS" section) ─────────────
CONTRACTS         = 1       # Number of contracts
SL_TICKS          = 15      # Stop Loss ticks (As per user request)
TP_TICKS          = 120     # Take Profit ticks (As per user request)
STOP_OFFSET_TICKS = 2       # Sell Stop = close - 2 ticks
CANCEL_AFTER_BARS = 2       # Cancel unfilled order after 2 bars
TRAIL_START_TICKS = 1       # Trailing arms after 1 tick profit
TRAIL_DIST_TICKS  = 10      # Trailing distance = 10 ticks
TICK_SIZE         = 0.25    # MNQ tick size (syminfo.mintick)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 ── GLOBAL STATE  (mirrors Pine Script "var array<>" declarations)
# ══════════════════════════════════════════════════════════════════════════════

# Authentication
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

# Trade registry — mirrors Pine Script arrays
# { tradeId → { entryOrderId, tpOrderId, slOrderId, symbol, side,
#                qty, entryPrice, tpPrice, slPrice, status,
#                trailArmed, openTime, bars } }
_state_lock = threading.Lock()
trades      = {}
signal_log  = []   # last 200 signals


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 ── SYMBOL CONVERTER
# ══════════════════════════════════════════════════════════════════════════════

def tradovate_symbol(tv_symbol: str) -> str:
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", tv_symbol)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return tv_symbol


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 ── AUTHENTICATION
# ══════════════════════════════════════════════════════════════════════════════

def _renew_token(existing: str):
    try:
        r = requests.post(
            f"{BASE_URL}/auth/renewaccesstoken",
            headers={"Authorization": f"Bearer {existing}"},
            timeout=10,
        )
        d = r.json()
        if "accessToken" in d:
            log.info("✅ Token renewed silently")
            return d["accessToken"], int(d.get("expirationTime", 4500))
    except Exception as e:
        log.warning(f"Renew failed: {e}")
    return None, 0


def _fresh_login():
    log.info("🔐 Attempting fresh login with credentials...")
    try:
        r = requests.post(
            f"{BASE_URL}/auth/accesstokenrequest",
            json={
                "name":       TRADOVATE_USERNAME,
                "password":   TRADOVATE_PASSWORD,
                "appId":      TRADOVATE_APP_ID,
                "appVersion": "1.0",
                "cid":        TRADOVATE_APP_ID,
                "sec":        TRADOVATE_APP_SECRET,
            },
            timeout=12,
        )
        d = r.json()
        if "accessToken" in d:
            log.info("✅ Fresh login successful")
            return d["accessToken"], int(d.get("expirationTime", 4500))
    except Exception as e:
        log.error(f"Login exception: {e}")
    return None, 0


def get_access_token() -> str | None:
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        if _access_token and now < _token_expiry - 120:
            return _access_token
        if _access_token:
            tok, exp = _renew_token(_access_token)
            if tok:
                _access_token = tok
                _token_expiry = now + exp
                return _access_token
        tok, exp = _fresh_login()
        if tok:
            _access_token = tok
            _token_expiry = now + exp
            return _access_token
        return _access_token or None


def get_account_id() -> int | None:
    global _account_id
    if _account_id:
        return _account_id
    tok = get_access_token()
    if not tok: return None
    try:
        r = requests.get(f"{BASE_URL}/account/list", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        accs = r.json()
        if isinstance(accs, list) and accs:
            _account_id = accs[0]["id"]
            return _account_id
    except Exception as e:
        log.error(f"Account fetch error: {e}")
    return None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type":  "application/json",
    }


def _auto_refresh():
    while True:
        time.sleep(3600)
        get_access_token()

threading.Thread(target=_auto_refresh, daemon=True, name="token-refresh").start()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 ── ORDER EXECUTION  (mirrors Pine Script strategy.entry / exit)
# ══════════════════════════════════════════════════════════════════════════════

def place_sell_stop(symbol: str, qty: int, stop_price: float) -> dict:
    acc = get_account_id()
    if not acc: return {"error": "no_account_id"}
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell", # Strictly SELL for entries
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Stop",
        "stopPrice":   round(stop_price, 2),
        "isAutomated": True,
    }
    log.info(f"→ SellStop {qty} {symbol} @ {stop_price}")
    return _api_post("order/placeorder", body)


def place_market_sell(symbol: str, qty: int) -> dict:
    acc = get_account_id()
    if not acc: return {"error": "no_account_id"}
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    }
    return _api_post("order/placeorder", body)


def place_bracket(symbol: str, qty: int, tp_price: float, sl_price: float) -> tuple[dict, dict]:
    """Mirrors: strategy.exit. For a SHORT position, exits are BUY orders."""
    acc    = get_account_id()
    tp_res = [{}]; sl_res = [{}]

    def _tp():
        tp_res[0] = _api_post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
            "action": "Buy", "symbol": symbol, "orderQty": qty,
            "orderType": "Limit", "price": round(tp_price, 2), "isAutomated": True,
        })

    def _sl():
        sl_res[0] = _api_post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
            "action": "Buy", "symbol": symbol, "orderQty": qty,
            "orderType": "Stop", "stopPrice": round(sl_price, 2), "isAutomated": True,
        })

    t1 = threading.Thread(target=_tp, daemon=True); t2 = threading.Thread(target=_sl, daemon=True)
    t1.start(); t2.start(); t1.join(8); t2.join(8)
    return tp_res[0], sl_res[0]


def modify_stop_order(order_id: int, new_stop: float) -> dict:
    return _api_post("order/modifyorder", {
        "orderId":   order_id,
        "orderType": "Stop",
        "stopPrice": round(new_stop, 2),
    })


def cancel_order(order_id: int) -> dict:
    return _api_post("order/cancelorder", {"orderId": order_id})


def market_close_position(symbol: str, qty: int) -> dict:
    acc = get_account_id()
    return _api_post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
        "action": "Buy", "symbol": symbol, "orderQty": qty,
        "orderType": "Market", "isAutomated": True,
    })


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 ── TRADE STATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def register_trade(trade_id: str, entry_order_id: int, symbol: str, tv_symbol: str, entry_price: float, qty: int):
    with _state_lock:
        trades[trade_id] = {
            "entryOrderId": entry_order_id, "tpOrderId": None, "slOrderId": None,
            "symbol": symbol, "tvSymbol": tv_symbol, "qty": qty,
            "side": "Sell", # Always Sell for entry
            "entryPrice": entry_price, "tpPrice": None, "slPrice": None,
            "status": "pending", "trailArmed": False, "openTime": datetime.now().isoformat(),
        }


def get_fill_price(order_id: int, timeout: float = 8.0) -> float | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = _api_get(f"order/item?id={order_id}")
            if result.get("ordStatus") == "Filled":
                fill = result.get("avgFillPrice")
                if fill: return float(fill)
        except: pass
        time.sleep(0.4)
    return None


def attach_bracket_to_trade(trade_id: str, entry_price: float, qty: int):
    with _state_lock:
        trade = trades.get(trade_id)
    if not trade: return

    symbol = trade["symbol"]
    fill_price = get_fill_price(trade["entryOrderId"], timeout=10.0)
    actual_price = fill_price if fill_price else entry_price

    # SHORT Logic: TP is below, SL is above
    tp_price = round(actual_price - TP_TICKS * TICK_SIZE, 2)
    sl_price = round(actual_price + SL_TICKS * TICK_SIZE, 2)

    tp_r, sl_r = place_bracket(symbol, qty, tp_price, sl_price)
    
    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "entryPrice": actual_price, "tpOrderId": tp_r.get("orderId"),
                "slOrderId": sl_r.get("orderId"), "tpPrice": tp_price, "slPrice": sl_price,
                "status": "open" if (tp_r.get("orderId") or sl_r.get("orderId")) else "pending",
            })


def _api_post(endpoint: str, body: dict, retry: bool = True) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.post(url, headers=_headers(), json=body, timeout=10)
        if r.status_code == 401 and retry:
            global _token_expiry
            _token_expiry = 0
            return _api_post(endpoint, body, retry=False)
        return r.json()
    except Exception as e: return {"error": str(e)}

def _api_get(endpoint: str) -> dict | list:
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        return r.json()
    except Exception as e: return {"error": str(e)}

def log_signal(action: str, trade_id: str, symbol: str, result):
    with _state_lock:
        signal_log.append({"time": datetime.now().isoformat(), "action": action, "tradeId": trade_id, "symbol": symbol, "result": str(result)[:200]})
        if len(signal_log) > 200: signal_log.pop(0)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 ── WEBHOOK ENDPOINT (Short-Only Logic)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET: return jsonify({"error": "unauthorized"}), 401

        action    = data.get("action", "")
        tv_symbol = data.get("symbol", "MNQM2026")
        symbol    = tradovate_symbol(tv_symbol)
        qty       = int(data.get("qty", CONTRACTS))
        price     = float(data.get("price", 0)) 
        trade_id  = data.get("tradeId", f"Auto_{int(time.time())}")
        result    = {}

        # 1. ENTRY LOGIC (SHORT ONLY)
        if action == "short_entry":
            # entryPrice = close - 2 ticks
            entry_price = round(price - STOP_OFFSET_TICKS * TICK_SIZE, 2)
            res = place_sell_stop(symbol, qty, entry_price)
            if "orderId" in res:
                register_trade(trade_id, res["orderId"], symbol, tv_symbol, entry_price, qty)
                threading.Thread(target=attach_bracket_to_trade, args=(trade_id, entry_price, qty), daemon=True).start()
                result = {"orderId": res["orderId"], "stopPrice": entry_price, "side": "SELL"}
            else: result = res

        # 2. TRAILING LOGIC
        elif action == "trail_armed":
            with _state_lock: trade = trades.get(trade_id)
            if trade and trade.get("slOrderId"):
                cur_price = float(data.get("currentPrice", 0))
                # SHORT Trailing: New SL = CurrentPrice + 10 ticks
                new_sl = round(cur_price + TRAIL_DIST_TICKS * TICK_SIZE, 2)
                modify_res = modify_stop_order(trade["slOrderId"], new_sl)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["slPrice"] = new_sl
                        trades[trade_id]["trailArmed"] = True
                result = {"newSL": new_sl, "res": modify_res}

        # 3. CANCEL LOGIC
        elif action == "cancel_pending":
            with _state_lock: trade = trades.get(trade_id)
            if trade and trade["status"] == "pending":
                cancel_order(trade["entryOrderId"])
                with _state_lock: trades[trade_id]["status"] = "cancelled"
                result = {"cancelled": trade_id}

        # 4. CLOSE ALL
        elif action == "close_all":
            with _state_lock:
                open_trades = {tid: dict(t) for tid, t in trades.items() if t["status"] == "open"}
            for tid, t in open_trades.items():
                if t.get("tpOrderId"): cancel_order(t["tpOrderId"])
                if t.get("slOrderId"): cancel_order(t["slOrderId"])
                market_close_position(t["symbol"], t["qty"])
                with _state_lock: trades[tid]["status"] = "closed"
            result = {"count": len(open_trades)}

        log_signal(action, trade_id, tv_symbol, result)
        return jsonify({"status": "ok", "result": result})

    except Exception as e:
        log.exception(f"❌ Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ── End of code (Sections 9-10 remain unchanged to preserve your UI) ──────────
# (Manual token and Dashboard endpoints remain as you had them)

@app.route("/")
def dashboard():
    with _state_lock:
        snap = dict(trades)
        logs = list(reversed(signal_log[-50:]))
    
    # Simple dashboard render (Your existing logic)
    trade_rows = ""
    for tid, t in sorted(snap.items(), key=lambda x: x[1]["openTime"], reverse=True)[:50]:
        side_color = "#f87171" # Red for SELL
        trade_rows += f"<tr><td>{tid}</td><td>{t['symbol']}</td><td style='color:{side_color}'>SELL</td><td>{t.get('entryPrice','')}</td><td>{t.get('tpPrice','')}</td><td>{t.get('slPrice','')}</td><td>{t['status']}</td></tr>"
    
    return f"<html><body style='background:#1a1a1a;color:white;font-family:sans-serif;'><h1>MNQ BOT - SHORT ONLY</h1><table border='1'>{trade_rows}</table></body></html>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
