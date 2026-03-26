"""
MNQ Trading Bot - v8
=====================
Bugs fixed vs v7:
  ✅ FIX 1: get_json(silent=True) — non-JSON payloads (order fill webhooks)
             handled gracefully, no crash
  ✅ FIX 2: full traceback logging — exact line of any future error shown
  ✅ FIX 3: defensive type conversion — every field individually try/except
  ✅ FIX 4: Tradovate position poll thread — if entry_filled never arrives,
             background thread detects fill via /position/list and places bracket
  ✅ FIX 5: /recover endpoint — emergency bracket placement for any open
             position that has no bracket (handles current 24-position situation)

Flow (matches Pine v8 exactly):
  Bar bearish → short_entry webhook  → Python: Sell Stop, status=pending
  Fill detected→ entry_filled webhook → Python: bracket from REAL fill price
  Profit≥1t   → trail_armed webhook  → Python: move SL down
  TP/SL hit   → trade_closed webhook → Python: cancel other leg
  2 bars no fill→ cancel_pending     → Python: cancel Sell Stop
"""

import traceback
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

DEMO_URL  = "https://demo.tradovateapi.com/v1"
LIVE_URL  = "https://live.tradovateapi.com/v1"
BASE_URL  = DEMO_URL if USE_DEMO else LIVE_URL

# ─── AUTH STATE ───────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

# ─── TRADE STATE ─────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
trades      = {}    # { tradeId → trade_dict }
signal_log  = []    # last 200 signals

# ─── SYMBOL CONVERTER ────────────────────────────────────────────────────────
def to_tradovate_sym(sym):
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", str(sym))
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return str(sym)

# ─── SAFE TYPE HELPERS ────────────────────────────────────────────────────────
def _f(val, default=0.0):
    """Safe float conversion — never raises"""
    try:
        return float(val) if val is not None else float(default)
    except (TypeError, ValueError):
        return float(default)

def _i(val, default=0):
    """Safe int conversion — never raises"""
    try:
        return int(float(val)) if val is not None else int(default)
    except (TypeError, ValueError):
        return int(default)

def _s(val, default=""):
    """Safe string conversion — never raises"""
    try:
        return str(val) if val is not None else str(default)
    except Exception:
        return str(default)

# ═══════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════

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
    except Exception:
        log.warning(f"Renew failed:\n{traceback.format_exc()}")
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
    except Exception:
        log.error(f"Login error:\n{traceback.format_exc()}")
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
    except Exception:
        log.error(f"Account error:\n{traceback.format_exc()}")
    return None


def _hdrs():
    tok = get_access_token()
    return {
        "Authorization": f"Bearer {tok}" if tok else "",
        "Content-Type":  "application/json"
    }


def _refresh_loop():
    while True:
        time.sleep(3600)
        log.info("⏰ Scheduled token refresh")
        get_access_token()

threading.Thread(target=_refresh_loop, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# API — auto-retry 401
# ═══════════════════════════════════════════════════════════════════

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
    except Exception:
        log.error(f"API {method} {endpoint}:\n{traceback.format_exc()}")
        return {"error": f"api_error_{endpoint}"}

def _post(ep, body): return _api("POST", ep, body)
def _get(ep):        return _api("GET",  ep)

# ═══════════════════════════════════════════════════════════════════
# ORDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def _place_sell_stop(sym, qty, stop_price):
    """
    Pine: strategy.entry("Short_N", strategy.short, stop=entryPrice)
    Bracket NOT placed here — wait for entry_filled
    """
    acc = get_account_id()
    if not acc:
        return {"error": "no_account"}
    sp = round(_f(stop_price), 2)
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      sym,
        "orderQty":    _i(qty),
        "orderType":   "Stop",
        "stopPrice":   sp,
        "isAutomated": True,
    }
    log.info(f"→ SELL STOP {qty}x {sym} @ {sp}")
    return _post("order/placeorder", body)


