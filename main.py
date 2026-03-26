"""
MNQ Short Bridge - v14
=======================
Entry  : Sell Stop @ close - 2 ticks
Cancel : 2 bars no fill → Pine cancels + webhook
Fill   : alert_message → Python places SL + TP
Trail  : Python monitor every 2s, arms @ 1t profit, distance 10t
IDs    : Short_1001, Short_1002 ... (unique, independent)

Trade lifecycle:
  "pending"  → Sell Stop placed, waiting fill
  "filling"  → fill detected, bracket being placed
  "open"     → bracket live (SL + TP both active)
  "closed"   → TP or SL hit, other leg cancelled
  "cancelled"→ 2-bar expiry, no fill
  "error"    → bracket placement failed
"""

import os
import time
import threading
import logging
import traceback
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME",   "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD",   "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID",     "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET",
                           "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET",       "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO",              "true").lower() == "true"
DEFAULT_SYMBOL       = os.environ.get("TRADOVATE_SYMBOL",     "MNQM6")

BASE_URL = ("https://demo.tradovateapi.com/v1" if USE_DEMO
            else "https://live.tradovateapi.com/v1")

# ─── AUTH ────────────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

# ─── TRADE STATE ─────────────────────────────────────────────────────────────
# trades[tradeId] = {
#   tradovate_order_id, tp_order_id, sl_order_id
#   symbol, quantity, stop_price, fill_price
#   tp_price, sl_price, sl_ticks, tp_ticks
#   trail_start, trail_dist, tick_size
#   trail_armed, status, placed_at
# }
_state_lock = threading.Lock()
trades      = {}
signal_log  = []

# ─── TYPE HELPERS ─────────────────────────────────────────────────────────────
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

# ═══════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════

def _renew(tok):
    try:
        r = requests.post(f"{BASE_URL}/auth/renewaccesstoken",
                          headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        d = r.json()
        if "accessToken" in d:
            return d["accessToken"], _f(d.get("expirationTime", 4500))
    except Exception:
        pass
    return None, 0


def _login():
    log.info("Logging in...")
    try:
        r = requests.post(f"{BASE_URL}/auth/accesstokenrequest", json={
            "name": TRADOVATE_USERNAME, "password": TRADOVATE_PASSWORD,
            "appId": TRADOVATE_APP_ID,  "appVersion": "1.0",
            "cid":  TRADOVATE_APP_ID,   "sec": TRADOVATE_APP_SECRET,
        }, timeout=12)
        d = r.json()
        if "accessToken" in d:
            log.info("Login OK")
            return d["accessToken"], _f(d.get("expirationTime", 4500))
        log.error(f"Login failed: {d}")
    except Exception:
        log.error(traceback.format_exc())
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
                _access_token, _token_expiry = tok, now + exp
                return _access_token
        tok, exp = _login()
        if tok:
            _access_token, _token_expiry = tok, now + exp
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
        r = requests.get(f"{BASE_URL}/account/list",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        accs = r.json()
        if isinstance(accs, list) and accs:
            _account_id = accs[0]["id"]
            log.info(f"Account: {_account_id}")
            return _account_id
    except Exception:
        log.error(traceback.format_exc())
    return None


def hdrs():
    return {"Authorization": f"Bearer {get_token()}",
            "Content-Type":  "application/json"}


def _api(method, ep, body=None, retry=True):
    url = f"{BASE_URL}/{ep}"
    try:
        r = (requests.post(url, headers=hdrs(), json=body, timeout=10)
             if method == "POST"
             else requests.get(url, headers=hdrs(), timeout=10))
        if r.status_code == 401 and retry:
            global _token_expiry
            with _auth_lock:
                _token_expiry = 0
            return _api(method, ep, body, retry=False)
        return r.json()
    except Exception:
        log.error(traceback.format_exc())
        return {"error": f"api_{ep}"}

def _post(ep, b): return _api("POST", ep, b)
def _get(ep):     return _api("GET",  ep)

threading.Thread(
    target=lambda: [time.sleep(3600) or get_token() for _ in iter(int, 1)],
    daemon=True
).start()

# ═══════════════════════════════════════════════════════════════════
# ORDER HELPERS
# ═══════════════════════════════════════════════════════════════════

def place_sell_stop(symbol, quantity, stop_price):
    """
    Sell Stop order — fills when price drops to stop_price.
    No bracket here — Python attaches SL+TP after fill confirmed.
    """
    acc = get_account()
    if not acc:
        return {"error": "no_account"}
    sp = round(_f(stop_price), 2)
    log.info(f"SELL STOP → {quantity}x {symbol} @ {sp}")
    res = _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      "Sell",
        "symbol":      symbol,
        "orderQty":    _i(quantity),
        "orderType":   "Stop",
        "stopPrice":   sp,
        "isAutomated": True,
    })
    log.info(f"SELL STOP result → {res}")
    return res


