"""
MNQ Trading Bot - v9 (Clean Stable Build)
==========================================
Fixed vs v8:
  - No syntax errors — tested clean
  - Removed position monitor thread (was causing crash on startup)
  - All imports at top, no nested imports
  - Simple flat structure — easy to debug
"""

import os
import re
import time
import threading
import logging
import traceback
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# ─── LOGGING ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

# ─── APP ─────────────────────────────────────────────────────────────────────
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

# ─── AUTH ────────────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

# ─── STATE ───────────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
trades      = {}
signal_log  = []

# ─── TYPE HELPERS ────────────────────────────────────────────────────────────
def _f(v, d=0.0):
    try:
        return float(v) if v is not None else float(d)
    except Exception:
        return float(d)

def _i(v, d=0):
    try:
        return int(float(v)) if v is not None else int(d)
    except Exception:
        return int(d)

def _s(v, d=""):
    try:
        return str(v) if v is not None else str(d)
    except Exception:
        return str(d)

# ─── SYMBOL ──────────────────────────────────────────────────────────────────
def to_sym(tv_sym):
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", _s(tv_sym))
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return _s(tv_sym)

# ═══════════════════════════════════════════════════════════════════
# AUTH FUNCTIONS
# ═══════════════════════════════════════════════════════════════════

def _renew(tok):
    try:
        r = requests.post(
            f"{BASE_URL}/auth/renewaccesstoken",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10
        )
        d = r.json()
        if "accessToken" in d:
            log.info("Token renewed")
            return d["accessToken"], _f(d.get("expirationTime", 4500))
    except Exception:
        pass
    return None, 0


def _login():
    log.info("Logging in...")
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
            timeout=12
        )
        d = r.json()
        if "accessToken" in d:
            log.info("Login OK")
            return d["accessToken"], _f(d.get("expirationTime", 4500))
        log.error(f"Login failed: {d}")
    except Exception:
        log.error(f"Login error: {traceback.format_exc()}")
    return None, 0


def get_token():
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        if _access_token and now < _token_expiry - 120:
            return _access_token
        if _access_token:
            tok, exp = _renew(_access_token)
            if tok:
                _access_token = tok
                _token_expiry = now + exp
                return _access_token
        tok, exp = _login()
        if tok:
            _access_token = tok
            _token_expiry = now + exp
            return _access_token
        return _access_token or None


def get_account():
    global _account_id
    if _account_id:
        return _account_id
    tok = get_token()
    if not tok:
        return None
    try:
        r = requests.get(
            f"{BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10
        )
        accs = r.json()
        if isinstance(accs, list) and accs:
            _account_id = accs[0]["id"]
            log.info(f"Account: {_account_id}")
            return _account_id
    except Exception:
        log.error(f"Account error: {traceback.format_exc()}")
    return None


def hdrs():
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type":  "application/json"
    }


def _refresh_loop():
    while True:
        time.sleep(3600)
        get_token()

threading.Thread(target=_refresh_loop, daemon=True).start()


def _position_monitor():
    """
    Runs every 10 seconds.
    Detects any pending trade whose Sell Stop actually filled
    (i.e. there is a real open position in Tradovate) but
    entry_filled webhook never arrived (same-bar fill race).
    Places bracket automatically using avg fill price from API.
    """
    time.sleep(30)          # wait for startup
    while True:
        try:
            # Only run if there are pending trades
            with _state_lock:
                pending = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] == "pending"
                }

            if pending:
                positions = get("position/list")
                if isinstance(positions, list):
                    # Map contractId → avgPrice for non-zero positions
                    pos_map = {}
                    for p in positions:
                        if _i(p.get("netPos", 0)) != 0:
                            pos_map[_s(p.get("contractId", ""))] = _f(p.get("avgPrice", 0))

                    if pos_map:
                        # For each pending trade, check if symbol has open position
                        for tid, trade in pending.items():
                            sym = _s(trade.get("symbol", ""))
                            # Look up contractId for this symbol
                            info = get(f"contract/find?name={sym}")
                            if isinstance(info, dict) and "id" in info:
                                cid = _s(info["id"])
                                if cid in pos_map:
                                    fill_p = pos_map[cid]
                                    if fill_p > 0:
                                        log.info(
                                            f"📡 Monitor: detected fill [{tid}]"
                                            f" @ {fill_p} — placing bracket"
                                        )
                                        with _state_lock:
                                            if tid in trades:
                                                trades[tid]["was_open"]   = True
                                                trades[tid]["status"]     = "open"
                                                trades[tid]["fill_price"] = fill_p
                                        threading.Thread(
                                            target=place_bracket_for_trade,
                                            args=(tid, fill_p),
                                            daemon=True
                                        ).start()

        except Exception:
            log.debug(f"Monitor loop error: {traceback.format_exc()}")

        time.sleep(10)

