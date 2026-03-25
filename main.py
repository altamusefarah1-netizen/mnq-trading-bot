"""
╔══════════════════════════════════════════════════════════════════════╗
║          MNQ Trading Bot — Python Mirror of PineScript v6           ║
║                                                                      ║
║  Pine Script Logic → Python (1:1 match)                             ║
║                                                                      ║
║  Signal    : close < open  (no filters)                             ║
║  Order     : Sell Stop  =  close - 2 ticks                         ║
║  Cancel    : after 2 bars if unfilled                               ║
║  SL        : 15 ticks                                               ║
║  TP        : 120 ticks                                              ║
║  Trailing  : armed at 1 tick profit, distance 10 ticks             ║
║  Break Even: OFF                                                    ║
║  Session   : OFF                                                    ║
║  Date Rng  : OFF                                                    ║
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
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID",     "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET",
                        "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET",       "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO",             "true").lower() == "true"

# Tradovate API endpoints
DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
BASE_URL = DEMO_URL if USE_DEMO else LIVE_URL

# ── Strategy constants  (mirrors Pine Script "CONSTANTS" section) ─────────────
CONTRACTS        = 1       # Number of contracts
SL_TICKS         = 15      # Stop Loss ticks
TP_TICKS         = 120     # Take Profit ticks
STOP_OFFSET_TICKS= 2       # Sell Stop = close - 2 ticks
CANCEL_AFTER_BARS= 2       # Cancel unfilled order after 2 bars
TRAIL_START_TICKS= 1       # Trailing arms after 1 tick profit
TRAIL_DIST_TICKS = 10      # Trailing distance = 10 ticks
TICK_SIZE        = 0.25    # MNQ tick size (syminfo.mintick)

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
#               qty, entryPrice, tpPrice, slPrice, status,
#               trailArmed, openTime, bars } }
_state_lock = threading.Lock()
trades      = {}
signal_log  = []   # last 200 signals


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 ── SYMBOL CONVERTER
#              TradingView uses MNQM2026 — Tradovate uses MNQM6
# ══════════════════════════════════════════════════════════════════════════════

def tradovate_symbol(tv_symbol: str) -> str:
    """
    MNQM2026  →  MNQM6
    NQM2026   →  NQM6
    Env override: set TRADOVATE_SYMBOL to force a fixed symbol.
    """
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", tv_symbol)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"   # last digit of year = contract month
    return tv_symbol


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 ── AUTHENTICATION  (fully automated — no manual token copy-paste)
# ══════════════════════════════════════════════════════════════════════════════

def _renew_token(existing: str):
    """Silent renewal — no CAPTCHA triggered."""
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
    """Full credential login — fallback when renewal fails."""
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
        if "p-captcha" in d:
            log.warning("⚠️  CAPTCHA triggered — using existing token until manual refresh")
        else:
            log.error(f"Login failed: {d}")
    except Exception as e:
        log.error(f"Login exception: {e}")
    return None, 0


def get_access_token() -> str | None:
    """
    Returns a valid access token.
    Priority:
      1. Current token still valid      → return it
      2. Token near expiry              → silent renewal
      3. No token / renewal failed      → fresh credential login
      4. All failed (CAPTCHA)           → return stale token (manual fix needed)
    Thread-safe.
    """
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()

        # 1 — still valid (2-minute safety buffer)
        if _access_token and now < _token_expiry - 120:
            return _access_token

        # 2 — try silent renewal first
        if _access_token:
            tok, exp = _renew_token(_access_token)
            if tok:
                _access_token = tok
                _token_expiry = now + exp
                return _access_token

        # 3 — full login
        tok, exp = _fresh_login()
        if tok:
            _access_token = tok
            _token_expiry = now + exp
            return _access_token

        # 4 — all failed
        log.error("❌ All authentication methods failed")
        return _access_token or None


def get_account_id() -> int | None:
    """Fetch and cache Tradovate account ID."""
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
            log.info(f"✅ Account ID: {_account_id}")
            return _account_id
    except Exception as e:
        log.error(f"Account fetch error: {e}")
    return None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type":  "application/json",
    }


# ── Background token refresh every 60 minutes ─────────────────────────────────
def _auto_refresh():
    while True:
        time.sleep(3600)
        log.info("⏰ Scheduled token refresh (60-min cycle)...")
        get_access_token()

threading.Thread(target=_auto_refresh, daemon=True, name="token-refresh").start()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 ── API WRAPPER  (with automatic 401 retry)
# ══════════════════════════════════════════════════════════════════════════════

def _api_post(endpoint: str, body: dict, retry: bool = True) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.post(url, headers=_headers(), json=body, timeout=10)
        if r.status_code == 401 and retry:
            log.warning("401 received — forcing token refresh and retrying once...")
            global _token_expiry
            with _auth_lock:
                _token_expiry = 0           # force re-auth on next call
            return _api_post(endpoint, body, retry=False)
        return r.json()
    except Exception as e:
        log.error(f"POST /{endpoint} error: {e}")
        return {"error": str(e)}


def _api_get(endpoint: str) -> dict | list:
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = requests.get(url, headers=_headers(), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"GET /{endpoint} error: {e}")
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 ── ORDER EXECUTION  (mirrors Pine Script strategy.entry / exit)
# ══════════════════════════════════════════════════════════════════════════════

def place_sell_stop(symbol: str, qty: int, stop_price: float) -> dict:
    """
    Mirrors: strategy.entry(entryId, strategy.short, qty=contracts, stop=entryPrice)
    entryPrice = close - (stopOffsetTicks * tickSize)
    """
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Stop",
        "stopPrice":   round(stop_price, 2),
        "isAutomated": True,
    }
    log.info(f"→ SellStop {qty} {symbol} @ {stop_price}")
    return _api_post("order/placeorder", body)


def place_market_sell(symbol: str, qty: int) -> dict:
    """Mirrors: strategy.entry(entryId, strategy.short, qty=contracts)"""
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    }
    log.info(f"→ MarketSell {qty} {symbol}")
    return _api_post("order/placeorder", body)


def place_bracket(symbol: str, qty: int,
                  tp_price: float, sl_price: float) -> tuple[dict, dict]:
    """
    Mirrors: strategy.exit(exitId, loss=slTicks, profit=tpTicks)
    Places TP (Limit) and SL (Stop) simultaneously using parallel threads.
    """
    acc    = get_account_id()
    tp_res = [{}]
    sl_res = [{}]

    def _tp():
        tp_res[0] = _api_post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   acc,
            "action":      "Buy",
            "symbol":      symbol,
            "orderQty":    qty,
            "orderType":   "Limit",
            "price":       round(tp_price, 2),
            "isAutomated": True,
        })

    def _sl():
        sl_res[0] = _api_post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   acc,
            "action":      "Buy",
            "symbol":      symbol,
            "orderQty":    qty,
            "orderType":   "Stop",
            "stopPrice":   round(sl_price, 2),
            "isAutomated": True,
        })

    t1 = threading.Thread(target=_tp, daemon=True)
    t2 = threading.Thread(target=_sl, daemon=True)
    t1.start(); t2.start()
    t1.join(8);  t2.join(8)

    log.info(f"→ Bracket [{symbol}] TP@{tp_price} SL@{sl_price} | tp={tp_res[0]} sl={sl_res[0]}")
    return tp_res[0], sl_res[0]


def modify_stop_order(order_id: int, new_stop: float) -> dict:
    """
    Mirrors: strategy.exit(exitId, trail_points=..., trail_offset=...)
    Moves an existing SL Stop order to a new price.
    """
    log.info(f"→ ModifyStop #{order_id} → {new_stop}")
    return _api_post("order/modifyorder", {
        "orderId":   order_id,
        "orderType": "Stop",
        "stopPrice": round(new_stop, 2),
    })


def cancel_order(order_id: int) -> dict:
    """Mirrors: strategy.cancel(entryId)"""
    log.info(f"→ Cancel #{order_id}")
    return _api_post("order/cancelorder", {"orderId": order_id})


def market_close_position(symbol: str, qty: int) -> dict:
    """Market-close an open short position (Buy to close)."""
    acc = get_account_id()
    if not acc:
        return {"error": "no_account_id"}
    log.info(f"→ MarketClose {qty} {symbol}")
    return _api_post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    })


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 ── TRADE STATE HELPERS
#              Mirrors Pine Script array.push / array.set / array.remove
# ══════════════════════════════════════════════════════════════════════════════

def register_trade(trade_id: str, entry_order_id: int,
                   symbol: str, tv_symbol: str,
                   entry_price: float, qty: int):
    """
    Mirrors:
      array.push(tradeIds, tradeCounter)
      array.push(tradeEntry, entryPrice)
      ...
    """
    with _state_lock:
        trades[trade_id] = {
            "entryOrderId": entry_order_id,
            "tpOrderId":    None,
            "slOrderId":    None,
            "symbol":       symbol,       # Tradovate symbol (MNQM6)
            "tvSymbol":     tv_symbol,    # TradingView symbol (MNQM2026)
            "qty":          qty,
            "entryPrice":   entry_price,
            "tpPrice":      None,
            "slPrice":      None,
            "status":       "pending",    # pending → open → closed/cancelled
            "trailArmed":   False,        # mirrors tradeTrailArm array
            "openTime":     datetime.now().isoformat(),
        }
    log.info(f"✅ Trade registered: {trade_id} @ {entry_price}")


def get_fill_price(order_id: int, timeout: float = 8.0) -> float | None:
    """
    Poll Tradovate until the order is filled, then return the actual fill price.
    Polls every 0.4s for up to `timeout` seconds.
    Returns None if not filled within timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = _api_get(f"order/item?id={order_id}")
            status = result.get("ordStatus", "")
            if status == "Filled":
                # avgFillPrice is the actual execution price
                fill = result.get("avgFillPrice")
                if fill:
                    log.info(f"✅ Fill confirmed: order #{order_id} @ {fill}")
                    return float(fill)
        except Exception as e:
            log.warning(f"Fill poll error: {e}")
        time.sleep(0.4)
    log.warning(f"⚠️ Fill poll timeout for order #{order_id} — using estimated price")
    return None