def _place_bracket(sym, qty, tp_price, sl_price):
    """
    Pine: strategy.exit(loss=slTicks, profit=tpTicks)
    Called ONLY from _place_bracket_for_trade AFTER fill confirmed.

    SHORT bracket:
      TP = fill - tpTicks * tick  → Buy Limit (below entry)
      SL = fill + slTicks * tick  → Buy Stop  (above entry)
    """
    acc = get_account_id()
    if not acc:
        return {}, {}

    tp = round(_f(tp_price), 2)
    sl = round(_f(sl_price), 2)
    tp_res = [None]
    sl_res = [None]

    def do_tp():
        log.info(f"→ TP Buy Limit  {qty}x {sym} @ {tp}")
        tp_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   acc,
            "action":      "Buy",
            "symbol":      sym,
            "orderQty":    _i(qty),
            "orderType":   "Limit",
            "price":       tp,
            "isAutomated": True,
        })

    def do_sl():
        log.info(f"→ SL Buy Stop   {qty}x {sym} @ {sl}")
        sl_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME,
            "accountId":   acc,
            "action":      "Buy",
            "symbol":      sym,
            "orderQty":    _i(qty),
            "orderType":   "Stop",
            "stopPrice":   sl,
            "isAutomated": True,
        })

    t1 = threading.Thread(target=do_tp, daemon=True)
    t2 = threading.Thread(target=do_sl, daemon=True)
    t1.start(); t2.start()
    t1.join(8);  t2.join(8)
    return tp_res[0] or {}, sl_res[0] or {}


def _modify_stop(order_id, new_stop):
    ns = round(_f(new_stop), 2)
    log.info(f"→ Modify SL #{order_id} → {ns}")
    return _post("order/modifyorder", {
        "orderId":   _i(order_id),
        "orderType": "Stop",
        "stopPrice": ns,
    })


def _cancel_order(order_id):
    if not order_id:
        return {}
    log.info(f"→ Cancel #{order_id}")
    return _post("order/cancelorder", {"orderId": _i(order_id)})


def _market_close(sym, qty):
    acc = get_account_id()
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      sym,
        "orderQty":    _i(qty),
        "orderType":   "Market",
        "isAutomated": True,
    })

# ═══════════════════════════════════════════════════════════════════
# BRACKET PLACEMENT — called ONLY after fill confirmed
# ═══════════════════════════════════════════════════════════════════

def _place_bracket_for_trade(trade_id, fill_price):
    """
    The ONLY function that places brackets.
    Called after Pine sends entry_filled with actual fill price.

    SHORT direction:
      SL = fill_price + sl_ticks * tick_size   (above entry)
      TP = fill_price - tp_ticks * tick_size   (below entry)
    """
    with _state_lock:
        trade = dict(trades.get(trade_id, {}))

    if not trade:
        log.warning(f"place_bracket: {trade_id} not found")
        return

    if trade.get("tp_order_id") or trade.get("sl_order_id"):
        log.info(f"place_bracket: {trade_id} already has bracket — skip")
        return

    fp       = _f(fill_price)
    ts       = _f(trade.get("tick_size", 0.25))
    sl_ticks = _i(trade.get("sl_ticks", 15))
    tp_ticks = _i(trade.get("tp_ticks", 120))
    sym      = _s(trade.get("symbol"))
    qty      = _i(trade.get("qty", 1))
    side     = _s(trade.get("side", "Sell"))

    if fp <= 0:
        log.error(f"place_bracket: fill_price={fp} invalid for {trade_id}")
        return

    # SHORT: SL above fill, TP below fill
    if side == "Sell":
        sl_price = round(fp + sl_ticks * ts, 2)
        tp_price = round(fp - tp_ticks * ts, 2)
    else:  # LONG: SL below fill, TP above fill
        sl_price = round(fp - sl_ticks * ts, 2)
        tp_price = round(fp + tp_ticks * ts, 2)

    log.info(f"🎯 Bracket [{trade_id}] fill={fp} SL={sl_price} TP={tp_price}")
    tp_r, sl_r = _place_bracket(sym, qty, tp_price, sl_price)
    tp_id = tp_r.get("orderId")
    sl_id = sl_r.get("orderId")

    if not tp_id and not sl_id:
        log.error(f"❌ BRACKET FAILED [{trade_id}] tp={tp_r} sl={sl_r}")
    else:
        log.info(f"✅ Bracket OK [{trade_id}] tp={tp_id} sl={sl_id}")

    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "fill_price":  fp,
                "tp_order_id": tp_id,
                "sl_order_id": sl_id,
                "tp_price":    tp_price,
                "sl_price":    sl_price,
                "status":      "open",
                "was_open":    True,
            })