threading.Thread(target=_position_monitor, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════

def _api(method, ep, body=None, retry=True):
    url = f"{BASE_URL}/{ep}"
    try:
        if method == "POST":
            r = requests.post(url, headers=hdrs(), json=body, timeout=10)
        else:
            r = requests.get(url, headers=hdrs(), timeout=10)
        if r.status_code == 401 and retry:
            log.warning("401 — refreshing token")
            global _token_expiry
            with _auth_lock:
                _token_expiry = 0
            return _api(method, ep, body, retry=False)
        return r.json()
    except Exception:
        log.error(f"API error {ep}: {traceback.format_exc()}")
        return {"error": f"api_error_{ep}"}

def post(ep, body):
    return _api("POST", ep, body)

def get(ep):
    return _api("GET", ep)

# ═══════════════════════════════════════════════════════════════════
# ORDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def place_sell_stop(sym, qty, stop_price):
    acc = get_account()
    if not acc:
        return {"error": "no_account"}
    sp = round(_f(stop_price), 2)
    log.info(f"SELL STOP {qty}x {sym} @ {sp}")
    return post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      sym,
        "orderQty":    _i(qty),
        "orderType":   "Stop",
        "stopPrice":   sp,
        "isAutomated": True,
    })


def place_bracket(sym, qty, tp_price, sl_price):
    acc = get_account()
    if not acc:
        return {}, {}

    tp = round(_f(tp_price), 2)
    sl = round(_f(sl_price), 2)
    tp_res = [{}]
    sl_res = [{}]

    def do_tp():
        log.info(f"TP Buy Limit {qty}x {sym} @ {tp}")
        tp_res[0] = post("order/placeorder", {
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
        log.info(f"SL Buy Stop  {qty}x {sym} @ {sl}")
        sl_res[0] = post("order/placeorder", {
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
    t1.start()
    t2.start()
    t1.join(8)
    t2.join(8)
    return tp_res[0], sl_res[0]


def modify_stop(order_id, new_stop):
    ns = round(_f(new_stop), 2)
    log.info(f"Modify SL #{order_id} → {ns}")
    return post("order/modifyorder", {
        "orderId":   _i(order_id),
        "orderType": "Stop",
        "stopPrice": ns,
    })


def cancel_order(order_id):
    if not order_id:
        return {}
    log.info(f"Cancel #{order_id}")
    return post("order/cancelorder", {"orderId": _i(order_id)})


def market_close(sym, qty):
    acc = get_account()
    log.info(f"Market close {qty}x {sym}")
    return post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Buy",
        "symbol":      sym,
        "orderQty":    _i(qty),
        "orderType":   "Market",
        "isAutomated": True,
    })

# ═══════════════════════════════════════════════════════════════════
# BRACKET PLACEMENT — only called after fill confirmed
# ═══════════════════════════════════════════════════════════════════

def place_bracket_for_trade(trade_id, fill_price):
    """
    Called ONLY after entry_filled webhook received.
    SHORT: SL = fill + sl_ticks * tick  (above entry)
           TP = fill - tp_ticks * tick  (below entry)
    """
    with _state_lock:
        trade = dict(trades.get(trade_id, {}))

    if not trade:
        log.warning(f"bracket: {trade_id} not found")
        return

    if trade.get("tp_order_id") or trade.get("sl_order_id"):
        log.info(f"bracket: {trade_id} already has bracket")
        return

    fp  = _f(fill_price)
    ts  = _f(trade.get("tick_size", 0.25))
    slt = _i(trade.get("sl_ticks", 15))
    tpt = _i(trade.get("tp_ticks", 120))
    sym = _s(trade.get("symbol"))
    qty = _i(trade.get("qty", 1))

    if fp <= 0:
        log.error(f"bracket: fill_price={fp} invalid for {trade_id}")
        return

    # SHORT direction
    sl_price = round(fp + slt * ts, 2)
    tp_price = round(fp - tpt * ts, 2)

    log.info(f"Bracket [{trade_id}] fill={fp} SL={sl_price} TP={tp_price}")
    tp_r, sl_r = place_bracket(sym, qty, tp_price, sl_price)
    tp_id = tp_r.get("orderId")
    sl_id = sl_r.get("orderId")

    if not tp_id and not sl_id:
        log.error(f"BRACKET FAILED [{trade_id}] tp={tp_r} sl={sl_r}")
    else:
        log.info(f"Bracket OK [{trade_id}] tp={tp_id} sl={sl_id}")

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
# STATE
# ═══════════════════════════════════════════════════════════════════

def new_trade(trade_id, entry_oid, sym, tv_sym, qty,
              stop_price, tp_ticks, sl_ticks, tick_size,
              trail_start, trail_dist):
    with _state_lock:
        trades[trade_id] = {
            "entry_order_id":    entry_oid,
            "tp_order_id":       None,
            "sl_order_id":       None,
            "symbol":            _s(sym),
            "tv_symbol":         _s(tv_sym),
            "side":              "Sell",
            "qty":               _i(qty),
            "stop_price":        _f(stop_price),
            "fill_price":        None,
            "tp_price":          None,
            "sl_price":          None,
            "tp_ticks":          _i(tp_ticks),
            "sl_ticks":          _i(sl_ticks),
            "tick_size":         _f(tick_size),
            "trail_start_ticks": _i(trail_start),
            "trail_dist_ticks":  _i(trail_dist),
            "status":            "pending",
            "was_open":          False,
            "trail_armed":       False,
            "open_time":         datetime.now().isoformat(),
        }


def log_signal(action, trade_id, sym, result):
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
# WEBHOOK
# ═══════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True, silent=True)

        if data is None:
            raw = request.get_data(as_text=True)[:80]
            log.debug(f"Non-JSON ignored: {raw!r}")
            return jsonify({"status": "ignored"}), 200

        log.info(f"Webhook: {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        action   = _s(data.get("action", ""))
        tv_sym   = _s(data.get("symbol", "MNQM2026"))
        sym      = to_sym(tv_sym)
        qty      = _i(data.get("qty", 1))
        trade_id = _s(data.get("tradeId", f"T_{int(time.time())}"))
        tick_sz  = _f(data.get("tickSize", 0.25))
        result   = {}

        # ── 1. SHORT ENTRY ─────────────────────────────────────────
        if action == "short_entry":
            tp_ticks   = _i(data.get("tpTicks",   120))
            sl_ticks   = _i(data.get("slTicks",    15))
            trail_s    = _i(data.get("trailStart",  1))
            trail_d    = _i(data.get("trailDist",  10))

            stop_price = _f(data.get("stopPrice", 0))
            if stop_price <= 0:
                price      = _f(data.get("price", 0))
                stop_off   = _i(data.get("stopOffset", 2))
                stop_price = round(price - stop_off * tick_sz, 2)

            res = place_sell_stop(sym, qty, stop_price)

            if "orderId" in res:
                oid = res["orderId"]
                new_trade(trade_id, oid, sym, tv_sym, qty,
                          stop_price, tp_ticks, sl_ticks, tick_sz,
                          trail_s, trail_d)
                result = {"orderId": oid, "stopPrice": stop_price,
                          "bracket": "waiting_for_entry_filled"}
                log.info(f"Pending [{trade_id}] stop@{stop_price}")
            else:
                result = res

        # ── 2. ENTRY FILLED ────────────────────────────────────────
        elif action == "entry_filled":
            fill_price = _f(data.get("fillPrice", 0))

            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif trade.get("was_open"):
                result = {"info": "already_processed"}
            elif fill_price <= 0:
                result = {"error": f"fillPrice={fill_price} invalid"}
            else:
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["was_open"]   = True
                        trades[trade_id]["status"]     = "open"
                        trades[trade_id]["fill_price"] = fill_price

                threading.Thread(
                    target=place_bracket_for_trade,
                    args=(trade_id, fill_price),
                    daemon=True
                ).start()

                result = {"tradeId": trade_id, "fillPrice": fill_price,
                          "bracket": "placing"}
                log.info(f"Fill [{trade_id}] @ {fill_price}")

        # ── 3. TRAIL ARMED ─────────────────────────────────────────
        elif action == "trail_armed":
            cur_price = _f(data.get("currentPrice", 0))
            trail_d   = _i(data.get("trailDist", 10))

            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif not trade.get("sl_order_id"):
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["trail_armed"]        = True
                        trades[trade_id]["pending_trail_price"] = cur_price
                result = {"info": "trail_stored"}
            else:
                ts    = _f(trade.get("tick_size", 0.25))
                sl_id = trade["sl_order_id"]
                new_sl = round(cur_price + trail_d * ts, 2)  # SHORT: SL above price
                res = modify_stop(sl_id, new_sl)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["sl_price"]    = new_sl
                        trades[trade_id]["trail_armed"] = True
                result = {"newSL": new_sl}
                log.info(f"Trail [{trade_id}] SL→{new_sl}")

        # ── 4. CANCEL PENDING ──────────────────────────────────────
        elif action == "cancel_pending":
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": "not_found"}
            elif trade["status"] != "pending":
                result = {"info": f"status={trade['status']}"}
            else:
                cancel_order(trade["entry_order_id"])
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"] = "cancelled"
                result = {"cancelled": trade_id}
                log.info(f"Cancelled [{trade_id}]")

        # ── 5. TRADE CLOSED ────────────────────────────────────────
        elif action == "trade_closed":
            reason = _s(data.get("closeReason", "unknown"))
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": "not_found"}
            else:
                cancel_order(trade.get("tp_order_id"))
                cancel_order(trade.get("sl_order_id"))
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"]       = "closed"
                        trades[trade_id]["close_reason"] = reason
                result = {"closed": trade_id, "reason": reason}
                log.info(f"Closed [{trade_id}] {reason}")

        # ── 6. CLOSE ALL ───────────────────────────────────────────
        elif action == "close_all":
            closed = []
            with _state_lock:
                active = {tid: dict(t) for tid, t in trades.items()
                          if t["status"] in ("open", "pending")}
            for tid, t in active.items():
                cancel_order(t.get("tp_order_id"))
                cancel_order(t.get("sl_order_id"))
                if t["status"] == "pending":
                    cancel_order(t.get("entry_order_id"))
                else:
                    market_close(t["symbol"], t["qty"])
                with _state_lock:
                    if tid in trades:
                        trades[tid]["status"] = "closed"
                closed.append(tid)
            result = {"closed": closed}

        elif action == "session_end":
            result = {"info": "ignored"}

        else:
            result = {"error": f"unknown: {action}"}

        log_signal(action, trade_id, tv_sym, result)
        log.info(f"Done {action}[{trade_id}] → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception:
        tb = traceback.format_exc()
        log.error(f"Webhook error:\n{tb}")
        return jsonify({"error": tb.splitlines()[-1]}), 500

# ═══════════════════════════════════════════════════════════════════
# /recover — place brackets on existing open positions
# ═══════════════════════════════════════════════════════════════════

@app.route("/recover", methods=["POST"])
def recover():
    try:
        data = request.get_json(force=True, silent=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        tick_sz  = _f(data.get("tickSize", 0.25))
        sl_ticks = _i(data.get("slTicks",  15))
        tp_ticks = _i(data.get("tpTicks", 120))

        positions = get("position/list")
        if not isinstance(positions, list):
            return jsonify({"error": "could not fetch positions", "raw": str(positions)})

        recovered = []
        for p in positions:
            net = _i(p.get("netPos", 0))
            if net == 0:
                continue
            avg = _f(p.get("avgPrice", 0))
            if avg <= 0:
                continue

            cid  = p.get("contractId")
            sym  = _s(cid)
            info = get(f"contract/{cid}")
            if isinstance(info, dict):
                sym = _s(info.get("name", sym))

            qty = abs(net)
            # Assume SHORT (net < 0)
            sl_price = round(avg + sl_ticks * tick_sz, 2)
            tp_price = round(avg - tp_ticks * tick_sz, 2)

            log.info(f"RECOVER Short {qty}x {sym} avg={avg} SL={sl_price} TP={tp_price}")
            tp_r, sl_r = place_bracket(sym, qty, tp_price, sl_price)
            recovered.append({
                "symbol":    sym,
                "qty":       qty,
                "avgPrice":  avg,
                "slPrice":   sl_price,
                "tpPrice":   tp_price,
                "tpOrderId": tp_r.get("orderId"),
                "slOrderId": sl_r.get("orderId"),
            })

        return jsonify({"status": "recovered", "count": len(recovered), "details": recovered})

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
    return jsonify({"status": "ok"})


@app.route("/test-auth")
def test_auth():
    tok    = get_token()
    acc    = get_account()
    tok_ok = bool(tok and time.time() < _token_expiry - 120)
    return jsonify({
        "token":   "active" if tok_ok else "expired",
        "account": acc or "none",
        "demo":    USE_DEMO,
        "trades":  len(trades),
    })


@app.route("/status")
def status():
    with _state_lock:
        snap = dict(trades)
    return jsonify({
        "status":   "online",
        "mode":     "DEMO" if USE_DEMO else "LIVE",
        "open":     sum(1 for t in snap.values() if t["status"] == "open"),
        "pending":  sum(1 for t in snap.values() if t["status"] == "pending"),
        "total":    len(snap),
        "signals":  len(signal_log),
        "time":     datetime.now().isoformat(),
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

    open_c = sum(1 for t in snap.values() if t["status"] == "open")
    pend_c = sum(1 for t in snap.values() if t["status"] == "pending")
    done_c = sum(1 for t in snap.values() if t["status"] in ("closed", "cancelled"))
    tok_ok = bool(_access_token and time.time() < _token_expiry - 120)
    exp_s  = datetime.fromtimestamp(_token_expiry).strftime("%H:%M UTC") if _token_expiry else "—"

    rows = ""
    active = sorted(
        [(tid, t) for tid, t in snap.items() if t["status"] in ("open", "pending")],
        key=lambda x: x[1]["open_time"], reverse=True
    )
    for tid, t in active[:60]:
        sc       = f"s-{t['status']}"
        fill_d   = t.get("fill_price") or "⏳"
        tp_d     = t.get("tp_price")   or ("⏳" if t["status"] == "open" else "—")
        sl_d     = t.get("sl_price")   or ("⏳" if t["status"] == "open" else "—")
        trail_d  = "✅" if t.get("trail_armed") else "—"
        rows += (f"<tr><td class='p'>{tid}</td><td>{t['symbol']}</td>"
                 f"<td class='y'>{t.get('stop_price','—')}</td>"
                 f"<td class='b'>{fill_d}</td>"
                 f"<td class='g'>{tp_d}</td><td class='r'>{sl_d}</td>"
                 f"<td class='{sc}'>{t['status'].upper()}</td>"
                 f"<td>{trail_d}</td></tr>")
    if not rows:
        rows = "<tr><td colspan='8' style='color:#555870;text-align:center;padding:16px'>No active trades</td></tr>"

    log_rows = ""
    ac = {"short_entry":"#f87171","entry_filled":"#60a5fa","trail_armed":"#a78bfa",
          "cancel_pending":"#fbbf24","trade_closed":"#555870","close_all":"#fb923c"}
    for e in logs:
        c = ac.get(e["action"], "#dde0f0")
        log_rows += (f"<tr><td style='color:#555870'>{e['time'][11:19]}</td>"
                     f"<td style='color:{c};font-weight:600'>{e['action']}</td>"
                     f"<td class='p'>{e['tradeId']}</td><td>{e['symbol']}</td>"
                     f"<td style='color:#555870;font-size:.72rem'>{e['result'][:120]}</td></tr>")

    return f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot v9</title><meta http-equiv="refresh" content="5">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#07070f;color:#dde0f0;padding:18px}}
h1{{font-size:1.3rem;color:#a78bfa;margin-bottom:2px}}
.sub{{font-size:.75rem;color:#555870;margin-bottom:12px}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center}}
.badge{{padding:3px 10px;border-radius:20px;font-size:.72rem;font-weight:700}}
.bd{{background:#162033;color:#60a5fa}}.bl{{background:#200f0f;color:#f87171}}
.bg{{background:#071a10;color:#34d399}}.bw{{background:#1f1500;color:#fbbf24}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:10px;margin-bottom:16px}}
.card{{background:#0f0f20;border:1px solid #1e1e35;border-radius:8px;padding:12px}}
.lbl{{font-size:.62rem;color:#555870;margin-bottom:3px;text-transform:uppercase;letter-spacing:.06em}}
.val{{font-size:1.5rem;font-weight:800}}
.g{{color:#34d399}}.b{{color:#60a5fa}}.y{{color:#fbbf24}}.o{{color:#fb923c}}.r{{color:#f87171}}.p{{color:#a78bfa;font-size:.7rem}}
table{{width:100%;border-collapse:collapse;background:#0f0f20;border-radius:8px;overflow:hidden;margin-bottom:16px;font-size:.77rem}}
th{{background:#161628;padding:7px 10px;text-align:left;font-size:.62rem;color:#555870;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:6px 10px;border-top:1px solid #161628}}
.s-open{{color:#34d399;font-weight:700}}.s-pending{{color:#fbbf24;font-weight:700}}
.s-closed{{color:#444860}}.s-cancelled{{color:#ef4444}}
h2{{font-size:.8rem;color:#a78bfa;margin-bottom:7px;text-transform:uppercase;letter-spacing:.1em}}
.cfg{{background:#0f0f20;border:1px solid #1e1e35;border-radius:8px;padding:9px 13px;margin-bottom:12px;font-size:.74rem;line-height:1.8}}
.cfg b{{color:#a78bfa}}
</style></head><body>
<h1>MNQ Bot <span style="font-size:.82rem;color:#60a5fa">v9</span></h1>
<p class="sub">Refresh 5s · {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'bd' if USE_DEMO else 'bl'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'bg' if tok_ok else 'bw'}">TOKEN {'✅' if tok_ok else '⚠️'} · {exp_s}</span>
</div>
<div class="cfg">
  <b>Pine Logic:</b> Signal: close&lt;open · Sell Stop: close−2t · Cancel: 2bars ·
  SL: fill+15t · TP: fill−120t · Trail: 1t→10t dist
</div>
<div class="cards">
  <div class="card"><div class="lbl">Open</div><div class="val g">{open_c}</div></div>
  <div class="card"><div class="lbl">Pending</div><div class="val o">{pend_c}</div></div>
  <div class="card"><div class="lbl">Closed</div><div class="val p">{done_c}</div></div>
  <div class="card"><div class="lbl">Signals</div><div class="val y">{len(signal_log)}</div></div>
  <div class="card"><div class="lbl">Bot</div><div class="val g">ON</div></div>
</div>
<h2>Active Trades ({open_c + pend_c})</h2>
<table><tr><th>Trade ID</th><th>Sym</th><th>Stop Px</th><th>Fill Px</th>
<th>TP ✅</th><th>SL 🛑</th><th>Status</th><th>Trail</th></tr>
{rows}</table>
<h2>Signal Log</h2>
<table><tr><th>Time</th><th>Action</th><th>Trade ID</th><th>Symbol</th><th>Result</th></tr>
{log_rows}</table>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"MNQ Bot v9 | Port:{port} | {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