def attach_bracket_to_trade(trade_id: str, entry_price: float, qty: int):
    """
    Runs in background thread.
    Mirrors: strategy.exit(exitId, loss=slTicks, profit=tpTicks)

    IMPORTANT: For Sell Stop orders we POLL for the actual fill price
    before placing the bracket — this ensures SL/TP are calculated
    from the real execution price, not the estimated stop price.

    For SHORT:
      TP = fill_price - (TP_TICKS * TICK_SIZE)
      SL = fill_price + (SL_TICKS * TICK_SIZE)
    """
    with _state_lock:
        trade = trades.get(trade_id)
    if not trade:
        log.error(f"attach_bracket: {trade_id} not found in registry")
        return

    symbol    = trade["symbol"]
    order_id  = trade.get("entryOrderId")

    # ── Poll for actual fill price ────────────────────────────────────────
    fill_price = None
    if order_id:
        fill_price = get_fill_price(order_id, timeout=10.0)

    # Fall back to estimated price if poll times out
    actual_price = fill_price if fill_price else entry_price
    log.info(f"Bracket [{trade_id}] using price={actual_price} "
             f"(fill={'real' if fill_price else 'estimated'})")

    # ── Calculate TP and SL from actual fill price ────────────────────────
    tp_price = round(actual_price - TP_TICKS * TICK_SIZE, 2)
    sl_price = round(actual_price + SL_TICKS * TICK_SIZE, 2)

    tp_r, sl_r = place_bracket(symbol, qty, tp_price, sl_price)
    tp_id = tp_r.get("orderId")
    sl_id = sl_r.get("orderId")

    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "entryPrice": actual_price,   # update with real fill price
                "tpOrderId":  tp_id,
                "slOrderId":  sl_id,
                "tpPrice":    tp_price,
                "slPrice":    sl_price,
                "status":     "open" if (tp_id or sl_id) else "pending",
            })
    log.info(f"✅ Bracket [{trade_id}] fill@{actual_price} "
             f"TP={tp_id}@{tp_price} SL={sl_id}@{sl_price}")


