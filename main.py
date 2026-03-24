"""
MNQ Trading Bot - v6 Final
============================
✅ Auto token refresh (no manual copy-paste after Railway variable set)
✅ Pyramiding 100+ trades with trade ID mapping
✅ Sell Stop 2 ticks, Cancel after 2 bars
✅ SL=15 ticks, TP=120 ticks
✅ Trailing: start=1 tick, distance=10 ticks
✅ NO Break Even
✅ NO session filter, NO date filter
✅ 401 auto-retry
✅ Thread-safe state
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re, os, time, threading, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s")
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

# ─── TRADE STATE ─────────────────────────────────────────────────────────────
_state_lock = threading.Lock()
trades      = {}   # { tradeId -> trade_dict }
signal_log  = []   # last 200

# ─── SYMBOL CONVERTER ────────────────────────────────────────────────────────
def to_tv_sym(sym):
    forced = os.environ.get("TRADOVATE_SYMBOL", "")
    if forced:
        return forced
    m = re.match(r"([A-Z]+)(\d{4})$", sym)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return sym

# ═══════════════════════════════════════════════════════════════════════
# AUTH — FULLY AUTOMATED
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
            log.warning("⚠️ CAPTCHA — manual token needed")
        else:
            log.error(f"Login failed: {d}")
    except Exception as e:
        log.error(f"Login error: {e}")
    return None, 0


def get_access_token():
    global _access_token, _token_expiry
    with _auth_lock:
        now = time.time()
        # Still valid
        if _access_token and now < _token_expiry - 120:
            return _access_token
        # Try renew first (no CAPTCHA)
        if _access_token:
            tok, exp = _do_renew(_access_token)
            if tok:
                _access_token = tok
                _token_expiry = now + exp
                return _access_token
        # Fresh login
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
        r = requests.get(f"{BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {tok}"}, timeout=10)
        accs = r.json()
        if isinstance(accs, list) and accs:
            _account_id = accs[0]["id"]
            log.info(f"✅ Account: {_account_id}")
            return _account_id
    except Exception as e:
        log.error(f"Account error: {e}")
    return None


def _hdrs():
    return {"Authorization": f"Bearer {get_access_token()}",
            "Content-Type": "application/json"}


def _refresh_loop():
    while True:
        time.sleep(60 * 60)
        log.info("⏰ Scheduled refresh...")
        get_access_token()

threading.Thread(target=_refresh_loop, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════
# API — auto-retry 401
# ═══════════════════════════════════════════════════════════════════════

def _api(method, endpoint, body=None, retry=True):
    url = f"{BASE_URL}/{endpoint}"
    try:
        r = (requests.post(url, headers=_hdrs(), json=body, timeout=10)
             if method == "POST"
             else requests.get(url, headers=_hdrs(), timeout=10))
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

def _place_order(sym, side, qty, order_type,
                 price=None, stop_price=None):
    acc = get_account_id()
    if not acc:
        return {"error": "no_account"}
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      side,
        "symbol":      sym,
        "orderQty":    qty,
        "orderType":   order_type,
        "isAutomated": True,
    }
    if order_type == "Stop"  and stop_price is not None:
        body["stopPrice"] = round(stop_price, 2)
    if order_type == "Limit" and price is not None:
        body["price"] = round(price, 2)
    log.info(f"→ {order_type} {side} {qty} {sym} sp={stop_price} p={price}")
    return _post("order/placeorder", body)


def _modify_sl(order_id, new_stop):
    log.info(f"→ ModifySL #{order_id} → {new_stop}")
    return _post("order/modifyorder", {
        "orderId":   order_id,
        "orderType": "Stop",
        "stopPrice": round(new_stop, 2),
    })


def _cancel(order_id):
    log.info(f"→ Cancel #{order_id}")
    return _post("order/cancelorder", {"orderId": order_id})


def _market_close(sym, qty, side):
    acc   = get_account_id()
    cside = "Buy" if side == "Sell" else "Sell"
    return _post("order/placeorder", {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      cside,
        "symbol":      sym,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    })


def _bracket_parallel(sym, close_side, qty, tp_price, sl_price):
    acc    = get_account_id()
    tp_res = [None]
    sl_res = [None]

    def do_tp():
        tp_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
            "action": close_side, "symbol": sym, "orderQty": qty,
            "orderType": "Limit", "price": round(tp_price, 2),
            "isAutomated": True,
        })

    def do_sl():
        sl_res[0] = _post("order/placeorder", {
            "accountSpec": TRADOVATE_USERNAME, "accountId": acc,
            "action": close_side, "symbol": sym, "orderQty": qty,
            "orderType": "Stop", "stopPrice": round(sl_price, 2),
            "isAutomated": True,
        })

    t1 = threading.Thread(target=do_tp, daemon=True)
    t2 = threading.Thread(target=do_sl, daemon=True)
    t1.start(); t2.start()
    t1.join(8);  t2.join(8)
    return tp_res[0] or {}, sl_res[0] or {}

# ═══════════════════════════════════════════════════════════════════════
# TRADE STATE
# ═══════════════════════════════════════════════════════════════════════

def _new_trade(trade_id, entry_oid, sym, tv_sym,
               side, qty, entry_price,
               tp_ticks, sl_ticks, tick_size):
    with _state_lock:
        trades[trade_id] = {
            "entryOrderId": entry_oid,
            "tpOrderId":    None,
            "slOrderId":    None,
            "symbol":       sym,
            "tvSymbol":     tv_sym,
            "side":         side,
            "qty":          qty,
            "entryPrice":   entry_price,
            "tpPrice":      None,
            "slPrice":      None,
            "tpTicks":      tp_ticks,
            "slTicks":      sl_ticks,
            "tickSize":     tick_size,
            "status":       "pending",
            "trailArmed":   False,
            "openTime":     datetime.now().isoformat(),
        }


def _attach_bracket(trade_id, sym, side, qty,
                    entry_price, tp_ticks, sl_ticks, tick_size):
    close_side = "Buy"  if side == "Sell" else "Sell"
    mult       = -1     if side == "Sell" else 1
    tp_price   = round(entry_price + mult * tp_ticks  * tick_size, 2)
    sl_price   = round(entry_price - mult * sl_ticks  * tick_size, 2)
    log.info(f"Bracket [{trade_id}] TP@{tp_price} SL@{sl_price}")
    tp_r, sl_r = _bracket_parallel(sym, close_side, qty, tp_price, sl_price)
    tp_id = tp_r.get("orderId")
    sl_id = sl_r.get("orderId")
    log.info(f"Bracket stored [{trade_id}] tp={tp_id} sl={sl_id}")
    with _state_lock:
        if trade_id in trades:
            trades[trade_id].update({
                "tpOrderId": tp_id,
                "slOrderId": sl_id,
                "tpPrice":   tp_price,
                "slPrice":   sl_price,
                "status":    "open" if (tp_id or sl_id) else "pending",
            })


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
        log.info(f"📨 {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        action     = data.get("action", "")
        tv_sym     = data.get("symbol", "MNQM2026")
        sym        = to_tv_sym(tv_sym)
        qty        = int(data.get("qty", 1))
        order_type = data.get("orderType", "Stop")
        tp_ticks   = int(data.get("tpTicks",  120))
        sl_ticks   = int(data.get("slTicks",   15))
        stop_off   = int(data.get("stopOffset", 2))
        trade_id   = data.get("tradeId", f"T_{int(time.time())}")
        tick_size  = float(data.get("tickSize", 0.25))
        price      = float(data.get("price", 0))
        result     = {}

        # ── SHORT ENTRY ─────────────────────────────────────────────────
        if action == "short_entry":
            if order_type == "Market":
                res = _place_order(sym, "Sell", qty, "Market")
                if "orderId" in res:
                    oid = res["orderId"]
                    _new_trade(trade_id, oid, sym, tv_sym,
                               "Sell", qty, price,
                               tp_ticks, sl_ticks, tick_size)
                    threading.Thread(
                        target=_attach_bracket,
                        args=(trade_id, sym, "Sell", qty,
                              price, tp_ticks, sl_ticks, tick_size),
                        daemon=True).start()
                    result = {"orderId": oid, "bracket": "queued"}
                else:
                    result = res

            else:  # Sell Stop
                sp = round(price - stop_off * tick_size, 2)
                res = _place_order(sym, "Sell", qty, "Stop", stop_price=sp)
                if "orderId" in res:
                    oid = res["orderId"]
                    _new_trade(trade_id, oid, sym, tv_sym,
                               "Sell", qty, sp,
                               tp_ticks, sl_ticks, tick_size)
                    threading.Thread(
                        target=_attach_bracket,
                        args=(trade_id, sym, "Sell", qty,
                              sp, tp_ticks, sl_ticks, tick_size),
                        daemon=True).start()
                    result = {"orderId": oid, "stopPrice": sp}
                else:
                    result = res

        # ── LONG ENTRY ──────────────────────────────────────────────────
        elif action == "long_entry":
            res = _place_order(sym, "Buy", qty, "Market")
            if "orderId" in res:
                oid = res["orderId"]
                _new_trade(trade_id, oid, sym, tv_sym,
                           "Buy", qty, price,
                           tp_ticks, sl_ticks, tick_size)
                threading.Thread(
                    target=_attach_bracket,
                    args=(trade_id, sym, "Buy", qty,
                          price, tp_ticks, sl_ticks, tick_size),
                    daemon=True).start()
                result = {"orderId": oid, "bracket": "queued"}
            else:
                result = res

        # ── TRAILING ARMED ──────────────────────────────────────────────
        elif action == "trail_armed":
            trail_dist = int(data.get("trailDist", 10))
            cur_price  = float(data.get("currentPrice", 0))
            with _state_lock:
                trade = trades.get(trade_id)
            if not trade:
                result = {"error": f"{trade_id} not found"}
            elif not trade.get("slOrderId"):
                result = {"error": "no SL order"}
            else:
                ts    = trade["tickSize"]
                side  = trade["side"]
                sl_id = trade["slOrderId"]
                new_sl = round(
                    cur_price + trail_dist * ts if side == "Sell"
                    else cur_price - trail_dist * ts, 2)
                res = _modify_sl(sl_id, new_sl)
                with _state_lock:
                    if trade_id in trades:
                        trades[trade_id]["slPrice"]    = new_sl
                        trades[trade_id]["trailArmed"] = True
                result = {"newSL": new_sl, "res": res}

        # ── CANCEL PENDING ──────────────────────────────────────────────
        elif action == "cancel_pending":
            with _state_lock:
                trade = trades.get(trade_id)
            if trade and trade["status"] == "pending":
                res = _cancel(trade["entryOrderId"])
                with _state_lock:
                    trades[trade_id]["status"] = "cancelled"
                result = {"cancelled": trade_id, "res": res}
            else:
                result = {"info": "not pending or not found"}

        # ── CLOSE ALL ───────────────────────────────────────────────────
        elif action == "close_all":
            closed = []
            with _state_lock:
                open_t = {tid: dict(t) for tid, t in trades.items()
                          if t["status"] == "open"}
            for tid, t in open_t.items():
                if t.get("tpOrderId"): _cancel(t["tpOrderId"])
                if t.get("slOrderId"): _cancel(t["slOrderId"])
                _market_close(t["symbol"], t["qty"], t["side"])
                with _state_lock:
                    if tid in trades:
                        trades[tid]["status"] = "closed"
                closed.append(tid)
            result = {"closed": closed}

        # ── SESSION END — ignored ────────────────────────────────────────
        elif action == "session_end":
            result = {"info": "session filter OFF — ignored"}

        _log_signal(action, trade_id, tv_sym, result)
        log.info(f"✅ {action}[{trade_id}] → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════
# ENDPOINTS
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
                  if t["status"] in ("closed","session_closed","cancelled"))
    tok_ok  = bool(_access_token and time.time() < _token_expiry - 120)
    exp_str = (datetime.fromtimestamp(_token_expiry).strftime('%H:%M UTC')
               if _token_expiry else "—")

    html = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot v6</title>
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
.s-closed,.s-session-closed{{color:#444860}}
.s-cancelled{{color:#ef4444}}
h2{{font-size:.82rem;color:#a78bfa;margin-bottom:8px;
    text-transform:uppercase;letter-spacing:.1em}}
.cfg{{background:#0f0f20;border:1px solid #1e1e35;border-radius:9px;
      padding:12px 14px;margin-bottom:18px;font-size:.78rem;line-height:2}}
.cfg b{{color:#a78bfa}}
</style></head><body>
<h1>MNQ Bot <span style="font-size:.85rem;color:#60a5fa">v6</span></h1>
<p class="sub">Refresh 5s | {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'bd' if USE_DEMO else 'bl'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'bg' if tok_ok else 'bw'}">
    TOKEN {'✅' if tok_ok else '⚠️'} · exp {exp_str}
  </span>
</div>
<div class="cfg">
  <b>Strategy:</b> &nbsp;
  Sell Stop 2t &nbsp;|&nbsp;
  Cancel 2 bars &nbsp;|&nbsp;
  SL 15t &nbsp;|&nbsp;
  TP 120t &nbsp;|&nbsp;
  Trail start 1t · dist 10t &nbsp;|&nbsp;
  BE <span style="color:#ef4444">OFF</span> &nbsp;|&nbsp;
  Session <span style="color:#ef4444">OFF</span> &nbsp;|&nbsp;
  Date <span style="color:#ef4444">OFF</span>
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
  <th>Entry</th><th>TP ✅</th><th>SL 🛑</th>
  <th>Status</th><th>Trail</th>
</tr>"""

    active = sorted(
        [(tid,t) for tid,t in snap.items()
         if t["status"] in ("open","pending")],
        key=lambda x: x[1]["openTime"], reverse=True
    )
    for tid, t in active[:60]:
        sc = f"s-{t['status']}"
        html += f"""<tr>
<td class="p" style="font-size:.72rem">{tid}</td>
<td>{t['symbol']}</td>
<td style="color:{'#f87171' if t['side']=='Sell' else '#34d399'}">{t['side']}</td>
<td>{t.get('entryPrice','—')}</td>
<td class="g">{t.get('tpPrice','—')}</td>
<td class="r">{t.get('slPrice','—')}</td>
<td class="{sc}">{t['status'].upper()}</td>
<td>{'✅' if t.get('trailArmed') else '—'}</td>
</tr>"""

    if not active:
        html += "<tr><td colspan='8' style='color:#555870;text-align:center;padding:18px'>No active trades</td></tr>"

    html += "</table><h2>Signal Log</h2><table><tr><th>Time</th><th>Action</th><th>Trade ID</th><th>Symbol</th><th>Result</th></tr>"
    action_colors = {
        "short_entry":    "#f87171",
        "long_entry":     "#34d399",
        "trail_armed":    "#a78bfa",
        "cancel_pending": "#fbbf24",
        "close_all":      "#fb923c",
    }
    for e in logs:
        ac = action_colors.get(e["action"], "#dde0f0")
        html += f"""<tr>
<td style="color:#555870">{e['time'][11:19]}</td>
<td style="color:{ac};font-weight:600">{e['action']}</td>
<td class="p" style="font-size:.72rem">{e['tradeId']}</td>
<td>{e['symbol']}</td>
<td style="color:#555870;font-size:.72rem">{e['result'][:120]}</td>
</tr>"""

    html += "</table></body></html>"
    return html


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🚀 MNQ Bot v6 | Port:{port} | {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