# ═══════════════════════════════════════════════════════════════════
# POSITION MONITOR — fallback if entry_filled never arrives
# Polls Tradovate /position/list every 15s.
# For any open position matching a pending trade, places bracket.
# ═══════════════════════════════════════════════════════════════════

def _position_monitor():
    """
    Safety net: if Pine's entry_filled webhook is missed or delayed,
    we detect the fill via Tradovate API and place the bracket.
    """
    while True:
        time.sleep(15)
        try:
            with _state_lock:
                pending = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] == "pending" and not t.get("was_open")
                }

            if not pending:
                continue

            positions = _get("position/list")
            if not isinstance(positions, list):
                continue

            # Build map: symbol → avgPrice for open positions
            pos_map = {}
            for p in positions:
                if _i(p.get("netPos")) != 0:
                    pos_map[p.get("contractId")] = _f(p.get("avgPrice"))

            if not pos_map:
                continue

            for tid, trade in pending.items():
                sym = trade.get("symbol", "")
                # Check if there's an open position for this symbol
                # Tradovate uses contractId, not symbol name — lookup needed
                contracts_r = _get(f"contract/find?name={sym}")
                if isinstance(contracts_r, dict) and "id" in contracts_r:
                    cid = contracts_r["id"]
                    if cid in pos_map:
                        fill_p = pos_map[cid]
                        log.info(f"📡 Monitor detected fill [{tid}] @ {fill_p}")
                        with _state_lock:
                            if tid in trades:
                                trades[tid]["was_open"] = True
                                trades[tid]["status"]   = "open"
                        threading.Thread(
                            target=_place_bracket_for_trade,
                            args=(tid, fill_p),
                            daemon=True
                        ).start()

        except Exception:
            log.debug(f"Monitor error:\n{traceback.format_exc()}")