def place_bracket(trade_id, fill_price):
    """
    Place independent SL + TP for this specific trade.
    SHORT:
      SL = fill + sl_ticks * tick   (Buy Stop  above — loss cut)
      TP = fill - tp_ticks * tick   (Buy Limit below — profit take)
    """
    with _state_lock:
        trade = dict(trades.get(trade_id, {}))
    if not trade:
        log.warning(f"bracket: {trade_id} not found")
        return

    if trade.get("tp_order_id") or trade.get("sl_order_id"):
        log.info(f"bracket: {trade_id} already has bracket")
        return

    acc      = get_account()
    fp       = _f(fill_price)
    ts       = _f(trade["tick_size"])
    sl_ticks = _i(trade["sl_ticks"])
    tp_ticks = _i(trade["tp_ticks"])
    sym      = _s(trade["symbol"])
    qty      = _i(trade["quantity"])

    sl = round(fp + sl_ticks * ts, 2)
    tp = round(fp - tp_ticks * ts, 2)

    log.info(f"BRACKET [{trade_id}] fill={fp} SL={sl} TP={tp}")

    tp_res = [{}]
    sl_res = [{}]

    def do_tp():
        log.info(f"  TP Buy Limit {qty}x {sym} @ {tp}")
        tp_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
            "action": "Buy", "symbol": sym, "orderQty": qty,
            "orderType": "Limit", "price": tp, "isAutomated": True,
        })
        log.info(f"  TP result → {tp_res[0]}")

    def do_sl():
        log.info(f"  SL Buy Stop  {qty}x {sym} @ {sl}")
        sl_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
            "action": "Buy", "symbol": sym, "orderQty": qty,
            "orderType": "Stop", "stopPrice": sl, "isAutomated": True,
        })
        log.info(f"  SL result → {sl_res[0]}")

    t1 = threading.Thread(target=do_tp, daemon=True)
    t2 = threading.Thread(target=do_sl, daemon=True)
    t1.start(); t2.start()
    t1.join(8);  t2.join(8)

    tp_id = tp_res[0].get("orderId")
    sl_id = sl_res[0].get("orderId")

    with _state_lock:
        if trade_id in trades:
            if tp_id or sl_id:
                trades[trade_id].update({
                    "fill_price":  fp,
                    "tp_order_id": tp_id,
                    "sl_order_id": sl_id,
                    "tp_price":    tp,
                    "sl_price":    sl,
                    "status":      "open",
                })
                log.info(f"BRACKET OK [{trade_id}] tp={tp_id} sl={sl_id}")
            else:
                trades[trade_id]["status"] = "error"
                log.error(f"BRACKET FAILED [{trade_id}]")


def modify_sl(trade_id, new_sl):
    with _state_lock:
        sl_id = trades.get(trade_id, {}).get("sl_order_id")
    if not sl_id:
        return
    ns = round(_f(new_sl), 2)
    log.info(f"TRAIL [{trade_id}] SL → {ns}")
    res = _post("order/modifyorder", {
        "orderId": _i(sl_id), "orderType": "Stop", "stopPrice": ns
    })
    with _state_lock:
        if trade_id in trades:
            trades[trade_id]["sl_price"] = ns
    return res


def cancel_order(order_id):
    if not order_id:
        return {}
    return _post("order/cancelorder", {"orderId": _i(order_id)})

# ═══════════════════════════════════════════════════════════════════
# TRAILING MONITOR — runs every 2 seconds
# Each open trade trailed independently
# SHORT: profit = fill - current, arms when >= trail_start ticks
#        ideal_sl = current + trail_dist * tick
#        only moves DOWN (never raises SL for short)
# ═══════════════════════════════════════════════════════════════════

def _get_price(symbol):
    try:
        res = _get(f"md/getquote?symbol={symbol}")
        if isinstance(res, dict):
            tp = res.get("trade", {})
            if isinstance(tp, dict):
                return _f(tp.get("price", 0))
    except Exception:
        pass
    return 0.0


