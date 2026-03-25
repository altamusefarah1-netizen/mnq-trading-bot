"""
╔══════════════════════════════════════════════════════════════════════╗
║          MNQ Trading Bot — Python Mirror of PineScript v6           ║
║                                                                      ║
║  Signal    : close < open  (no filters)                             ║
║  Order     : Sell Stop  =  close - 2 ticks                         ║
║  Cancel    : after 2 bars if unfilled                               ║
║  SL        : 15 ticks above fill                                    ║
║  TP        : 120 ticks below fill                                   ║
║  Trailing  : armed at 1 tick profit, distance 10 ticks              ║
║  Fill      : WebSocket instant detection (no polling)               ║
║  Token     : auto-renew every 55 min (keep-alive)                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests, re, os, time, threading, logging, json
from datetime import datetime

# WebSocket library
try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s | %(levelname)s | %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ══════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION
# ══════════════════════════════════════════════════════════════════════

TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME",   "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD",   "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID",     "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET",
                        "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"

DEMO_URL    = "https://demo.tradovateapi.com/v1"
LIVE_URL    = "https://live.tradovateapi.com/v1"
DEMO_WS_URL = "wss://demo.tradovateapi.com/v1/websocket"
LIVE_WS_URL = "wss://live.tradovateapi.com/v1/websocket"
BASE_URL    = DEMO_URL    if USE_DEMO else LIVE_URL
WS_URL      = DEMO_WS_URL if USE_DEMO else LIVE_WS_URL

# Strategy constants
CONTRACTS         = 1
SL_TICKS          = 15
TP_TICKS          = 120
STOP_OFFSET_TICKS = 2
CANCEL_AFTER_BARS = 2
TRAIL_START_TICKS = 1
TRAIL_DIST_TICKS  = 10
TICK_SIZE         = 0.25

# ══════════════════════════════════════════════════════════════════════
# SECTION 2 — GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════

_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

_state_lock = threading.Lock()
trades      = {}
signal_log  = []

# WebSocket state
_ws_conn        = None
_ws_lock        = threading.Lock()
_ws_connected   = False
_ws_request_id  = 0

# Fill events: { orderId(int) -> threading.Event }
# When WebSocket sees a fill, it sets the event so bracket thread wakes instantly
_fill_events  = {}   # { orderId -> threading.Event() }
_fill_prices  = {}   # { orderId -> float }
_fills_lock   = threading.Lock()

# ══════════════════════════════════════════════════════════════════════
# SECTION 3 — SYMBOL CONVERTER
# ══════════════════════════════════════════════════════════════════════

def tradovate_symbol(tv: str) -> str:
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", tv)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return tv

# ══════════════════════════════════════════════════════════════════════
# SECTION 4 — AUTHENTICATION
# ══════════════════════════════════════════════════════════════════════

def _renew_token(tok: str):
    try:
        r = requests.post(
            f"{BASE_URL}/auth/renewaccesstoken",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10)
        d = r.json()
        if "accessToken" in d:
            exp = d.get("expirationTime", 4500)
            log.info(f"✅ Token renewed — {exp}s")
            return d["accessToken"], int(exp)
    except Exception as e:
        log.warning(f"Renew failed: {e}")
    return None, 0


def _fresh_login():
    log.info("🔐 Fresh login...")
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
            }, timeout=12)
        d = r.json()
        if "accessToken" in d:
            exp = d.get("expirationTime", 4500)
            log.info(f"✅ Login OK — {exp}s")
            return d["accessToken"], int(exp)
        if "p-captcha" in d:
            log.warning("⚠️ CAPTCHA — need /set-token")
        else:
            log.error(f"Login failed: {d}")
    except Exception as e:
        log.error(f"Login error: {e}")
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
    if not tok:
        return None
    try:
        r    = requests.get(f"{BASE_URL}/account/list",
                            headers={"Authorization": f"Bearer {tok}"},
                            timeout=10)
        accs = r.json()
        if isinstance(accs, list) and accs:
            _account_id = accs[0]["id"]
            log.info(f"✅ Account: {_account_id}")
            return _account_id
    except Exception as e:
        log.error(f"Account error: {e}")
    return None


def _hdrs() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type":  "application/json",
    }


def _token_keepalive():
    while True:
        time.sleep(55 * 60)
        log.info("⏰ Token keep-alive...")
        global _access_token, _token_expiry
        with _auth_lock:
            if _access_token:
                tok, exp = _renew_token(_access_token)
                if tok:
                    _access_token = tok
                    _token_expiry = time.time() + exp
                    # Re-auth WebSocket with new token
                    _ws_reauth()
                else:
                    _token_expiry = 0

threading.Thread(target=_token_keepalive, daemon=True, name="token-keepalive").start()

# ══════════════════════════════════════════════════════════════════════
# SECTION 5 — API WRAPPER
# ══════════════════════════════════════════════════════════════════════

def _post(endpoint: str, body: dict, retry: bool = True) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.post(url, headers=_hdrs(), json=body, timeout=10)
        if r.status_code == 401 and retry:
            log.warning("401 — refreshing + retry")
            global _token_expiry
            with _auth_lock:
                _token_expiry = 0
            return _post(endpoint, body, retry=False)
        return r.json()
    except Exception as e:
        log.error(f"POST /{endpoint}: {e}")
        return {"error": str(e)}


def _get(endpoint: str) -> dict | list:
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.get(url, headers=_hdrs(), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"GET /{endpoint}: {e}")
        return {"error": str(e)}

# ══════════════════════════════════════════════════════════════════════
# SECTION 6 — WEBSOCKET (instant fill detection)
# ══════════════════════════════════════════════════════════════════════

def _on_ws_message(ws, message: str):
    """
    Handle all WebSocket messages from Tradovate.
    We listen for execution reports (fills) and instantly
    notify any waiting bracket thread via threading.Event.
    """
    global _ws_connected
    try:
        # Tradovate WS messages: "frame\n{json}" or just heartbeat "o"
        if message in ("o", "h", "c"):
            if message == "o":
                log.info("🔌 WebSocket opened — authenticating...")
                _ws_authenticate(ws)
            elif message == "h":
                # Heartbeat — send pong
                ws.send("[]")
            elif message == "c":
                log.warning("🔌 WebSocket closed by server")
                _ws_connected = False
            return

        # Parse frame
        if message.startswith("a"):
            frames = json.loads(message[1:])
            for frame in frames:
                _handle_ws_frame(frame)

    except Exception as e:
        log.warning(f"WS message error: {e} | msg={message[:100]}")


def _handle_ws_frame(frame: dict):
    """Process a single WebSocket frame."""
    global _ws_connected

    event = frame.get("e", "")
    data  = frame.get("d", {})

    # Auth response
    if event == "authorized":
        _ws_connected = True
        log.info("✅ WebSocket authenticated")
        # Subscribe to execution reports (fills)
        _ws_subscribe_executions()
        return

    # Execution report — this fires when an order is FILLED
    if event in ("execution", "executionReport"):
        _process_execution(data)
        return

    # Order update
    if event in ("order", "orderUpdate"):
        orders = data if isinstance(data, list) else [data]
        for order in orders:
            status = order.get("ordStatus", "")
            oid    = order.get("id") or order.get("orderId")
            if status == "Filled" and oid:
                fill_price = order.get("avgFillPrice")
                if fill_price:
                    _notify_fill(int(oid), float(fill_price))
        return


def _process_execution(data: dict):
    """Extract fill price from execution report and notify waiting threads."""
    oid        = data.get("orderId") or data.get("id")
    fill_price = data.get("execPrice") or data.get("avgFillPrice") or data.get("price")
    status     = data.get("execType") or data.get("ordStatus", "")

    if oid and fill_price and status in ("Fill", "Filled", "Trade"):
        log.info(f"⚡ WS Fill: order #{oid} @ {fill_price}")
        _notify_fill(int(oid), float(fill_price))


def _notify_fill(order_id: int, fill_price: float):
    """Signal waiting bracket thread that fill occurred."""
    with _fills_lock:
        _fill_prices[order_id] = fill_price
        ev = _fill_events.get(order_id)
    if ev:
        ev.set()   # wake the waiting bracket thread instantly
        log.info(f"⚡ Fill event set: #{order_id} @ {fill_price}")


def _ws_authenticate(ws):
    """Send auth request over WebSocket."""
    tok = get_access_token()
    if not tok:
        return
    msg = json.dumps({
        "url":   "/auth/accesstokenrequest",
        "token": tok,
    })
    ws.send(f'["{msg}"]')


def _ws_subscribe_executions():
    """Subscribe to execution/order updates."""
    global _ws_conn, _ws_request_id
    if not _ws_conn:
        return
    _ws_request_id += 1
    msg = json.dumps({
        "url":   "/user/syncrequest",
        "body":  {"users": []},
        "i":     _ws_request_id,
    })
    try:
        _ws_conn.send(f'["{msg}"]')
        log.info("✅ WebSocket subscribed to execution updates")
    except Exception as e:
        log.warning(f"WS subscribe error: {e}")


def _ws_reauth():
    """Re-authenticate WebSocket after token renewal."""
    global _ws_conn
    if _ws_conn and _ws_connected:
        _ws_authenticate(_ws_conn)


def _on_ws_error(ws, error):
    global _ws_connected
    log.warning(f"🔌 WebSocket error: {error}")
    _ws_connected = False


def _on_ws_close(ws, close_status, close_msg):
    global _ws_connected
    log.warning(f"🔌 WebSocket closed: {close_status} {close_msg}")
    _ws_connected = False


def _on_ws_open(ws):
    log.info("🔌 WebSocket connection established")


def _run_websocket():
    """Background thread: maintain persistent WebSocket connection."""
    global _ws_conn, _ws_connected
    if not WS_AVAILABLE:
        log.warning("⚠️ websocket-client not installed — using HTTP polling fallback")
        return

    while True:
        try:
            log.info(f"🔌 Connecting WebSocket: {WS_URL}")
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open    = _on_ws_open,
                on_message = _on_ws_message,
                on_error   = _on_ws_error,
                on_close   = _on_ws_close,
            )
            with _ws_lock:
                _ws_conn = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log.error(f"WebSocket run error: {e}")
        finally:
            _ws_connected = False
            with _ws_lock:
                _ws_conn = None
        # Reconnect after 3s
        log.info("🔌 Reconnecting WebSocket in 3s...")
        time.sleep(3)


threading.Thread(target=_run_websocket, daemon=True, name="ws-main").start()

# ══════════════════════════════════════════════════════════════════════
# SECTION 7 — FILL DETECTION (WebSocket + HTTP fallback)
# ══════════════════════════════════════════════════════════════════════

def wait_for_fill(order_id: int, timeout: float = 30.0) -> float | None:
    """
    Wait for order fill using WebSocket event (instant).
    Falls back to HTTP polling if WebSocket not connected.

    WebSocket path:
      - Register fill event for this order_id
      - Block until WebSocket fires the event (microseconds after fill)
      - Return fill price instantly

    HTTP fallback path (if WS disconnected):
      - Poll /order/item every 0.2s
    """
    # Register WebSocket fill event BEFORE placing order
    ev = threading.Event()
    with _fills_lock:
        _fill_events[order_id] = ev

        # Check if already filled (race condition guard)
        if order_id in _fill_prices:
            fp = _fill_prices.pop(order_id)
            _fill_events.pop(order_id, None)
            return fp

    # ── WebSocket path ─────────────────────────────────────────────────
    if _ws_connected:
        log.info(f"⚡ WS: waiting fill #{order_id} (timeout={timeout}s)")
        filled = ev.wait(timeout=timeout)
        with _fills_lock:
            _fill_events.pop(order_id, None)
            if filled:
                fp = _fill_prices.pop(order_id, None)
                if fp:
                    log.info(f"⚡ WS fill instant: #{order_id} @ {fp}")
                    return fp

    # ── HTTP polling fallback ──────────────────────────────────────────
    log.info(f"📡 HTTP poll: waiting fill #{order_id}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res    = _get(f"order/item?id={order_id}")
            status = res.get("ordStatus", "")
            if status == "Filled":
                fp = res.get("avgFillPrice")
                if fp:
                    log.info(f"✅ HTTP fill: #{order_id} @ {fp}")
                    with _fills_lock:
                        _fill_events.pop(order_id, None)
                    return float(fp)
            if status in ("Canceled", "Rejected", "Expired"):
                log.warning(f"⚠️ Order #{order_id} {status}")
                with _fills_lock:
                    _fill_events.pop(order_id, None)
                return None
        except Exception as e:
            log.warning(f"Poll error: {e}")
        time.sleep(0.2)

    with _fills_lock:
        _fill_events.pop(order_id, None)
        _fill_prices.pop(order_id, None)
    log.warning(f"⚠️ Fill timeout #{order_id}")
    return None

# ══════════════════════════════════════════════════════════════════════
# SECTION 8 — ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════

def place_sell_stop(symbol: str, qty: int, stop_price: float) -> dict:
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    log.info(f"→ SellStop {qty} {symbol} @ {stop_price}")
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Stop",
        "stopPrice":   round(stop_price, 2),
        "isAutomated": True,
    })


def place_market_sell(symbol: str, qty: int) -> dict:
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    log.info(f"→ MarketSell {qty} {symbol}")
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    })


def place_tp_order(symbol: str, qty: int, tp_price: float) -> dict:
    """TP: Buy Limit below entry (closes SHORT at profit target)."""
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    log.info(f"→ TP BuyLimit {qty} {symbol} @ {tp_price}")
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Limit",
        "price":       round(tp_price, 2),
        "isAutomated": True,
    })


def place_sl_order(symbol: str, qty: int, sl_price: float) -> dict:
    """SL: Buy Stop above entry (closes SHORT if price rises)."""
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    log.info(f"→ SL BuyStop {qty} {symbol} @ {sl_price}")
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Stop",
        "stopPrice":   round(sl_price, 2),
        "isAutomated": True,
    })


def place_bracket_parallel(symbol: str, qty: int,
                            tp_price: float, sl_price: float) -> tuple:
    """Place TP and SL simultaneously in parallel threads."""
    tp_res = [{}]
    sl_res = [{}]

    def _do_tp(): tp_res[0] = place_tp_order(symbol, qty, tp_price)
    def _do_sl(): sl_res[0] = place_sl_order(symbol, qty, sl_price)

    t1 = threading.Thread(target=_do_tp, daemon=True)
    t2 = threading.Thread(target=_do_sl, daemon=True)
    t1.start(); t2.start()
    t1.join(8);  t2.join(8)
    log.info(f"→ Bracket {symbol} TP@{tp_price}={tp_res[0]} SL@{sl_price}={sl_res[0]}")
    return tp_res[0], sl_res[0]


def modify_sl_order(order_id: int, new_stop: float) -> dict:
    log.info(f"→ ModifySL #{order_id} → {new_stop}")
    return _post("order/modifyorder", {
        "orderId":   order_id,
        "orderType": "Stop",
        "stopPrice": round(new_stop, 2),
    })


def cancel_order(order_id: int) -> dict:
    log.info(f"→ Cancel #{order_id}")
    return _post("order/cancelorder", {"orderId": order_id})


def close_position_market(symbol: str, qty: int) -> dict:
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    log.info(f"→ EmergencyClose {qty} {symbol}")
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    })

# ══════════════════════════════════════════════════════════════════════
# SECTION 9 — BRACKET ATTACHMENT
# ══════════════════════════════════════════════════════════════════════

def attach_bracket(trade_id: str, entry_price: float, qty: int):
    """
    Background thread:
      1. Register fill event for WebSocket instant notification
      2. Wait for fill (WebSocket fires instantly, HTTP polls as fallback)
      3. Place TP + SL from real fill price
    """
    with _state_lock:
        trade = trades.get(trade_id)
    if not trade:
        log.error(f"attach_bracket: {trade_id} not found")
        return

    order_id = trade.get("entryOrderId")
    symbol   = trade["symbol"]

    if not order_id:
        log.error(f"attach_bracket: no entryOrderId for {trade_id}")
        return

    # Register event BEFORE wait (prevent race condition)
    ev = threading.Event()
    with _fills_lock:
        _fill_events[order_id] = ev

    log.info(f"⏳ Waiting fill: #{order_id} [{trade_id}] WS={'connected' if _ws_connected else 'polling'}")
    fill = wait_for_fill(order_id, timeout=30.0)

    if fill is None:
        log.warning(f"🚫 No fill for {trade_id} — bracket skipped")
        with _state_lock:
            if trade_id in trades:
                trades[trade_id]["status"] = "cancelled"
        return

    # Calculate bracket from REAL fill price
    tp_price = round(fill - TP_TICKS  * TICK_SIZE, 2)
    sl_price = round(fill + SL_TICKS  * TICK_SIZE, 2)
    log.info(f"📐 [{trade_id}] fill={fill} TP={tp_price} SL={sl_price}")

    tp_r, sl_r = place_bracket_parallel(symbol, qty, tp_price, sl_price)
    tp_id = tp_r.get("orderId")
    sl_id = sl_r.get("orderId")

    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "entryPrice": fill,
                "tpOrderId":  tp_id,
                "slOrderId":  sl_id,
                "tpPrice":    tp_price,
                "slPrice":    sl_price,
                "status":     "open" if (tp_id or sl_id) else "error",
            })
    log.info(f"✅ [{trade_id}] fill@{fill} TP#{tp_id}@{tp_price} SL#{sl_id}@{sl_price}")

# ══════════════════════════════════════════════════════════════════════
# SECTION 10 — TRADE REGISTRY
# ══════════════════════════════════════════════════════════════════════

def register_trade(trade_id, entry_order_id, symbol,
                   tv_symbol, entry_price, qty):
    with _state_lock:
        trades[trade_id] = {
            "entryOrderId": entry_order_id,
            "tpOrderId":    None,
            "slOrderId":    None,
            "symbol":       symbol,
            "tvSymbol":     tv_symbol,
            "qty":          qty,
            "entryPrice":   entry_price,
            "tpPrice":      None,
            "slPrice":      None,
            "status":       "pending",
            "trailArmed":   False,
            "openTime":     datetime.now().isoformat(),
        }
    log.info(f"✅ Registered: {trade_id} @ {entry_price}")


def log_signal(action, trade_id, symbol, result):
    with _state_lock:
        signal_log.append({
            "time":    datetime.now().isoformat(),
            "action":  action,
            "tradeId": trade_id,
            "symbol":  symbol,
            "result":  str(result)[:200],
        })
        if len(signal_log) > 200:
            signal_log.pop(0)

# ══════════════════════════════════════════════════════════════════════
# SECTION 11 — WEBHOOK
# ══════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        log.info(f"📨 {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        action     = data.get("action",       "")
        tv_symbol  = data.get("symbol",       "MNQM2026")
        symbol     = tradovate_symbol(tv_symbol)
        qty        = int(data.get("qty",           CONTRACTS))
        order_type = data.get("orderType",    "Stop")
        price      = float(data.get("price",     0))
        stop_off   = int(data.get("stopOffset",    STOP_OFFSET_TICKS))
        trail_dist = int(data.get("trailDist",     TRAIL_DIST_TICKS))
        cur_price  = float(data.get("currentPrice", 0))
        trade_id   = data.get("tradeId", f"Auto_{int(time.time())}")
        result     = {}

        # ── short_entry ────────────────────────────────────────────────
        if action == "short_entry":

            if order_type == "Market":
                res = place_market_sell(symbol, qty)
                if "orderId" in res:
                    oid = res["orderId"]
                    register_trade(trade_id, oid, symbol, tv_symbol, price, qty)
                    threading.Thread(
                        target=attach_bracket,
                        args=(trade_id, price, qty),
                        daemon=True, name=f"bracket-{trade_id}"
                    ).start()
                    result = {"orderId": oid, "type": "Market"}
                else:
                    result = res

            else:
                sp = round(price - stop_off * TICK_SIZE, 2)
                res = place_sell_stop(symbol, qty, sp)
                if "orderId" in res:
                    oid = res["orderId"]
                    register_trade(trade_id, oid, symbol, tv_symbol, sp, qty)
                    threading.Thread(
                        target=attach_bracket,
                        args=(trade_id, sp, qty),
                        daemon=True, name=f"bracket-{trade_id}"
                    ).start()
                    result = {"orderId": oid, "stopPrice": sp}
                else:
                    result = res

        # ── trail_armed ────────────────────────────────────────────────
        elif action == "trail_armed":
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": f"{trade_id} not in registry"}

            elif trade.get("slOrderId"):
                # Normal: modify existing SL
                new_sl = round(cur_price + trail_dist * TICK_SIZE, 2)
                res    = modify_sl_order(trade["slOrderId"], new_sl)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["slPrice"]    = new_sl
                        trades[trade_id]["trailArmed"] = True
                result = {"newSL": new_sl, "res": res}

            else:
                # SL not yet placed — place new SL instantly
                entry  = trade.get("entryPrice", 0)
                sym    = trade["symbol"]
                qty_t  = trade["qty"]
                new_sl = round(cur_price + trail_dist * TICK_SIZE, 2)
                res    = place_sl_order(sym, qty_t, new_sl)
                sl_id  = res.get("orderId")
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["slOrderId"]  = sl_id
                        trades[trade_id]["slPrice"]    = new_sl
                        trades[trade_id]["trailArmed"] = True
                result = {"newSL": new_sl, "slOrderId": sl_id}

        # ── cancel_pending ─────────────────────────────────────────────
        elif action == "cancel_pending":
            with _state_lock:
                trade = trades.get(trade_id)

            if trade and trade["status"] == "pending":
                res = cancel_order(trade["entryOrderId"])
                with _state_lock:
                    trades[trade_id]["status"] = "cancelled"
                result = {"cancelled": trade_id, "res": res}
            else:
                # Fallback: cancel all working Sell Stops for this symbol
                try:
                    acc         = get_account_id()
                    open_orders = _get(f"order/list?accountId={acc}")
                    cancelled   = []
                    if isinstance(open_orders, list):
                        for o in open_orders:
                            if (o.get("symbol")    == symbol
                                    and o.get("ordStatus") == "Working"
                                    and o.get("action")    == "Sell"
                                    and o.get("orderType") == "Stop"):
                                cancel_order(o["orderId"])
                                cancelled.append(o["orderId"])
                    result = {"cancelled": cancelled}
                except Exception as e:
                    result = {"info": f"cancel fallback: {e}"}

        # ── close_all ──────────────────────────────────────────────────
        elif action == "close_all":
            closed = []
            with _state_lock:
                open_trades = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] == "open"
                }
            for tid, t in open_trades.items():
                if t.get("tpOrderId"): cancel_order(t["tpOrderId"])
                if t.get("slOrderId"): cancel_order(t["slOrderId"])
                close_position_market(t["symbol"], t["qty"])
                with _state_lock:
                    if tid in trades:
                        trades[tid]["status"] = "closed"
                closed.append(tid)
            result = {"closed": closed, "count": len(closed)}

        else:
            result = {"info": f"'{action}' ignored"}

        log_signal(action, trade_id, tv_symbol, result)
        log.info(f"✅ {action}[{trade_id}] → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception as e:
        log.exception(f"❌ Webhook: {e}")
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════════════════════════════════
# SECTION 12 — MANAGEMENT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

@app.route("/set-token", methods=["POST"])
def set_token():
    global _access_token, _token_expiry, _account_id
    data = request.get_json(force=True)
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    tok = data.get("token", "").strip()
    if not tok:
        return jsonify({"error": "token required"}), 400
    with _auth_lock:
        _access_token = tok
        _token_expiry = time.time() + 4500
        _account_id   = None
    _ws_reauth()
    log.info("✅ Token updated via /set-token")
    return jsonify({"status": "ok", "message": "Token updated"})


@app.route("/test-auth")
def test_auth():
    tok    = get_access_token()
    acc    = get_account_id()
    tok_ok = bool(tok and time.time() < _token_expiry - 120)
    exp    = (datetime.fromtimestamp(_token_expiry).isoformat()
              if _token_expiry else None)
    return jsonify({
        "token_status":   "✅ Active"          if tok_ok else "⚠️ Expired",
        "account_status": f"✅ Account: {acc}" if acc    else "❌ No account",
        "token_preview":  (tok[:35] + "...")   if tok    else None,
        "token_expires":  exp,
        "websocket":      "✅ Connected"        if _ws_connected else "⚠️ Disconnected",
        "mode":           "DEMO" if USE_DEMO else "LIVE",
        "username":       TRADOVATE_USERNAME,
        "active_trades":  sum(1 for t in trades.values()
                              if t["status"] in ("open","pending")),
        "total_trades":   len(trades),
    })


@app.route("/status")
def status():
    with _state_lock:
        snap = dict(trades)
    tok_ok = bool(_access_token and time.time() < _token_expiry - 120)
    return jsonify({
        "status":         "online",
        "mode":           "DEMO" if USE_DEMO else "LIVE",
        "token_active":   tok_ok,
        "websocket":      _ws_connected,
        "open_trades":    sum(1 for t in snap.values() if t["status"]=="open"),
        "pending_trades": sum(1 for t in snap.values() if t["status"]=="pending"),
        "total_trades":   len(snap),
        "total_signals":  len(signal_log),
        "time":           datetime.now().isoformat(),
    })


@app.route("/trades")
def get_trades():
    with _state_lock:
        return jsonify(dict(trades))


@app.route("/logs")
def get_logs():
    with _state_lock:
        return jsonify(list(reversed(signal_log[-100:])))

# ══════════════════════════════════════════════════════════════════════
# SECTION 13 — DASHBOARD
# ══════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    with _state_lock:
        snap = dict(trades)
        logs = list(reversed(signal_log[-50:]))

    open_c  = sum(1 for t in snap.values() if t["status"] == "open")
    pend_c  = sum(1 for t in snap.values() if t["status"] == "pending")
    done_c  = sum(1 for t in snap.values()
                  if t["status"] in ("closed","cancelled","error"))
    tok_ok  = bool(_access_token and time.time() < _token_expiry - 120)
    exp_str = (datetime.fromtimestamp(_token_expiry).strftime("%Y-%m-%d %H:%M UTC")
               if _token_expiry else "—")
    ws_ok   = _ws_connected

    ACTION_COLORS = {
        "short_entry":    "#f87171",
        "trail_armed":    "#a78bfa",
        "cancel_pending": "#fbbf24",
        "close_all":      "#fb923c",
    }

    trade_rows = ""
    active = sorted(
        [(tid, t) for tid, t in snap.items()
         if t["status"] in ("open","pending")],
        key=lambda x: x[1]["openTime"], reverse=True
    )
    for tid, t in active[:80]:
        sc    = f"s-{t['status']}"
        trail = "✅" if t.get("trailArmed") else "—"
        trade_rows += f"""<tr>