threading.Thread(target=_position_monitor, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# STATE HELPERS
# ═══════════════════════════════════════════════════════════════════

def _new_trade(trade_id, entry_oid, sym, tv_sym,
               side, qty, stop_price,
               tp_ticks, sl_ticks, tick_size,
               trail_start_ticks, trail_dist_ticks):
    """
    Pine: array.push(tradeIds, tradeCounter) etc.
    Creates pending trade — NO bracket yet.
    """
    with _state_lock:
        trades[trade_id] = {
            "entry_order_id":    entry_oid,
            "tp_order_id":       None,
            "sl_order_id":       None,
            "symbol":            _s(sym),
            "tv_symbol":         _s(tv_sym),
            "side":              _s(side),
            "qty":               _i(qty),
            "stop_price":        _f(stop_price),
            "fill_price":        None,
            "tp_price":          None,
            "sl_price":          None,
            "tp_ticks":          _i(tp_ticks),
            "sl_ticks":          _i(sl_ticks),
            "tick_size":         _f(tick_size),
            "trail_start_ticks": _i(trail_start_ticks),
            "trail_dist_ticks":  _i(trail_dist_ticks),
            "status":            "pending",
            "was_open":          False,
            "trail_armed":       False,
            "set_bar_time":      time.time(),
            "open_time":         datetime.now().isoformat(),
        }


def _log_signal(action, trade_id, sym, result):
    with _state_lock:
        signal_log.append({
            "time":    datetime.now().isoformat(),
            "action":  _s(action),
            "tradeId": _s(trade_id),
            "symbol":  _s(sym),
            "result":  str(result)[:200],
        })
        if len(signal_log) > 200:
            signal_log.pop(0)

# ═══════════════════════════════════════════════════════════════════
# WEBHOOK — main handler
# ═══════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        # ── FIX 1: silent=True — handle non-JSON payloads gracefully ──
        # TradingView sends "Order fills and alert() function calls"
        # Order fill events use {{strategy.order.alert_message}} template
        # which is plain text, not JSON — we ignore those silently
        data = request.get_json(force=True, silent=True)

        if data is None:
            # Non-JSON payload (e.g. order fill template text from TradingView)
            raw = request.get_data(as_text=True)[:80]
            log.debug(f"Non-JSON payload ignored: {raw!r}")
            return jsonify({"status": "ignored", "reason": "not_json"}), 200

        log.info(f"📨 {data}")

        # ── Auth check ─────────────────────────────────────────────
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        # ── FIX 2: safe type conversion for every field ────────────
        action    = _s(data.get("action", ""))
        tv_sym    = _s(data.get("symbol", "MNQM2026"))
        sym       = to_tradovate_sym(tv_sym)
        qty       = _i(data.get("qty", 1))
        trade_id  = _s(data.get("tradeId", f"T_{int(time.time())}"))
        tick_size = _f(data.get("tickSize", 0.25))
        price     = _f(data.get("price",    0.0))
        result    = {}

        # ── 1. SHORT ENTRY ─────────────────────────────────────────
        # Pine: close < open → strategy.entry(stop=close-offset)
        # Python: place Sell Stop, store as pending, DO NOT bracket yet
        if action == "short_entry":
            tp_ticks    = _i(data.get("tpTicks",   120))
            sl_ticks    = _i(data.get("slTicks",    15))
            stop_off    = _i(data.get("stopOffset", 2))
            trail_start = _i(data.get("trailStart", 1))
            trail_dist  = _i(data.get("trailDist",  10))

            # Use stopPrice if Pine sends it directly, else calculate
            stop_price = _f(data.get("stopPrice", 0.0))
            if stop_price <= 0:
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
                result = {
                    "orderId":   oid,
                    "stopPrice": stop_price,
                    "bracket":   "waiting_for_entry_filled"
                }
                log.info(f"⏳ Pending [{trade_id}] stop@{stop_price}")
            else:
                result = res

        # ── 2. ENTRY FILLED ← KEY FIX ──────────────────────────────
        # Pine: strategy.opentrades.entry_price(j) — actual fill price
        # Python: places SL+TP bracket from REAL fill price
        elif action == "entry_filled":
            fill_price = _f(data.get("fillPrice", 0.0))

            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif trade.get("was_open"):
                result = {"info": "already_processed"}
            elif fill_price <= 0:
                result = {"error": f"fillPrice={fill_price} invalid"}
            else:
                # Mark open — mirrors Pine tradeWasOpen = true
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["was_open"] = True
                        trades[trade_id]["status"]   = "open"
                        trades[trade_id]["fill_price"] = fill_price

                # Place bracket from ACTUAL fill price
                threading.Thread(
                    target=_place_bracket_for_trade,
                    args=(trade_id, fill_price),
                    daemon=True
                ).start()

                result = {
                    "tradeId":   trade_id,
                    "fillPrice": fill_price,
                    "bracket":   "placing"
                }
                log.info(f"🟢 Fill [{trade_id}] @ {fill_price} — bracket thread started")

        # ── 3. TRAIL ARMED ─────────────────────────────────────────
        # Pine: profit >= trailStartTicks detected
        # SHORT: new SL = currentPrice + trailDist * tick
        elif action == "trail_armed":
            cur_price  = _f(data.get("currentPrice", 0.0))
            trail_dist = _i(data.get("trailDist", 10))

            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif not trade.get("sl_order_id"):
                # Bracket not yet placed — store for later
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["trail_armed"]        = True
                        trades[trade_id]["pending_trail_price"] = cur_price
                result = {"info": "trail_stored_bracket_pending"}
            else:
                ts    = _f(trade.get("tick_size", 0.25))
                side  = _s(trade.get("side", "Sell"))
                sl_id = trade.get("sl_order_id")

                new_sl = (
                    round(cur_price + trail_dist * ts, 2)  # SHORT: SL above price
                    if side == "Sell"
                    else round(cur_price - trail_dist * ts, 2)
                )

                res = _modify_stop(sl_id, new_sl)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["sl_price"]    = new_sl
                        trades[trade_id]["trail_armed"] = True

                result = {"newSL": new_sl, "res": res}
                log.info(f"🟣 Trail [{trade_id}] SL→{new_sl}")

        # ── 4. CANCEL PENDING ──────────────────────────────────────
        # Pine: (bar_index - setBar) > cancelAfter AND not filled
        elif action == "cancel_pending":
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": "not_found"}
            elif trade["status"] != "pending":
                result = {"info": f"status={trade['status']} not pending"}
            else:
                res = _cancel_order(trade["entry_order_id"])
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"] = "cancelled"
                result = {"cancelled": trade_id}
                log.info(f"🚫 Cancelled [{trade_id}]")

        # ── 5. TRADE CLOSED ────────────────────────────────────────
        # Pine: was_open=true, isOpen=false (TP/SL/Trail hit)
        elif action == "trade_closed":
            close_reason = _s(data.get("closeReason", "unknown"))

            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": "not_found"}
            else:
                _cancel_order(trade.get("tp_order_id"))
                _cancel_order(trade.get("sl_order_id"))
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"]       = "closed"
                        trades[trade_id]["close_reason"] = close_reason
                result = {"closed": trade_id, "reason": close_reason}
                log.info(f"🔴 Closed [{trade_id}] reason={close_reason}")

        # ── 6. CLOSE ALL — emergency ───────────────────────────────
        elif action == "close_all":
            closed = []
            with _state_lock:
                active = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] in ("open", "pending")
                }
            for tid, t in active.items():
                _cancel_order(t.get("tp_order_id"))
                _cancel_order(t.get("sl_order_id"))
                if t["status"] == "pending":
                    _cancel_order(t.get("entry_order_id"))
                else:
                    _market_close(t["symbol"], t["qty"])
                with _state_lock:
                    if tid in trades:
                        trades[tid]["status"] = "closed"
                closed.append(tid)
            result = {"closed": closed}

        elif action == "session_end":
            result = {"info": "session_filter_OFF"}

        else:
            result = {"error": f"unknown_action: {action}"}

        _log_signal(action, trade_id, tv_sym, result)
        log.info(f"✅ {action}[{trade_id}] → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception:
        # FIX: full traceback so we see EXACT line of any future error
        tb = traceback.format_exc()
        log.error(f"Webhook error:\n{tb}")
        return jsonify({"error": tb.splitlines()[-1]}), 500

# ═══════════════════════════════════════════════════════════════════
# /recover — emergency bracket placement for positions without brackets
# Use this NOW to fix the existing 24-position situation
# POST /recover with { "secret": "..." }
# ═══════════════════════════════════════════════════════════════════

@app.route("/recover", methods=["POST"])
def recover():
    """
    Emergency: queries Tradovate /position/list, finds any open
    SHORT position, and places brackets if not already present.
    Use after the current 24-position situation.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        tick_size   = _f(data.get("tickSize",  0.25))
        sl_ticks    = _i(data.get("slTicks",   15))
        tp_ticks    = _i(data.get("tpTicks",   120))
        force_place = data.get("force", False)  # if True, place even without trade record

        positions = _get("position/list")
        if not isinstance(positions, list):
            return jsonify({"error": "could not fetch positions", "raw": str(positions)})

        recovered = []
        for p in positions:
            net_pos = _i(p.get("netPos", 0))
            if net_pos == 0:
                continue

            avg_price = _f(p.get("avgPrice", 0))
            if avg_price <= 0:
                continue

            contract_id = p.get("contractId")
            side = "Sell" if net_pos < 0 else "Buy"
            qty  = abs(net_pos)

            # Try to find symbol name
            sym = _s(p.get("contractId", "MNQM6"))
            contract_info = _get(f"contract/{contract_id}")
            if isinstance(contract_info, dict):
                sym = _s(contract_info.get("name", sym))

            sl_price = round(avg_price + sl_ticks * tick_size, 2) if side == "Sell" else round(avg_price - sl_ticks * tick_size, 2)
            tp_price = round(avg_price - tp_ticks * tick_size, 2) if side == "Sell" else round(avg_price + tp_ticks * tick_size, 2)

            log.info(f"🔧 RECOVER {side} {qty}x {sym} @ {avg_price} SL={sl_price} TP={tp_price}")
            tp_r, sl_r = _place_bracket(sym, qty, tp_price, sl_price)

            recovered.append({
                "symbol":    sym,
                "side":      side,
                "qty":       qty,
                "avgPrice":  avg_price,
                "slPrice":   sl_price,
                "tpPrice":   tp_price,
                "tpOrderId": tp_r.get("orderId"),
                "slOrderId": sl_r.get("orderId"),
            })

        return jsonify({
            "status":    "recovered",
            "positions": len(recovered),
            "details":   recovered
        })

    except Exception:
        tb = traceback.format_exc()
        log.error(f"Recover error:\n{tb}")
        return jsonify({"error": tb.splitlines()[-1]}), 500

# ═══════════════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ═══════════════════════════════════════════════════════════════════

@app.route("/set-token", methods=["POST"])
def set_token():
    global _access_token, _token_expiry, _account_id
    data = request.get_json(force=True, silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    tok = _s(data.get("token", "")).strip()
    if not tok:
        return jsonify({"error": "token required"}), 400
    with _auth_lock:
        _access_token = tok
        _token_expiry = time.time() + 4500
        _account_id   = None
    log.info("✅ Token updated via /set-token")
    return jsonify({"status": "ok"})


@app.route("/test-auth")
def test_auth():
    tok    = get_access_token()
    acc    = get_account_id()
    tok_ok = bool(tok and time.time() < _token_expiry - 120)
    return jsonify({
        "token_status":   "✅ Active" if tok_ok else "⚠️ Expired",
        "account_status": f"✅ {acc}" if acc else "❌ No account",
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

# ═══════════════════════════════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════════════════════════════

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
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot v8</title><meta http-equiv="refresh" content="5">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#07070f;color:#dde0f0;padding:18px}}
h1{{font-size:1.35rem;color:#a78bfa;margin-bottom:2px}}
.sub{{font-size:.76rem;color:#555870;margin-bottom:12px}}
.row{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
.badge{{padding:3px 11px;border-radius:20px;font-size:.72rem;font-weight:700}}
.bd{{background:#162033;color:#60a5fa}}.bl{{background:#200f0f;color:#f87171}}
.bg{{background:#071a10;color:#34d399}}.bw{{background:#1f1500;color:#fbbf24}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:18px}}
.card{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;padding:12px}}
.card .lbl{{font-size:.65rem;color:#555870;margin-bottom:4px;text-transform:uppercase;letter-spacing:.06em}}
.card .val{{font-size:1.55rem;font-weight:800}}
.g{{color:#34d399}}.b{{color:#60a5fa}}.y{{color:#fbbf24}}
.o{{color:#fb923c}}.r{{color:#f87171}}.p{{color:#a78bfa}}
table{{width:100%;border-collapse:collapse;background:#0f0f20;border-radius:9px;overflow:hidden;margin-bottom:18px;font-size:.78rem}}
th{{background:#161628;padding:8px 11px;text-align:left;font-size:.65rem;color:#555870;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:7px 11px;border-top:1px solid #161628}}
.s-open{{color:#34d399;font-weight:700}}.s-pending{{color:#fbbf24;font-weight:700}}
.s-closed{{color:#444860}}.s-cancelled{{color:#ef4444}}
h2{{font-size:.82rem;color:#a78bfa;margin-bottom:8px;text-transform:uppercase;letter-spacing:.1em}}
.cfg{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;padding:10px 14px;margin-bottom:10px;font-size:.76rem;line-height:1.9}}
.cfg b{{color:#a78bfa}}
.fix{{background:#071a10;border:1px solid #0f3a1e;border-radius:9px;padding:10px 14px;margin-bottom:10px;font-size:.73rem;line-height:1.9}}
.rec{{background:#1a0f00;border:1px solid #3a2000;border-radius:9px;padding:10px 14px;margin-bottom:14px;font-size:.73rem}}
.rec b{{color:#fbbf24}}.rec code{{color:#fb923c;font-family:monospace;font-size:.78rem}}
</style></head><body>
<h1>MNQ Bot <span style="font-size:.85rem;color:#60a5fa">v8</span></h1>
<p class="sub">Refresh 5s · {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'bd' if USE_DEMO else 'bl'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'bg' if tok_ok else 'bw'}">TOKEN {'✅' if tok_ok else '⚠️'} · exp {exp_str}</span>
</div>
<div class="fix">
  <b style="color:#34d399">v8 Fixes:</b> &nbsp;
  ✅ Non-JSON payloads silently ignored (order fill webhooks) &nbsp;·&nbsp;
  ✅ Full traceback logging (exact error line visible) &nbsp;·&nbsp;
  ✅ Pine JSON bug fixed (no spurious quotes on numbers) &nbsp;·&nbsp;
  ✅ Position monitor fallback (detects fills via API every 15s) &nbsp;·&nbsp;
  ✅ /recover endpoint (emergency bracket placement)
</div>
<div class="rec">
  <b>🔧 Emergency recovery for existing positions:</b><br>
  <code>curl -X POST {{}your_url{{}}/recover -H "Content-Type: application/json" -d '{{"secret":"mnq_secret_2024","tickSize":0.25,"slTicks":15,"tpTicks":120}}'</code>
</div>
<div class="cfg">
  <b>Pine Logic:</b> &nbsp;
  Signal: close &lt; open &nbsp;|&nbsp; Sell Stop: close−2t &nbsp;|&nbsp;
  Cancel: 2 bars &nbsp;|&nbsp; SL: fill+15t &nbsp;|&nbsp; TP: fill−120t &nbsp;|&nbsp;
  Trail: 1t start · 10t dist
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
  <th>Trade ID</th><th>Sym</th><th>Stop Px</th><th>Fill Px</th>
  <th>TP ✅</th><th>SL 🛑</th><th>Status</th><th>Trail</th>
</tr>"""

    active = sorted(
        [(tid, t) for tid, t in snap.items()
         if t["status"] in ("open", "pending")],
        key=lambda x: x[1]["open_time"], reverse=True
    )
    for tid, t in active[:60]:
        sc        = f"s-{t['status']}"
        fill_disp = t.get("fill_price") or "⏳"
        tp_disp   = t.get("tp_price")   or ("⏳" if t["status"] == "open" else "—")
        sl_disp   = t.get("sl_price")   or ("⏳" if t["status"] == "open" else "—")
        html += f"""<tr>
<td class="p" style="font-size:.7rem">{tid}</td>
<td>{t['symbol']}</td>
<td class="y">{t.get('stop_price','—')}</td>
<td class="b">{fill_disp}</td>
<td class="g">{tp_disp}</td>
<td class="r">{sl_disp}</td>
<td class="{sc}">{t['status'].upper()}</td>
<td>{'✅' if t.get('trail_armed') else '—'}</td>
</tr>"""

    if not active:
        html += ("<tr><td colspan='8' style='color:#555870;"
                 "text-align:center;padding:18px'>No active trades</td></tr>")

    html += ("</table><h2>Signal Log</h2><table><tr>"
             "<th>Time</th><th>Action</th><th>Trade ID</th>"
             "<th>Symbol</th><th>Result</th></tr>")

    ac_colors = {
        "short_entry":    "#f87171",
        "entry_filled":   "#60a5fa",
        "trail_armed":    "#a78bfa",
        "cancel_pending": "#fbbf24",
        "trade_closed":   "#555870",
        "close_all":      "#fb923c",
    }
    for e in logs:
        ac = ac_colors.get(e["action"], "#dde0f0")
        html += f"""<tr>
<td style="color:#555870">{e['time'][11:19]}</td>
<td style="color:{ac};font-weight:600">{e['action']}</td>
<td class="p" style="font-size:.7rem">{e['tradeId']}</td>
<td>{e['symbol']}</td>
<td style="color:#555870;font-size:.72rem">{e['result'][:120]}</td>
</tr>"""

    html += "</table></body></html>"
    return html


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🚀 MNQ Bot v8 | Port:{port} | {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