def _trailing_monitor():
    time.sleep(15)
    while True:
        try:
            with _state_lock:
                open_trades = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] == "open" and t.get("fill_price")
                }

            for tid, t in open_trades.items():
                fp      = _f(t["fill_price"])
                ts      = _f(t["tick_size"])
                t_start = _i(t["trail_start"])
                t_dist  = _i(t["trail_dist"])
                armed   = t.get("trail_armed", False)
                cur_sl  = _f(t.get("sl_price", 0))
                sym     = t["symbol"]

                cur = _get_price(sym)
                if cur <= 0:
                    continue

                # SHORT: profit when price goes down
                profit_t = (fp - cur) / ts

                # Arm when profit reaches trail_start ticks
                if not armed and profit_t >= t_start:
                    with _state_lock:
                        if tid in trades:
                            trades[tid]["trail_armed"] = True
                    armed = True
                    log.info(f"TRAIL ARMED [{tid}] profit={profit_t:.1f}t")

                # Move SL — only downward for SHORT
                if armed:
                    ideal_sl = round(cur + t_dist * ts, 2)
                    if ideal_sl < cur_sl - ts:   # move only if improvement
                        modify_sl(tid, ideal_sl)

        except Exception:
            log.debug(f"Trail error: {traceback.format_exc()}")
        time.sleep(2)