<td class="mono p">{tid}</td><td>{t['symbol']}</td><td class="r">Short</td>
<td>{t.get('entryPrice','—')}</td>
<td class="g">{t.get('tpPrice','—')}</td>
<td class="r">{t.get('slPrice','—')}</td>
<td class="{sc}">{t['status'].upper()}</td>
<td style="text-align:center">{trail}</td>
<td class="dim">{t.get('openTime','')[:19]}</td>
</tr>"""
    if not trade_rows:
        trade_rows = "<tr><td colspan='9' class='empty'>No active trades</td></tr>"

    log_rows = ""
    for e in logs:
        color = ACTION_COLORS.get(e["action"], "#dde0f0")
        log_rows += f"""<tr>
<td class="dim">{e['time'][11:19]}</td>
<td style="color:{color};font-weight:700">{e['action']}</td>
<td class="mono p">{e['tradeId']}</td>
<td>{e['symbol']}</td>
<td class="dim small">{e['result'][:130]}</td>
</tr>"""
    if not log_rows:
        log_rows = "<tr><td colspan='5' class='empty'>No signals yet</td></tr>"

    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot</title>
<meta http-equiv="refresh" content="5">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg0:#05050e;--bg1:#0c0c1c;--bg2:#121228;--bd:#1e1e38;
  --t0:#e8eaf6;--t1:#9497b8;--t2:#555870;
  --grn:#34d399;--red:#f87171;--ylw:#fbbf24;
  --org:#fb923c;--blu:#60a5fa;--pur:#a78bfa;
}}
body{{font-family:system-ui,sans-serif;background:var(--bg0);color:var(--t0);padding:20px;font-size:14px}}
h1{{font-size:1.3rem;color:var(--pur);margin-bottom:3px}}
.sub{{font-size:.75rem;color:var(--t2);margin-bottom:14px}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px;align-items:center}}
.badge{{padding:3px 12px;border-radius:20px;font-size:.7rem;font-weight:700;letter-spacing:.04em}}
.b-demo{{background:#0e2240;color:var(--blu)}}
.b-live{{background:#280f0f;color:var(--red)}}
.b-ok{{background:#071a10;color:var(--grn)}}
.b-warn{{background:#1f1500;color:var(--ylw)}}
.cfg{{background:var(--bg1);border:1px solid var(--bd);border-radius:8px;
     padding:10px 14px;font-size:.74rem;color:var(--t1);margin-bottom:18px;line-height:1.9}}
.cfg b{{color:var(--pur)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:10px;margin-bottom:22px}}
.card{{background:var(--bg1);border:1px solid var(--bd);border-radius:9px;padding:13px}}
.card .lbl{{font-size:.64rem;color:var(--t2);text-transform:uppercase;letter-spacing:.07em;margin-bottom:5px}}
.card .val{{font-size:1.6rem;font-weight:800}}
h2{{font-size:.78rem;color:var(--pur);margin-bottom:8px;text-transform:uppercase;letter-spacing:.1em}}
table{{width:100%;border-collapse:collapse;background:var(--bg1);border-radius:9px;overflow:hidden;margin-bottom:22px}}
th{{background:var(--bg2);padding:8px 11px;text-align:left;font-size:.63rem;color:var(--t2);text-transform:uppercase;letter-spacing:.08em}}
td{{padding:7px 11px;border-top:1px solid var(--bd)}}
.g{{color:var(--grn)}}.r{{color:var(--red)}}.b{{color:var(--blu)}}
.y{{color:var(--ylw)}}.o{{color:var(--org)}}.p{{color:var(--pur)}}
.dim{{color:var(--t2)}}.small{{font-size:.72rem}}
.mono{{font-family:monospace;font-size:.78rem}}
.empty{{color:var(--t2);text-align:center;padding:18px}}
.s-open{{color:var(--grn);font-weight:700}}
.s-pending{{color:var(--ylw);font-weight:700}}
.s-closed,.s-cancelled{{color:var(--t2)}}
.s-error{{color:var(--red)}}
</style></head><body>
<h1>MNQ Trading Bot</h1>
<p class="sub">Auto-refresh: 5s | {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'b-demo' if USE_DEMO else 'b-live'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'b-ok' if tok_ok else 'b-warn'}">TOKEN {'✅' if tok_ok else '⚠️'} · {exp_str}</span>
  <span class="badge {'b-ok' if ws_ok else 'b-warn'}">WS {'⚡ LIVE' if ws_ok else '⚠️ OFFLINE'}</span>
</div>
<div class="cfg">
<b>Strategy:</b>
Sell Stop {STOP_OFFSET_TICKS}t below close &nbsp;|&nbsp;
Cancel {CANCEL_AFTER_BARS} bars &nbsp;|&nbsp;
SL {SL_TICKS}t &nbsp;|&nbsp; TP {TP_TICKS}t &nbsp;|&nbsp;
Trail start {TRAIL_START_TICKS}t · dist {TRAIL_DIST_TICKS}t &nbsp;|&nbsp;
Fill: <span style="color:var(--grn)">{'⚡ WebSocket' if ws_ok else '📡 HTTP Poll'}</span>
</div>
<div class="cards">
<div class="card"><div class="lbl">Open</div><div class="val g">{open_c}</div></div>
<div class="card"><div class="lbl">Pending</div><div class="val y">{pend_c}</div></div>
<div class="card"><div class="lbl">Closed</div><div class="val p">{done_c}</div></div>
<div class="card"><div class="lbl">Signals</div><div class="val b">{len(signal_log)}</div></div>
<div class="card"><div class="lbl">Bot</div><div class="val g">ON</div></div>
</div>
<h2>Active Trades ({open_c + pend_c})</h2>
<table><tr>
<th>Trade ID</th><th>Symbol</th><th>Side</th>
<th>Entry</th><th>TP ✅</th><th>SL 🛑</th>
<th>Status</th><th>Trail</th><th>Time</th>
</tr>{trade_rows}</table>
<h2>Signal Log</h2>
<table><tr>
<th>Time</th><th>Action</th><th>Trade ID</th><th>Symbol</th><th>Result</th>
</tr>{log_rows}</table>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("=" * 60)
    log.info("  MNQ Trading Bot — WebSocket Edition")
    log.info(f"  Mode      : {'DEMO' if USE_DEMO else 'LIVE'}")
    log.info(f"  Port      : {port}")
    log.info(f"  SL/TP     : {SL_TICKS} / {TP_TICKS} ticks")
    log.info(f"  Trail     : start={TRAIL_START_TICKS}t dist={TRAIL_DIST_TICKS}t")
    log.info(f"  Fill      : WebSocket instant + HTTP fallback")
    log.info(f"  Token     : keep-alive 55 min")
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
