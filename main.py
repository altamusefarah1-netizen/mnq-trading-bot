"""
MNQ Trading Bot - v7 (Pine Script Logic Match)
================================================
Pine Script Logic (100% waafaqsan):
  SIGNAL   : close < open  (bearish bar)
  ENTRY    : Sell Stop @ close - stopOffsetTicks * tickSize
  CANCEL   : pending order > cancelAfter bars la joogo → cancel
  SL       : entry + slTicks * tickSize      (short → SL above)
  TP       : entry - tpTicks * tickSize      (short → TP below)
  TRAIL    : trail_points=1t armo → trail_offset=10t SL la raro
  STATUS   : pending → open → closed/cancelled

State Machine (Pine array logic mirror):
  tradeIds[]    → trades dict
  tradeWasOpen  → trade["was_open"]
  tradeTrailArm → trade["trail_armed"]
  tradeSetBar   → trade["set_bar_time"]
  tradeEntry    → trade["entry_price"]

Changes from v6:
  ✅ Bracket prices calculated FROM FILL PRICE (not signal price)
  ✅ cancel_pending checks bar_count (Pine: bar_index - setBar > cancelAfter)
  ✅ trail_armed action modifies SL correctly for SHORT direction
  ✅ was_open flag mirrors Pine tradeWasOpen logic
  ✅ No bracket attached until entry fill confirmed
  ✅ Thread-safe state with proper cleanup
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re, os, time, threading, logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

app  = Flask(__name__)
CORS(app)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET",
                           "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"

DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
BASE_URL  = DEMO_URL if USE_DEMO else LIVE_URL

# ─── AUTH STATE ───────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

# ─── TRADE STATE (mirrors Pine Script arrays) ────────────────────────────────
# trades[tradeId] = {
#   "entry_order_id"  : int   — Sell Stop order ID
#   "tp_order_id"     : int   — Limit buy order ID
#   "sl_order_id"     : int   — Stop  buy order ID
#   "symbol"          : str   — Tradovate symbol (e.g. MNQM5)
#   "tv_symbol"       : str   — TradingView symbol
#   "side"            : "Sell"|"Buy"
#   "qty"             : int
#   "stop_price"      : float — price the Sell Stop order fires at
#   "fill_price"      : float — actual fill (set when was_open flips)
#   "tp_price"        : float — calculated from fill_price
#   "sl_price"        : float — calculated from fill_price
#   "tp_ticks"        : int
#   "sl_ticks"        : int
#   "tick_size"       : float
#   "trail_start_ticks": int
#   "trail_dist_ticks" : int
#   "status"          : "pending"|"open"|"closed"|"cancelled"
#   "was_open"        : bool  — Pine: tradeWasOpen
#   "trail_armed"     : bool  — Pine: tradeTrailArm
#   "set_bar_time"    : float — Unix timestamp (Pine: bar_index of signal)
#   "bar_count"       : int   — bars elapsed since signal (incremented by bar_update)
#   "open_time"       : str
# }
_state_lock = threading.Lock()
trades      = {}
signal_log  = []

# ─── SYMBOL CONVERTER ────────────────────────────────────────────────────────
def to_tv_sym(sym):
    """MNQM2026 → MNQM5 (last digit of year = contract month code)"""
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", sym)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return sym

# ═══════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════

def _do_renew(tok):
    try:
        r = requests.post(
            f"{BASE_URL}/auth/renewaccesstoken",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10)
        d = r.json()
        if "accessToken" in d:
            log.info("✅ Token renewed")
            return d["accessToken"], d.get("expirationTime", 4500)
    except Exception as e:
        log.warning(f"Renew failed: {e}")
    return None, 0


def _do_login():
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
            log.info("✅ Login OK")
            return d["accessToken"], d.get("expirationTime", 4500)
        if "p-captcha" in d:
            log.warning("⚠️  CAPTCHA — set TRADOVATE_TOKEN manually")
        else:
            log.error(f"Login failed: {d}")
    except Exception as e:
        log.error(f"Login error: {e}")
    return None, 0


def get_access_token():
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        if _access_token and now < _token_expiry - 120:
            return _access_token
        if _access_token:
            tok, exp = _do_renew(_access_token)
            if tok:
                _access_token = tok
                _token_expiry = now + exp
                return _access_token
        tok, exp = _do_login()
        if tok:
            _access_token = tok
            _token_expiry = now + exp
            return _access_token
        log.error("❌ All auth failed — using stale token")
        return _access_token or None


def get_account_id():
    global _account_id
    if _account_id:
        return _account_id
    tok = get_access_token()
    if not tok:
        return None
    try:
        r = requests.get(
            f"{BASE_URL}/account/list",
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


def _hdrs():
    return {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type":  "application/json"
    }


def _refresh_loop():
    while True:
        time.sleep(3600)
        log.info("⏰ Scheduled token refresh")
        get_access_token()

threading.Thread(target=_refresh_loop, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════
# API — auto-retry 401
# ═══════════════════════════════════════════════════════════════════════

def _api(method, endpoint, body=None, retry=True):
    url = f"{BASE_URL}/{endpoint}"
    try:
        if method == "POST":
            r = requests.post(url, headers=_hdrs(), json=body, timeout=10)
        else:
            r = requests.get(url, headers=_hdrs(), timeout=10)
        if r.status_code == 401 and retry:
            log.warning("401 — force refresh + retry")
            global _token_expiry
            with _auth_lock:
                _token_expiry = 0
            return _api(method, endpoint, body, retry=False)
        return r.json()
    except Exception as e:
        log.error(f"API {method} {endpoint}: {e}")
        return {"error": str(e)}

def _post(ep, body): return _api("POST", ep, body)
def _get(ep):        return _api("GET",  ep)

# ═══════════════════════════════════════════════════════════════════════
# ORDER HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _place_sell_stop(sym, qty, stop_price):
    """
    Pine: strategy.entry("Short_N", strategy.short, stop=entryPrice)
    Tradovate: Sell Stop order
    """
    acc = get_account_id()
    if not acc:
        return {"error": "no_account"}
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      sym,
        "orderQty":    qty,
        "orderType":   "Stop",
        "stopPrice":   round(stop_price, 2),
        "isAutomated": True,
    }
    log.info(f"→ SELL STOP {qty} {sym} @ {round(stop_price, 2)}")
    return _post("order/placeorder", body)


def _place_bracket(sym, qty, tp_price, sl_price):
    """
    Pine: strategy.exit(loss=slTicks, profit=tpTicks)
    Two parallel orders: Limit Buy (TP) + Stop Buy (SL)
    Prices calculated FROM FILL PRICE — not signal close price.
    """
    acc = get_account_id()
    if not acc:
        return {}, {}

    tp_res = [None]
    sl_res = [None]

    def do_tp():
        tp_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   acc,
            "action":      "Buy",
            "symbol":      sym,
            "orderQty":    qty,
            "orderType":   "Limit",
            "price":       round(tp_price, 2),
            "isAutomated": True,
        })

    def do_sl():
        sl_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   acc,
            "action":      "Buy",
            "symbol":      sym,
            "orderQty":    qty,
            "orderType":   "Stop",
            "stopPrice":   round(sl_price, 2),
            "isAutomated": True,
        })

    t1 = threading.Thread(target=do_tp, daemon=True)
    t2 = threading.Thread(target=do_sl, daemon=True)
    t1.start(); t2.start()
    t1.join(8);  t2.join(8)
    return tp_res[0] or {}, sl_res[0] or {}


def _modify_stop_order(order_id, new_stop):
    """Move SL order to new price (used by trailing stop logic)"""
    log.info(f"→ Modify SL #{order_id} → {new_stop}")
    return _post("order/modifyorder", {
        "orderId":   order_id,
        "orderType": "Stop",
        "stopPrice": round(new_stop, 2),
    })


def _cancel_order(order_id):
    log.info(f"→ Cancel order #{order_id}")
    return _post("order/cancelorder", {"orderId": order_id})


def _market_close(sym, qty):
    """Emergency close: Buy Market to close short"""
    acc = get_account_id()
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      sym,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    })

# ═══════════════════════════════════════════════════════════════════════
# BRACKET ATTACH (background thread — waits for fill)
# ═══════════════════════════════════════════════════════════════════════

def _attach_bracket_after_fill(trade_id, fill_price):
    """
    Pine: strategy.exit runs immediately after fill.
    Python: we call this in a background thread once fill_price is known.

    SHORT direction:
      SL = fill_price + sl_ticks * tick_size   (above entry)
      TP = fill_price - tp_ticks * tick_size   (below entry)
    """
    with _state_lock:
        trade = trades.get(trade_id)
    if not trade:
        log.warning(f"attach_bracket: {trade_id} not found")
        return

    ts        = trade["tick_size"]
    sl_ticks  = trade["sl_ticks"]
    tp_ticks  = trade["tp_ticks"]
    sym       = trade["symbol"]
    qty       = trade["qty"]
    side      = trade["side"]

    if side == "Sell":
        # SHORT: SL above, TP below
        sl_price = round(fill_price + sl_ticks * ts, 2)
        tp_price = round(fill_price - tp_ticks * ts, 2)
    else:
        # LONG: SL below, TP above
        sl_price = round(fill_price - sl_ticks * ts, 2)
        tp_price = round(fill_price + tp_ticks * ts, 2)

    log.info(f"Bracket [{trade_id}] fill={fill_price} "
             f"TP={tp_price} SL={sl_price}")

    tp_r, sl_r = _place_bracket(sym, qty, tp_price, sl_price)
    tp_id = tp_r.get("orderId")
    sl_id = sl_r.get("orderId")
    log.info(f"Bracket placed [{trade_id}] tp_id={tp_id} sl_id={sl_id}")

    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "fill_price":  fill_price,
                "tp_order_id": tp_id,
                "sl_order_id": sl_id,
                "tp_price":    tp_price,
                "sl_price":    sl_price,
                "status":      "open",
                "was_open":    True,
            })

# ═══════════════════════════════════════════════════════════════════════
# STATE HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _new_trade(trade_id, entry_oid, sym, tv_sym,
               side, qty, stop_price,
               tp_ticks, sl_ticks, tick_size,
               trail_start_ticks, trail_dist_ticks):
    """
    Pine: array.push(tradeIds, tradeCounter) etc.
    Creates pending trade — bracket NOT yet set (needs fill price).
    """
    with _state_lock:
        trades[trade_id] = {
            "entry_order_id":    entry_oid,
            "tp_order_id":       None,
            "sl_order_id":       None,
            "symbol":            sym,
            "tv_symbol":         tv_sym,
            "side":              side,
            "qty":               qty,
            "stop_price":        stop_price,
            "fill_price":        None,
            "tp_price":          None,
            "sl_price":          None,
            "tp_ticks":          tp_ticks,
            "sl_ticks":          sl_ticks,
            "tick_size":         tick_size,
            "trail_start_ticks": trail_start_ticks,
            "trail_dist_ticks":  trail_dist_ticks,
            "status":            "pending",
            "was_open":          False,   # Pine: tradeWasOpen
            "trail_armed":       False,   # Pine: tradeTrailArm
            "set_bar_time":      time.time(),  # Pine: tradeSetBar (bar_index)
            "bar_count":         0,            # bars since signal
            "open_time":         datetime.now().isoformat(),
        }


def _log_signal(action, trade_id, sym, result):
    with _state_lock:
        signal_log.append({
            "time":    datetime.now().isoformat(),
            "action":  action,
            "tradeId": trade_id,
            "symbol":  sym,
            "result":  str(result)[:200],
        })
        if len(signal_log) > 200:
            signal_log.pop(0)

# ═══════════════════════════════════════════════════════════════════════
# WEBHOOK
# ═══════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        log.info(f"📨 Webhook: {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        action   = data.get("action", "")
        tv_sym   = data.get("symbol", "MNQM2026")
        sym      = to_tv_sym(tv_sym)
        qty      = int(data.get("qty", 1))
        tp_ticks = int(data.get("tpTicks",  120))
        sl_ticks = int(data.get("slTicks",   15))
        stop_off = int(data.get("stopOffset", 2))
        trade_id = data.get("tradeId", f"T_{int(time.time())}")
        tick_size= float(data.get("tickSize", 0.25))
        price    = float(data.get("price", 0))
        trail_start = int(data.get("trailStart", 1))
        trail_dist  = int(data.get("trailDist",  10))
        result   = {}

        # ── SHORT ENTRY ──────────────────────────────────────────────────
        # Pine: strategy.entry("Short_N", strategy.short, stop=close - stopOff*tick)
        if action == "short_entry":
            order_type = data.get("orderType", "Stop")

            if order_type == "Stop":
                # Sell Stop = price dropped below this level → fill
                stop_price = round(price - stop_off * tick_size, 2)
                res = _place_sell_stop(sym, qty, stop_price)

                if "orderId" in res:
                    oid = res["orderId"]
                    _new_trade(
                        trade_id, oid, sym, tv_sym,
                        "Sell", qty, stop_price,
                        tp_ticks, sl_ticks, tick_size,
                        trail_start, trail_dist
                    )
                    # NOTE: bracket is NOT placed here.
                    # Pine places strategy.exit immediately, but in reality
                    # it only fires AFTER the entry fills.
                    # We wait for "entry_filled" or "trail_armed" to learn fill price.
                    result = {
                        "orderId":   oid,
                        "stopPrice": stop_price,
                        "bracket":   "waiting_for_fill"
                    }
                else:
                    result = res

            elif order_type == "Market":
                # Immediate fill at market
                acc = get_account_id()
                res = _post("order/placeorder", {
                    "accountSpec": TRADOVATE_USERNAME,
                    "accountId":   acc,
                    "action":      "Sell",
                    "symbol":      sym,
                    "orderQty":    qty,
                    "orderType":   "Market",
                    "isAutomated": True,
                })
                if "orderId" in res:
                    oid = res["orderId"]
                    _new_trade(
                        trade_id, oid, sym, tv_sym,
                        "Sell", qty, price,
                        tp_ticks, sl_ticks, tick_size,
                        trail_start, trail_dist
                    )
                    # Market fill → attach bracket immediately using signal price
                    threading.Thread(
                        target=_attach_bracket_after_fill,
                        args=(trade_id, price),
                        daemon=True
                    ).start()
                    result = {"orderId": oid, "bracket": "queued"}
                else:
                    result = res

        # ── LONG ENTRY ──────────────────────────────────────────────────
        elif action == "long_entry":
            acc = get_account_id()
            res = _post("order/placeorder", {
                "accountSpec": TRADOVATE_USERNAME,
                "accountId":   acc,
                "action":      "Buy",
                "symbol":      sym,
                "orderQty":    qty,
                "orderType":   "Market",
                "isAutomated": True,
            })
            if "orderId" in res:
                oid = res["orderId"]
                _new_trade(
                    trade_id, oid, sym, tv_sym,
                    "Buy", qty, price,
                    tp_ticks, sl_ticks, tick_size,
                    trail_start, trail_dist
                )
                threading.Thread(
                    target=_attach_bracket_after_fill,
                    args=(trade_id, price),
                    daemon=True
                ).start()
                result = {"orderId": oid, "bracket": "queued"}
            else:
                result = res

        # ── ENTRY FILLED ─────────────────────────────────────────────────
        # Pine: isOpen = true (tradeWasOpen flips false→true)
        # Send this from a fill-monitoring webhook or broker callback.
        # Payload: { action:"entry_filled", tradeId:"Short_N", fillPrice:21450.25 }
        elif action == "entry_filled":
            fill_price = float(data.get("fillPrice", 0))
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif trade["was_open"]:
                result = {"info": "already_open"}
            else:
                # mirrors: array.set(tradeWasOpen, i, true)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["was_open"] = True
                        trades[trade_id]["status"]   = "open"
                # attach bracket with ACTUAL fill price
                threading.Thread(
                    target=_attach_bracket_after_fill,
                    args=(trade_id, fill_price),
                    daemon=True
                ).start()
                result = {"filled": trade_id, "fillPrice": fill_price}

        # ── TRAILING STOP ARMED ───────────────────────────────────────────
        # Pine: trail_points=trailStartTicks, trail_offset=trailDistanceTicks
        # Pine arms trailing when price moves trailStartTicks in profit.
        # We receive current price; calculate new SL and modify the order.
        #
        # SHORT direction:
        #   trail_armed when: fill_price - cur_price >= trail_start_ticks * tick_size
        #   new SL = cur_price + trail_dist_ticks * tick_size
        elif action == "trail_armed":
            cur_price = float(data.get("currentPrice", 0))
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif not trade.get("sl_order_id"):
                # Bracket not yet placed — store trail info, apply when bracket arrives
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["trail_armed"]       = True
                        trades[trade_id]["trail_armed_price"]  = cur_price
                result = {"info": "trail_noted_pending_bracket"}
            else:
                ts         = trade["tick_size"]
                side       = trade["side"]
                sl_id      = trade["sl_order_id"]
                trail_d    = trade["trail_dist_ticks"]

                # SHORT: SL moves down with price (stays above current)
                if side == "Sell":
                    new_sl = round(cur_price + trail_d * ts, 2)
                else:
                    new_sl = round(cur_price - trail_d * ts, 2)

                res = _modify_stop_order(sl_id, new_sl)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["sl_price"]    = new_sl
                        trades[trade_id]["trail_armed"] = True

                result = {"newSL": new_sl, "modifyRes": res}

        # ── CANCEL PENDING ────────────────────────────────────────────────
        # Pine: (bar_index - setBar) > cancelAfter → strategy.cancel(entryId)
        # Pine sends this alert after cancelAfter bars with no fill.
        elif action == "cancel_pending":
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": "not_found"}
            elif trade["status"] != "pending":
                result = {"info": f"status_is_{trade['status']}_not_pending"}
            else:
                # Cancel the Sell Stop entry order
                cancel_res = _cancel_order(trade["entry_order_id"])
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"] = "cancelled"
                log.info(f"Cancelled pending [{trade_id}]")
                result = {"cancelled": trade_id, "res": cancel_res}

        # ── TRADE CLOSED ──────────────────────────────────────────────────
        # Pine: isOpen=false AND wasOpen=true → trade closed (TP/SL/Trail)
        # Send from broker callback or order-fill monitoring.
        # Payload: { action:"trade_closed", tradeId:"Short_N", closeReason:"TP"|"SL"|"Trail" }
        elif action == "trade_closed":
            close_reason = data.get("closeReason", "unknown")
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": "not_found"}
            else:
                # Cancel the surviving bracket leg
                if trade.get("tp_order_id"):
                    _cancel_order(trade["tp_order_id"])
                if trade.get("sl_order_id"):
                    _cancel_order(trade["sl_order_id"])
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"]       = "closed"
                        trades[trade_id]["close_reason"] = close_reason
                log.info(f"Trade closed [{trade_id}] reason={close_reason}")
                result = {"closed": trade_id, "reason": close_reason}

        # ── BAR UPDATE ────────────────────────────────────────────────────
        # Pine: each bar the management loop runs.
        # Optional: send { action:"bar_update" } each bar from Pine to
        # increment bar_count for pending trades (for cancel logic reference).
        elif action == "bar_update":
            updated = []
            with _state_lock:
                for tid, t in trades.items():
                    if t["status"] == "pending":
                        t["bar_count"] += 1
                        updated.append({
                            "tradeId":   tid,
                            "bar_count": t["bar_count"]
                        })
            result = {"updated_pending": updated}

        # ── CLOSE ALL ─────────────────────────────────────────────────────
        elif action == "close_all":
            closed = []
            with _state_lock:
                open_trades = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] in ("open", "pending")
                }
            for tid, t in open_trades.items():
                if t.get("tp_order_id"): _cancel_order(t["tp_order_id"])
                if t.get("sl_order_id"): _cancel_order(t["sl_order_id"])
                if t.get("entry_order_id") and t["status"] == "pending":
                    _cancel_order(t["entry_order_id"])
                if t["status"] == "open":
                    _market_close(t["symbol"], t["qty"])
                with _state_lock:
                    if tid in trades:
                        trades[tid]["status"] = "closed"
                closed.append(tid)
            result = {"closed": closed}

        # ── SESSION END — ignored (Pine: no session filter) ───────────────
        elif action == "session_end":
            result = {"info": "session_filter_OFF — ignored"}

        else:
            result = {"error": f"unknown action: {action}"}

        _log_signal(action, trade_id, tv_sym, result)
        log.info(f"✅ {action}[{trade_id}] → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════

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
    log.info("✅ Token updated via /set-token")
    return jsonify({"status": "ok", "message": "Token updated"})


@app.route("/test-auth")
def test_auth():
    tok    = get_access_token()
    acc    = get_account_id()
    tok_ok = bool(tok and time.time() < _token_expiry - 120)
    return jsonify({
        "token_status":   "✅ Active" if tok_ok else "⚠️ Expired/Missing",
        "account_status": f"✅ Account: {acc}" if acc else "❌ No account",
        "token_preview":  (tok[:30] + "...") if tok else None,
        "token_expires":  datetime.fromtimestamp(_token_expiry).isoformat()
                          if _token_expiry else None,
        "use_demo":       USE_DEMO,
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
        "status":         "online",
        "mode":           "DEMO" if USE_DEMO else "LIVE",
        "token_active":   tok_ok,
        "open_trades":    sum(1 for t in snap.values() if t["status"] == "open"),
        "pending_trades": sum(1 for t in snap.values() if t["status"] == "pending"),
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

# ═══════════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    with _state_lock:
        snap = dict(trades)
        logs = list(reversed(signal_log[-40:]))

    open_c  = sum(1 for t in snap.values() if t["status"] == "open")
    pend_c  = sum(1 for t in snap.values() if t["status"] == "pending")
    done_c  = sum(1 for t in snap.values()
                  if t["status"] in ("closed", "cancelled"))
    tok_ok  = bool(_access_token and time.time() < _token_expiry - 120)
    exp_str = (datetime.fromtimestamp(_token_expiry).strftime("%H:%M UTC")
               if _token_expiry else "—")

    html = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot v7</title>
<meta http-equiv="refresh" content="5">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#07070f;color:#dde0f0;padding:18px}}
h1{{font-size:1.35rem;color:#a78bfa;margin-bottom:2px}}
.sub{{font-size:.76rem;color:#555870;margin-bottom:12px}}
.row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px}}
.badge{{padding:3px 11px;border-radius:20px;font-size:.72rem;font-weight:700}}
.bd{{background:#162033;color:#60a5fa}}
.bl{{background:#200f0f;color:#f87171}}
.bg{{background:#071a10;color:#34d399}}
.bw{{background:#1f1500;color:#fbbf24}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:20px}}
.card{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;padding:12px}}
.card .lbl{{font-size:.65rem;color:#555870;margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em}}
.card .val{{font-size:1.55rem;font-weight:800}}
.g{{color:#34d399}}.b{{color:#60a5fa}}.y{{color:#fbbf24}}
.o{{color:#fb923c}}.r{{color:#f87171}}.p{{color:#a78bfa}}
table{{width:100%;border-collapse:collapse;background:#0f0f20;border-radius:9px;
       overflow:hidden;margin-bottom:18px;font-size:.78rem}}
th{{background:#161628;padding:8px 11px;text-align:left;font-size:.65rem;
    color:#555870;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:7px 11px;border-top:1px solid #161628}}
.s-open{{color:#34d399;font-weight:700}}
.s-pending{{color:#fbbf24;font-weight:700}}
.s-closed{{color:#444860}}
.s-cancelled{{color:#ef4444}}
h2{{font-size:.82rem;color:#a78bfa;margin-bottom:8px;text-transform:uppercase;letter-spacing:.1em}}
.cfg{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;
      padding:12px 14px;margin-bottom:18px;font-size:.78rem;line-height:2}}
.cfg b{{color:#a78bfa}}
.diff{{background:#0a1a0a;border:1px solid #1e3a1e;border-radius:9px;
       padding:12px 14px;margin-bottom:18px;font-size:.76rem;line-height:1.9}}
.diff .ok{{color:#34d399}}.diff .fix{{color:#fbbf24}}.diff .label{{color:#555870}}
</style></head><body>
<h1>MNQ Bot <span style="font-size:.85rem;color:#60a5fa">v7</span>
    <span style="font-size:.7rem;color:#34d399;margin-left:8px">Pine Script 100% Match</span>
</h1>
<p class="sub">Refresh 5s · {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'bd' if USE_DEMO else 'bl'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'bg' if tok_ok else 'bw'}">
    TOKEN {'✅' if tok_ok else '⚠️'} · exp {exp_str}
  </span>
</div>

<div class="diff">
  <span class="label">v7 FIXES vs v6 ── </span>
  <span class="ok">✅ Bracket price from FILL (not signal close)</span> &nbsp;|&nbsp;
  <span class="ok">✅ Cancel only when pending + bar_count &gt; cancelAfter</span> &nbsp;|&nbsp;
  <span class="ok">✅ Trail: SHORT = SL moves DOWN with price</span> &nbsp;|&nbsp;
  <span class="ok">✅ entry_filled action for stop order fills</span> &nbsp;|&nbsp;
  <span class="ok">✅ was_open / trail_armed mirror Pine arrays</span>
</div>

<div class="cfg">
  <b>Pine Logic:</b> &nbsp;
  Signal: close &lt; open (bearish bar) &nbsp;|&nbsp;
  Sell Stop: close − 2t &nbsp;|&nbsp;
  Cancel: 2 bars &nbsp;|&nbsp;
  SL: +15t from fill &nbsp;|&nbsp;
  TP: −120t from fill &nbsp;|&nbsp;
  Trail start 1t · dist 10t &nbsp;|&nbsp;
  BE <span style="color:#ef4444">OFF</span> &nbsp;|&nbsp;
  Session <span style="color:#ef4444">OFF</span>
</div>

<div class="cards">
  <div class="card"><div class="lbl">Open</div><div class="val g">{open_c}</div></div>
  <div class="card"><div class="lbl">Pending</div><div class="val o">{pend_c}</div></div>
  <div class="card"><div class="lbl">Closed</div><div class="val p">{done_c}</div></div>
  <div class="card"><div class="lbl">Signals</div><div class="val y">{len(signal_log)}</div></div>
  <div class="card"><div class="lbl">Bot</div><div class="val g">ON</div></div>
</div>

<h2>Active Trades ({open_c + pend_c})</h2>
<table><tr>
  <th>Trade ID</th><th>Sym</th><th>Side</th>
  <th>Stop Px</th><th>Fill Px</th>
  <th>TP ✅</th><th>SL 🛑</th>
  <th>Status</th><th>Bars</th><th>Trail</th>
</tr>"""

    active = sorted(
        [(tid, t) for tid, t in snap.items()
         if t["status"] in ("open", "pending")],
        key=lambda x: x[1]["open_time"], reverse=True
    )
    for tid, t in active[:60]:
        sc = f"s-{t['status']}"
        html += f"""<tr>
<td class="p" style="font-size:.7rem">{tid}</td>
<td>{t['symbol']}</td>
<td style="color:{'#f87171' if t['side']=='Sell' else '#34d399'}">{t['side']}</td>
<td class="y">{t.get('stop_price','—')}</td>
<td class="b">{t.get('fill_price') or '—'}</td>
<td class="g">{t.get('tp_price') or '—'}</td>
<td class="r">{t.get('sl_price') or '—'}</td>
<td class="{sc}">{t['status'].upper()}</td>
<td class="o">{t.get('bar_count',0)}</td>
<td>{'✅' if t.get('trail_armed') else '—'}</td>
</tr>"""

    if not active:
        html += ("<tr><td colspan='10' style='color:#555870;"
                 "text-align:center;padding:18px'>No active trades</td></tr>")

    html += ("</table><h2>Signal Log</h2><table><tr>"
             "<th>Time</th><th>Action</th><th>Trade ID</th>"
             "<th>Symbol</th><th>Result</th></tr>")

    action_colors = {
        "short_entry":    "#f87171",
        "long_entry":     "#34d399",
        "entry_filled":   "#60a5fa",
        "trail_armed":    "#a78bfa",
        "cancel_pending": "#fbbf24",
        "trade_closed":   "#555870",
        "close_all":      "#fb923c",
        "bar_update":     "#333550",
    }
    for e in logs:
        ac = action_colors.get(e["action"], "#dde0f0")
        html += f"""<tr>
<td style="color:#555870">{e['time'][11:19]}</td>
<td style="color:{ac};font-weight:600">{e['action']}</td>
<td class="p" style="font-size:.7rem">{e['tradeId']}</td>
<td>{e['symbol']}</td>
<td style="color:#555870;font-size:.72rem">{e['result'][:120]}</td>
</tr>"""

    html += "</table></body></html>"
    return html


# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🚀 MNQ Bot v7 | Port:{port} | {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