threading.Thread(target=_trailing_monitor, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# POSITION MONITOR — fallback fill detection every 10s
# If alert_message missed, detect fill via position/list
# ═══════════════════════════════════════════════════════════════════

def _position_monitor():
    time.sleep(25)
    while True:
        try:
            with _state_lock:
                pending = {
                    tid: dict(t) for tid, t in trades.items()
                    if t["status"] == "pending"
                }
            if pending:
                positions = _get("position/list")
                if isinstance(positions, list):
                    for p in positions:
                        if _i(p.get("netPos", 0)) >= 0:
                            continue
                        avg = _f(p.get("avgPrice", 0))
                        if avg <= 0:
                            continue
                        cid  = p.get("contractId")
                        info = _get(f"contract/{cid}")
                        sym  = _s(info.get("name","")) if isinstance(info,dict) else ""
                        for tid, t in pending.items():
                            if t["symbol"] == sym and not t.get("fill_price"):
                                log.info(f"Monitor fill [{tid}] @ {avg}")
                                with _state_lock:
                                    if tid in trades:
                                        trades[tid]["status"]     = "filling"
                                        trades[tid]["fill_price"] = avg
                                threading.Thread(
                                    target=place_bracket,
                                    args=(tid, avg),
                                    daemon=True
                                ).start()
        except Exception:
            pass
        time.sleep(10)

threading.Thread(target=_position_monitor, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════

def new_trade(trade_id, tv_order_id, symbol, quantity, stop_price,
              sl_ticks, tp_ticks, trail_start, trail_dist, tick_size):
    with _state_lock:
        trades[trade_id] = {
            "tradovate_order_id": tv_order_id,
            "tp_order_id":        None,
            "sl_order_id":        None,
            "symbol":             _s(symbol),
            "quantity":           _i(quantity),
            "stop_price":         _f(stop_price),
            "fill_price":         None,
            "tp_price":           None,
            "sl_price":           None,
            "sl_ticks":           _i(sl_ticks),
            "tp_ticks":           _i(tp_ticks),
            "trail_start":        _i(trail_start),
            "trail_dist":         _i(trail_dist),
            "tick_size":          _f(tick_size),
            "trail_armed":        False,
            "status":             "pending",
            "placed_at":          time.time(),
        }


def _log(action, trade_id, symbol, result):
    with _state_lock:
        signal_log.append({
            "time":    datetime.now().isoformat(),
            "action":  _s(action),
            "tradeId": _s(trade_id),
            "symbol":  _s(symbol),
            "result":  str(result)[:300],
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
            return jsonify({"status": "ignored"}), 200

        log.info(f"Webhook: {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        action      = _s(data.get("action",     "")).strip()
        trade_id    = _s(data.get("tradeId",    f"T_{int(time.time())}"))
        symbol      = _s(data.get("symbol",     DEFAULT_SYMBOL))
        quantity    = _i(data.get("quantity",   1))
        stop_price  = _f(data.get("stopPrice",  0))
        sl_ticks    = _i(data.get("slTicks",    15))
        tp_ticks    = _i(data.get("tpTicks",    120))
        trail_start = _i(data.get("trailStart", 1))
        trail_dist  = _i(data.get("trailDist",  10))
        tick_size   = _f(data.get("tickSize",   0.25))
        result      = {}

        # ── SELL STOP — Pine alert_message on fill ─────────────────
        # Each tradeId is unique (Short_1001, Short_1002...)
        # Python places Sell Stop, stores trade as "pending"
        # SL + TP placed after fill confirmed
        if action == "Sell":
            if stop_price <= 0:
                return jsonify({"error": "stopPrice required"}), 400

            res = place_sell_stop(symbol, quantity, stop_price)

            if "orderId" in res:
                tv_id = res["orderId"]
                new_trade(trade_id, tv_id, symbol, quantity, stop_price,
                          sl_ticks, tp_ticks, trail_start, trail_dist, tick_size)
                result = {
                    "tradeId":  trade_id,
                    "orderId":  tv_id,
                    "stopPrice": stop_price,
                }
                log.info(f"Sell Stop [{trade_id}] tv={tv_id} @{stop_price}")
            else:
                result = res

        # ── CANCEL — Pine sends after 2 bars no fill ───────────────
        elif action == "cancel":
            with _state_lock:
                trade = trades.get(trade_id)

            if not trade:
                result = {"info": f"{trade_id} not found"}
            elif trade["status"] != "pending":
                result = {"info": f"status={trade['status']} skip cancel"}
            else:
                tv_id = trade.get("tradovate_order_id")
                cancel_order(tv_id)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["status"] = "cancelled"
                result = {"cancelled": trade_id, "tv_id": tv_id}
                log.info(f"Cancelled [{trade_id}]")

        else:
            result = {"error": f"unknown: '{action}'"}

        _log(action, trade_id, symbol, result)
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception:
        tb = traceback.format_exc()
        log.error(f"Webhook error:\n{tb}")
        return jsonify({"error": tb.splitlines()[-1]}), 500

# ═══════════════════════════════════════════════════════════════════
# ADMIN
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
        "mode":    "DEMO" if USE_DEMO else "LIVE",
        "open":    sum(1 for t in trades.values() if t["status"] == "open"),
        "pending": sum(1 for t in trades.values() if t["status"] == "pending"),
        "total":   len(trades),
    })


@app.route("/status")
def status():
    with _state_lock:
        snap = dict(trades)
    return jsonify({
        "status":    "online",
        "mode":      "DEMO" if USE_DEMO else "LIVE",
        "open":      sum(1 for t in snap.values() if t["status"] == "open"),
        "pending":   sum(1 for t in snap.values() if t["status"] == "pending"),
        "cancelled": sum(1 for t in snap.values() if t["status"] == "cancelled"),
        "total":     len(snap),
        "signals":   len(signal_log),
        "time":      datetime.now().isoformat(),
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
        logs = list(reversed(signal_log[-50:]))

    tok_ok  = bool(_access_token and time.time() < _token_expiry - 120)
    exp_str = (datetime.fromtimestamp(_token_expiry).strftime("%H:%M UTC")
               if _token_expiry else "—")

    open_c  = sum(1 for t in snap.values() if t["status"] == "open")
    pend_c  = sum(1 for t in snap.values() if t["status"] == "pending")
    canc_c  = sum(1 for t in snap.values() if t["status"] == "cancelled")
    err_c   = sum(1 for t in snap.values() if t["status"] == "error")

    status_color = {
        "pending":  "#fbbf24",
        "filling":  "#60a5fa",
        "open":     "#34d399",
        "cancelled":"#555870",
        "closed":   "#444860",
        "error":    "#f87171",
    }

    trade_rows = ""
    active = sorted(
        [(tid, t) for tid, t in snap.items()
         if t["status"] in ("pending","filling","open","error")],
        key=lambda x: x[1]["placed_at"], reverse=True
    )
    for tid, t in active[:50]:
        st   = _s(t["status"])
        sc   = status_color.get(st, "#dde0f0")
        fill = t.get("fill_price") or "⏳"
        tp   = t.get("tp_price")   or ("⏳" if st == "open" else "—")
        sl   = t.get("sl_price")   or ("⏳" if st == "open" else "—")
        tr   = "✅" if t.get("trail_armed") else "—"
        age  = int(time.time() - t.get("placed_at", time.time()))
        trade_rows += (
            f"<tr>"
            f"<td style='color:#a78bfa;font-size:.7rem'>{tid}</td>"
            f"<td>{t['symbol']}</td>"
            f"<td style='color:#fbbf24'>{t.get('stop_price','—')}</td>"
            f"<td style='color:#60a5fa'>{fill}</td>"
            f"<td style='color:#34d399'>{tp}</td>"
            f"<td style='color:#f87171'>{sl}</td>"
            f"<td style='color:{sc};font-weight:700'>{st.upper()}</td>"
            f"<td>{tr}</td>"
            f"<td style='color:#555870'>{age}s</td>"
            f"</tr>"
        )
    if not trade_rows:
        trade_rows = "<tr><td colspan='9' style='color:#555870;text-align:center;padding:14px'>No active trades</td></tr>"

    log_rows = ""
    for e in logs:
        c = {"Sell":"#f87171","cancel":"#fbbf24"}.get(e["action"],"#dde0f0")
        log_rows += (
            f"<tr>"
            f"<td style='color:#555870'>{e['time'][11:19]}</td>"
            f"<td style='color:{c};font-weight:700'>{e['action']}</td>"
            f"<td style='color:#a78bfa;font-size:.7rem'>{e['tradeId']}</td>"
            f"<td>{e['symbol']}</td>"
            f"<td style='color:#555870;font-size:.72rem'>{e['result'][:180]}</td>"
            f"</tr>"
        )
    if not log_rows:
        log_rows = "<tr><td colspan='5' style='color:#555870;text-align:center;padding:12px'>No signals</td></tr>"

    return f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Short v14</title><meta http-equiv="refresh" content="3">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#07070f;color:#dde0f0;padding:20px}}
h1{{font-size:1.3rem;color:#a78bfa;margin-bottom:2px}}
.sub{{font-size:.75rem;color:#555870;margin-bottom:14px}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}}
.badge{{padding:3px 11px;border-radius:20px;font-size:.72rem;font-weight:700}}
.bd{{background:#162033;color:#60a5fa}}.bl{{background:#200f0f;color:#f87171}}
.bg{{background:#071a10;color:#34d399}}.bw{{background:#1f1500;color:#fbbf24}}
.info{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;
       padding:12px 16px;margin-bottom:14px;font-size:.79rem;line-height:2.1}}
.info b{{color:#a78bfa}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:10px;margin-bottom:16px}}
.card{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;padding:12px}}
.lbl{{font-size:.62rem;color:#555870;margin-bottom:3px;text-transform:uppercase;letter-spacing:.06em}}
.val{{font-size:1.4rem;font-weight:800}}
h2{{font-size:.8rem;color:#a78bfa;margin-bottom:7px;text-transform:uppercase;letter-spacing:.1em}}
table{{width:100%;border-collapse:collapse;background:#0f0f20;border-radius:9px;
       overflow:hidden;margin-bottom:16px;font-size:.77rem}}
th{{background:#161628;padding:7px 10px;text-align:left;font-size:.62rem;
    color:#555870;text-transform:uppercase;letter-spacing:.06em}}
td{{padding:6px 10px;border-top:1px solid #161628}}
</style></head><body>
<h1>MNQ Short Bridge <span style="font-size:.82rem;color:#60a5fa">v14</span></h1>
<p class="sub">Refresh 3s · {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'bd' if USE_DEMO else 'bl'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'bg' if tok_ok else 'bw'}">TOKEN {'✅' if tok_ok else '⚠️'} · {exp_str}</span>
</div>
<div class="info">
  <b>Signal:</b> close &lt; open
  &nbsp;&nbsp;<b>Entry:</b> Sell Stop @ close − 2t
  &nbsp;&nbsp;<b>Cancel:</b> 2 bars no fill
  &nbsp;&nbsp;<b>SL:</b> fill + 15t
  &nbsp;&nbsp;<b>TP:</b> fill − 120t
  <br>
  <b>Trail:</b> arm @ 1t profit · distance 10t · independent per trade
  &nbsp;&nbsp;<b>IDs:</b> Short_1001, Short_1002 ... (each trade managed separately)
</div>
<div class="cards">
  <div class="card"><div class="lbl">Open</div>
    <div class="val" style="color:#34d399">{open_c}</div></div>
  <div class="card"><div class="lbl">Pending</div>
    <div class="val" style="color:#fbbf24">{pend_c}</div></div>
  <div class="card"><div class="lbl">Cancelled</div>
    <div class="val" style="color:#555870">{canc_c}</div></div>
  <div class="card"><div class="lbl">Error</div>
    <div class="val" style="color:#f87171">{err_c}</div></div>
  <div class="card"><div class="lbl">Signals</div>
    <div class="val" style="color:#60a5fa">{len(signal_log)}</div></div>
  <div class="card"><div class="lbl">Bot</div>
    <div class="val" style="color:#34d399">ON</div></div>
</div>
<h2>Active Trades ({open_c + pend_c})</h2>
<table>
  <tr><th>Trade ID</th><th>Sym</th><th>Stop Px</th><th>Fill Px</th>
      <th>TP</th><th>SL</th><th>Status</th><th>Trail</th><th>Age</th></tr>
  {trade_rows}
</table>
<h2>Signal Log</h2>
<table>
  <tr><th>Time</th><th>Action</th><th>Trade ID</th><th>Symbol</th><th>Result</th></tr>
  {log_rows}
</table>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"MNQ Short Bridge v14 | {port} | {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