def log_signal(action: str, trade_id: str, symbol: str, result):
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


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 ── WEBHOOK ENDPOINT
#              Mirrors Pine Script alert() → TradingView → here
#
#  Actions received (same as Pine Script alert messages):
#    short_entry    → mirrors: strategy.entry(stop=entryPrice)
#    trail_armed    → mirrors: strategy.exit(trail_points, trail_offset)
#    cancel_pending → mirrors: strategy.cancel(entryId)
#    close_all      → mirrors: strategy.close_all()
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        log.info(f"📨 Incoming webhook: {data}")

        # Security check
        if data.get("secret") != WEBHOOK_SECRET:
            log.warning("⛔ Unauthorized webhook attempt")
            return jsonify({"error": "unauthorized"}), 401

        # ── Parse fields (mirrors Pine Script alert JSON fields) ──────────
        action     = data.get("action",      "")
        tv_symbol  = data.get("symbol",      "MNQM2026")
        symbol     = tradovate_symbol(tv_symbol)
        qty        = int(data.get("qty",         CONTRACTS))
        order_type = data.get("orderType",   "Stop")
        price      = float(data.get("price",   0))     # close price from Pine
        stop_off   = int(data.get("stopOffset",  STOP_OFFSET_TICKS))
        tp_ticks   = int(data.get("tpTicks",     TP_TICKS))
        sl_ticks   = int(data.get("slTicks",     SL_TICKS))
        trail_dist = int(data.get("trailDist",   TRAIL_DIST_TICKS))
        cur_price  = float(data.get("currentPrice", 0))
        be_price   = float(data.get("bePrice",  0))
        trade_id   = data.get("tradeId", f"Auto_{int(time.time())}")
        result     = {}

        # ──────────────────────────────────────────────────────────────────
        # ACTION: short_entry
        # Mirrors Pine Script:
        #   entryPrice = close - (stopOffsetTicks * tickSize)
        #   strategy.entry(entryId, strategy.short, qty=contracts, stop=entryPrice)
        # ──────────────────────────────────────────────────────────────────
        if action == "short_entry":

            if order_type == "Market":
                # Market short
                res = place_market_sell(symbol, qty)
                if "orderId" in res:
                    oid         = res["orderId"]
                    entry_price = price
                    register_trade(trade_id, oid, symbol, tv_symbol,
                                   entry_price, qty)
                    # Place bracket immediately in background thread
                    threading.Thread(
                        target = attach_bracket_to_trade,
                        args   = (trade_id, entry_price, qty),
                        daemon = True,
                        name   = f"bracket-{trade_id}",
                    ).start()
                    result = {"orderId": oid, "type": "Market", "bracket": "queued"}
                else:
                    result = res

            else:
                # Sell Stop  =  close - (stop_offset * tick_size)
                # Mirrors: entryPrice = close - (stopOffsetTicks * tickSize)
                entry_price = round(price - stop_off * TICK_SIZE, 2)
                res = place_sell_stop(symbol, qty, entry_price)
                if "orderId" in res:
                    oid = res["orderId"]
                    register_trade(trade_id, oid, symbol, tv_symbol,
                                   entry_price, qty)
                    # Bracket placed immediately (will activate when stop fills)
                    threading.Thread(
                        target = attach_bracket_to_trade,
                        args   = (trade_id, entry_price, qty),
                        daemon = True,
                        name   = f"bracket-{trade_id}",
                    ).start()
                    result = {"orderId": oid, "stopPrice": entry_price, "bracket": "queued"}
                else:
                    result = res

        # ──────────────────────────────────────────────────────────────────
        # ACTION: trail_armed
        # Mirrors Pine Script:
        #   if low <= (ePrice - trailStartTicks * tickSize):
        #       strategy.exit(exitId, trail_points=..., trail_offset=...)
        #       → moves SL to:  currentPrice + trailDistTicks * tickSize
        # ──────────────────────────────────────────────────────────────────
        elif action == "trail_armed":
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"trade {trade_id} not found in registry"}
                log.warning(result["error"])

            elif not trade.get("slOrderId"):
                result = {"error": f"no SL order for {trade_id} — bracket not yet placed"}
                log.warning(result["error"])

            else:
                # For SHORT: new_sl = currentPrice + trailDist * tickSize
                new_sl    = round(cur_price + trail_dist * TICK_SIZE, 2)
                sl_order  = trade["slOrderId"]
                modify_res = modify_stop_order(sl_order, new_sl)

                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["slPrice"]    = new_sl
                        trades[trade_id]["trailArmed"] = True

                log.info(f"✅ Trail armed [{trade_id}] SL#{sl_order} → {new_sl}")
                result = {"newSL": new_sl, "slOrderId": sl_order, "res": modify_res}

        # ──────────────────────────────────────────────────────────────────
        # ACTION: cancel_pending
        # Mirrors Pine Script:
        #   else if (bar_index - setBar) > cancelAfter:
        #       strategy.cancel(entryId)
        # ──────────────────────────────────────────────────────────────────
        elif action == "cancel_pending":
            with _state_lock:
                trade = trades.get(trade_id)

            if trade and trade["status"] == "pending":
                cancel_res = cancel_order(trade["entryOrderId"])
                with _state_lock:
                    trades[trade_id]["status"] = "cancelled"
                log.info(f"✅ Cancelled pending [{trade_id}]")
                result = {"cancelled": trade_id, "res": cancel_res}
            else:
                result = {"info": f"{trade_id} not pending or not found"}
                log.info(result["info"])

        # ──────────────────────────────────────────────────────────────────
        # ACTION: close_all
        # Mirrors: strategy.close_all()
        # ──────────────────────────────────────────────────────────────────
        elif action == "close_all":
            closed = []
            with _state_lock:
                open_trades = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] == "open"
                }
            for tid, t in open_trades.items():
                # Cancel TP and SL first
                if t.get("tpOrderId"):
                    cancel_order(t["tpOrderId"])
                if t.get("slOrderId"):
                    cancel_order(t["slOrderId"])
                # Market close the position
                market_close_position(t["symbol"], t["qty"])
                with _state_lock:
                    if tid in trades:
                        trades[tid]["status"] = "closed"
                closed.append(tid)
            result = {"closed": closed, "count": len(closed)}
            log.info(f"✅ close_all: {len(closed)} positions closed")

        # ──────────────────────────────────────────────────────────────────
        # All other actions (session_end, etc.) → ignored (filters OFF)
        # ──────────────────────────────────────────────────────────────────
        else:
            result = {"info": f"action '{action}' ignored — no filters active"}

        log_signal(action, trade_id, tv_symbol, result)
        log.info(f"✅ Done — {action} [{trade_id}] → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception as e:
        log.exception(f"❌ Webhook exception: {e}")
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 ── MANAGEMENT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/set-token", methods=["POST"])
def set_token():
    """
    Manual token override — used when CAPTCHA blocks automated login.
    POST { "secret": "...", "token": "eyJ..." }
    """
    global _access_token, _token_expiry, _account_id
    data = request.get_json(force=True)
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    tok = data.get("token", "").strip()
    if not tok:
        return jsonify({"error": "token field required"}), 400
    with _auth_lock:
        _access_token = tok
        _token_expiry = time.time() + 4500
        _account_id   = None          # reset so account ID is re-fetched
    log.info("✅ Token manually updated via /set-token")
    return jsonify({"status": "ok", "message": "Token updated successfully"})


@app.route("/test-auth")
def test_auth():
    """Diagnose authentication status."""
    tok    = get_access_token()
    acc    = get_account_id()
    tok_ok = bool(tok and time.time() < _token_expiry - 120)
    exp    = (datetime.fromtimestamp(_token_expiry).isoformat()
              if _token_expiry else None)
    return jsonify({
        "token_status":   "✅ Active"  if tok_ok else "⚠️ Expired or Missing",
        "account_status": f"✅ Account: {acc}" if acc else "❌ No account found",
        "token_preview":  (tok[:35] + "...") if tok else None,
        "token_expires":  exp,
        "mode":           "DEMO" if USE_DEMO else "LIVE",
        "username":       TRADOVATE_USERNAME,
        "active_trades":  sum(1 for t in trades.values()
                              if t["status"] in ("open", "pending")),
        "total_trades":   len(trades),
    })


@app.route("/status")
def status():
    with _state_lock:
        snap = dict(trades)
    tok_ok = bool(_access_token and time.time() < _token_expiry - 120)
    return jsonify({
        "status":          "online",
        "mode":            "DEMO" if USE_DEMO else "LIVE",
        "token_active":    tok_ok,
        "open_trades":     sum(1 for t in snap.values() if t["status"] == "open"),
        "pending_trades":  sum(1 for t in snap.values() if t["status"] == "pending"),
        "total_trades":    len(snap),
        "total_signals":   len(signal_log),
        "time":            datetime.now().isoformat(),
    })


@app.route("/trades")
def get_trades():
    with _state_lock:
        return jsonify(dict(trades))


@app.route("/logs")
def get_logs():
    with _state_lock:
        return jsonify(list(reversed(signal_log[-100:])))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 ── LIVE DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    with _state_lock:
        snap = dict(trades)
        logs = list(reversed(signal_log[-50:]))

    open_c  = sum(1 for t in snap.values() if t["status"] == "open")
    pend_c  = sum(1 for t in snap.values() if t["status"] == "pending")
    done_c  = sum(1 for t in snap.values()
                  if t["status"] in ("closed", "cancelled"))
    tok_ok  = bool(_access_token and time.time() < _token_expiry - 120)
    exp_str = (datetime.fromtimestamp(_token_expiry).strftime("%H:%M UTC")
               if _token_expiry else "—")

    ACTION_COLORS = {
        "short_entry":    "#f87171",
        "long_entry":     "#34d399",
        "trail_armed":    "#a78bfa",
        "cancel_pending": "#fbbf24",
        "close_all":      "#fb923c",
    }

    # ── Active trades rows ─────────────────────────────────────────────────
    active = sorted(
        [(tid, t) for tid, t in snap.items()
         if t["status"] in ("open", "pending")],
        key=lambda x: x[1]["openTime"],
        reverse=True,
    )
    trade_rows = ""
    for tid, t in active[:80]:
        sc         = f"s-{t['status']}"
        side_color = "#f87171" if t.get("side", "Sell") == "Sell" else "#34d399"
        trail_icon = "✅" if t.get("trailArmed") else "—"
        trade_rows += f"""
        <tr>
          <td class="mono p">{tid}</td>
          <td>{t['symbol']}</td>
          <td style="color:{side_color}">Sell</td>
          <td>{t.get('entryPrice', '—')}</td>
          <td class="g">{t.get('tpPrice', '—')}</td>
          <td class="r">{t.get('slPrice', '—')}</td>
          <td class="{sc}">{t['status'].upper()}</td>
          <td style="text-align:center">{trail_icon}</td>
          <td class="dim">{t.get('openTime','')[:19]}</td>
        </tr>"""
    if not trade_rows:
        trade_rows = "<tr><td colspan='9' class='empty'>No active trades</td></tr>"

    # ── Signal log rows ────────────────────────────────────────────────────
    log_rows = ""
    for e in logs:
        color = ACTION_COLORS.get(e["action"], "#dde0f0")
        log_rows += f"""
        <tr>
          <td class="dim">{e['time'][11:19]}</td>
          <td style="color:{color};font-weight:700">{e['action']}</td>
          <td class="mono p">{e['tradeId']}</td>
          <td>{e['symbol']}</td>
          <td class="dim small">{e['result'][:130]}</td>
        </tr>"""
    if not log_rows:
        log_rows = "<tr><td colspan='5' class='empty'>No signals yet</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MNQ Bot</title>
<meta http-equiv="refresh" content="5">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg0: #05050e; --bg1: #0c0c1c; --bg2: #121228;
    --bd:  #1e1e38;
    --t0:  #e8eaf6; --t1:  #9497b8; --t2:  #555870;
    --grn: #34d399; --red: #f87171; --ylw: #fbbf24;
    --org: #fb923c; --blu: #60a5fa; --pur: #a78bfa;
  }}
  body  {{ font-family: system-ui, sans-serif; background: var(--bg0);
           color: var(--t0); padding: 20px; font-size: 14px; }}
  h1    {{ font-size: 1.3rem; color: var(--pur); margin-bottom: 3px; }}
  .sub  {{ font-size: .75rem; color: var(--t2); margin-bottom: 14px; }}

  /* Badges */
  .badges {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 18px; }}
  .badge  {{ padding: 3px 12px; border-radius: 20px; font-size: .7rem;
             font-weight: 700; letter-spacing: .04em; }}
  .b-demo {{ background: #0e2240; color: var(--blu); }}
  .b-live {{ background: #280f0f; color: var(--red); }}
  .b-ok   {{ background: #071a10; color: var(--grn); }}
  .b-warn {{ background: #1f1500; color: var(--ylw); }}

  /* Settings bar */
  .settings {{
    background: var(--bg1); border: 1px solid var(--bd);
    border-radius: 8px; padding: 10px 14px;
    font-size: .74rem; color: var(--t1);
    margin-bottom: 18px; line-height: 1.9;
  }}
  .settings b {{ color: var(--pur); }}

  /* Cards */
  .cards {{ display: grid;
            grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
            gap: 10px; margin-bottom: 22px; }}
  .card  {{ background: var(--bg1); border: 1px solid var(--bd);
            border-radius: 9px; padding: 13px; }}
  .card .lbl {{ font-size: .64rem; color: var(--t2);
                text-transform: uppercase; letter-spacing: .07em;
                margin-bottom: 5px; }}
  .card .val {{ font-size: 1.6rem; font-weight: 800; }}

  /* Tables */
  h2    {{ font-size: .78rem; color: var(--pur); margin-bottom: 8px;
           text-transform: uppercase; letter-spacing: .1em; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--bg1);
           border-radius: 9px; overflow: hidden; margin-bottom: 22px; }}
  th    {{ background: var(--bg2); padding: 8px 11px; text-align: left;
           font-size: .63rem; color: var(--t2);
           text-transform: uppercase; letter-spacing: .08em; }}
  td    {{ padding: 7px 11px; border-top: 1px solid var(--bd); }}

  /* Utility */
  .g    {{ color: var(--grn); }} .r {{ color: var(--red); }}
  .b    {{ color: var(--blu); }} .y {{ color: var(--ylw); }}
  .o    {{ color: var(--org); }} .p {{ color: var(--pur); }}
  .dim  {{ color: var(--t2); }} .small {{ font-size: .72rem; }}
  .mono {{ font-family: monospace; font-size: .78rem; }}
  .empty {{ color: var(--t2); text-align: center; padding: 18px; }}
  .s-open      {{ color: var(--grn); font-weight: 700; }}
  .s-pending   {{ color: var(--ylw); font-weight: 700; }}
  .s-closed    {{ color: var(--t2); }}
  .s-cancelled {{ color: var(--red); }}
</style>
</head><body>

<h1>MNQ Trading Bot</h1>
<p class="sub">Auto-refresh: 5s &nbsp;|&nbsp;
  {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>

<div class="badges">
  <span class="badge {'b-demo' if USE_DEMO else 'b-live'}">
    {'DEMO' if USE_DEMO else 'LIVE'}
  </span>
  <span class="badge {'b-ok' if tok_ok else 'b-warn'}">
    TOKEN {'✅ ACTIVE' if tok_ok else '⚠️ EXPIRED'} · {exp_str}
  </span>
</div>

<div class="settings">
  <b>Strategy settings:</b> &nbsp;
  Sell Stop offset = {STOP_OFFSET_TICKS} ticks &nbsp;|&nbsp;
  Cancel after = {CANCEL_AFTER_BARS} bars &nbsp;|&nbsp;
  SL = {SL_TICKS} ticks &nbsp;|&nbsp;
  TP = {TP_TICKS} ticks &nbsp;|&nbsp;
  Trail start = {TRAIL_START_TICKS} tick · dist = {TRAIL_DIST_TICKS} ticks &nbsp;|&nbsp;
  Break Even <span style="color:var(--red)">OFF</span> &nbsp;|&nbsp;
  Session filter <span style="color:var(--red)">OFF</span> &nbsp;|&nbsp;
  Date filter <span style="color:var(--red)">OFF</span>
</div>

<div class="cards">
  <div class="card"><div class="lbl">Open</div>
    <div class="val g">{open_c}</div></div>
  <div class="card"><div class="lbl">Pending</div>
    <div class="val o">{pend_c}</div></div>
  <div class="card"><div class="lbl">Closed</div>
    <div class="val p">{done_c}</div></div>
  <div class="card"><div class="lbl">Signals</div>
    <div class="val y">{len(signal_log)}</div></div>
  <div class="card"><div class="lbl">Status</div>
    <div class="val g">ON</div></div>
</div>

<h2>Active Trades &nbsp;<span style="color:var(--t2);font-weight:400">
  ({open_c + pend_c})</span></h2>
<table>
  <tr>
    <th>Trade ID</th><th>Symbol</th><th>Side</th>
    <th>Entry</th><th>TP ✅</th><th>SL 🛑</th>
    <th>Status</th><th>Trail</th><th>Time</th>
  </tr>
  {trade_rows}
</table>

<h2>Signal Log</h2>
<table>
  <tr>
    <th>Time</th><th>Action</th><th>Trade ID</th>
    <th>Symbol</th><th>Result</th>
  </tr>
  {log_rows}
</table>

</body></html>"""

    return html


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info("=" * 65)
    log.info(f"  MNQ Trading Bot — Starting")
    log.info(f"  Mode    : {'DEMO' if USE_DEMO else 'LIVE'}")
    log.info(f"  Port    : {port}")
    log.info(f"  SL/TP   : {SL_TICKS} / {TP_TICKS} ticks")
    log.info(f"  Trail   : start={TRAIL_START_TICKS}t  dist={TRAIL_DIST_TICKS}t")
    log.info(f"  Cancel  : after {CANCEL_AFTER_BARS} bars")
    log.info("=" * 65)
    app.run(host="0.0.0.0", port=port, debug=False)
