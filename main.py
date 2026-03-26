"""
MNQ ATM Bridge - v12
=====================
KEY CHANGE vs v11:
  ONE webhook only — alert_message from strategy.entry() fill.
  Python receives Buy/Sell → places Stop order WITH ATM attached.
  No pending_long / pending_short split.
  No race condition. No missing bracket.

Flow:
  Pine: stop fills → alert_message → Python
  Python: place Stop order + bracket1:{atmName}
  Tradovate ATM: SL + TP + Trail — done.

Actions:
  Buy    → place Buy  Stop + ATM
  Sell   → place Sell Stop + ATM
  cancel → cancel stored order by orderId
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
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET",
                           "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET",       "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO",              "true").lower() == "true"
DEFAULT_ATM          = os.environ.get("ATM_NAME",              "mnq_Trail_10")
DEFAULT_SYMBOL       = os.environ.get("TRADOVATE_SYMBOL",      "MNQM6")

BASE_URL = ("https://demo.tradovateapi.com/v1" if USE_DEMO
            else "https://live.tradovateapi.com/v1")

# ─── AUTH ────────────────────────────────────────────────────────────────────
_auth_lock    = threading.Lock()
_access_token = os.environ.get("TRADOVATE_TOKEN", "")
_token_expiry = time.time() + 4500 if _access_token else 0
_account_id   = None

# ─── STATE ───────────────────────────────────────────────────────────────────
_state_lock    = threading.Lock()
pending_orders = {}   # orderId → {tradovate_order_id, ...}
signal_log     = []

# ─── HELPERS ─────────────────────────────────────────────────────────────────
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
            "Content-Type": "application/json"}


def _api(method, ep, body=None, retry=True):
    url = f"{BASE_URL}/{ep}"
    try:
        r = (requests.post(url, headers=hdrs(), json=body, timeout=10)
             if method == "POST"
             else requests.get(url, headers=hdrs(), timeout=10))
        if r.status_code == 401 and retry:
            log.warning("401 — refreshing")
            global _token_expiry
            with _auth_lock:
                _token_expiry = 0
            return _api(method, ep, body, retry=False)
        return r.json()
    except Exception:
        log.error(traceback.format_exc())
        return {"error": f"api_error_{ep}"}

def _post(ep, b): return _api("POST", ep, b)
def _get(ep):     return _api("GET",  ep)

threading.Thread(
    target=lambda: [time.sleep(3600) or get_token() for _ in iter(int, 1)],
    daemon=True
).start()

# ═══════════════════════════════════════════════════════════════════
# CORE ORDER FUNCTION
# ONE function — Stop order + ATM in a single API call
# ═══════════════════════════════════════════════════════════════════

def place_stop_with_atm(action, symbol, quantity, stop_price, atm_name):
    """
    Places a Buy Stop or Sell Stop order with ATM attached.
    ATM (mnq_Trail_10) creates SL + TP + Trail automatically on fill.

    action     : "Buy" or "Sell"
    symbol     : "MNQM6"
    quantity   : int
    stop_price : float — the stop trigger price
    atm_name   : "mnq_Trail_10"
    """
    acc = get_account()
    if not acc:
        return {"error": "no_account"}

    sp = round(_f(stop_price), 2)

    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      action,       # "Buy" or "Sell"
        "symbol":      symbol,
        "orderQty":    _i(quantity),
        "orderType":   "Stop",
        "stopPrice":   sp,
        "isAutomated": True,
        "bracket1": {
            "atmName": atm_name      # ATM attached — fires on fill
        }
    }

    log.info(f"STOP+ATM → {action} {quantity}x {symbol} @ {sp} [{atm_name}]")
    res = _post("order/placeorder", body)
    log.info(f"RESULT   → {res}")
    return res


def cancel_order(tradovate_id):
    if not tradovate_id:
        return {"error": "no_id"}
    log.info(f"CANCEL → #{tradovate_id}")
    res = _post("order/cancelorder", {"orderId": _i(tradovate_id)})
    log.info(f"CANCEL RESULT → {res}")
    return res


def _log(action, order_id, symbol, result):
    with _state_lock:
        signal_log.append({
            "time":    datetime.now().isoformat(),
            "action":  _s(action),
            "orderId": _s(order_id),
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

        action     = _s(data.get("action",    "")).strip()
        order_id   = _s(data.get("orderId",   ""))
        symbol     = _s(data.get("symbol",    DEFAULT_SYMBOL))
        quantity   = _i(data.get("quantity",  1))
        stop_price = _f(data.get("stopPrice", 0))
        atm_name   = _s(data.get("atmName",   DEFAULT_ATM))
        result     = {}

        # ── BUY STOP + ATM ────────────────────────────────────────
        # Pine: longCondition fills → alert_message → action="Buy"
        if action == "Buy":
            if stop_price <= 0:
                return jsonify({"error": "stopPrice missing"}), 400

            res = place_stop_with_atm("Buy", symbol, quantity, stop_price, atm_name)

            if "orderId" in res:
                tv_id = res["orderId"]
                with _state_lock:
                    pending_orders[order_id] = {
                        "tradovate_order_id": tv_id,
                        "action":             "Buy",
                        "symbol":             symbol,
                        "stop_price":         stop_price,
                        "atm_name":           atm_name,
                        "placed_at":          time.time(),
                    }
                result = {"orderId": tv_id, "stopPrice": stop_price,
                          "atm": atm_name}
                log.info(f"Buy Stop+ATM [{order_id}] tv={tv_id} @{stop_price}")
            else:
                result = res

        # ── SELL STOP + ATM ───────────────────────────────────────
        # Pine: shortCondition fills → alert_message → action="Sell"
        elif action == "Sell":
            if stop_price <= 0:
                return jsonify({"error": "stopPrice missing"}), 400

            res = place_stop_with_atm("Sell", symbol, quantity, stop_price, atm_name)

            if "orderId" in res:
                tv_id = res["orderId"]
                with _state_lock:
                    pending_orders[order_id] = {
                        "tradovate_order_id": tv_id,
                        "action":             "Sell",
                        "symbol":             symbol,
                        "stop_price":         stop_price,
                        "atm_name":           atm_name,
                        "placed_at":          time.time(),
                    }
                result = {"orderId": tv_id, "stopPrice": stop_price,
                          "atm": atm_name}
                log.info(f"Sell Stop+ATM [{order_id}] tv={tv_id} @{stop_price}")
            else:
                result = res

        # ── CANCEL ────────────────────────────────────────────────
        # Pine: 2-bar expiry OR conflict prevention
        elif action == "cancel":
            with _state_lock:
                order_info = pending_orders.pop(order_id, None)

            if not order_info:
                result = {"info": f"{order_id} not in pending"}
            else:
                tv_id = order_info.get("tradovate_order_id")
                res   = cancel_order(tv_id)
                result = {"cancelled": order_id, "tradovate_id": tv_id}
                log.info(f"Cancelled [{order_id}] tv={tv_id}")

        else:
            result = {"error": f"unknown action: '{action}'"}

        _log(action, order_id, symbol, result)
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
        "atm":     DEFAULT_ATM,
        "symbol":  DEFAULT_SYMBOL,
        "pending": len(pending_orders),
    })


@app.route("/status")
def status():
    with _state_lock:
        pend = len(pending_orders)
    return jsonify({
        "status":  "online",
        "mode":    "DEMO" if USE_DEMO else "LIVE",
        "pending": pend,
        "signals": len(signal_log),
        "time":    datetime.now().isoformat(),
    })


@app.route("/pending")
def get_pending():
    with _state_lock:
        return jsonify(dict(pending_orders))


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
        logs = list(reversed(signal_log[-50:]))
        pend = dict(pending_orders)

    tok_ok  = bool(_access_token and time.time() < _token_expiry - 120)
    exp_str = (datetime.fromtimestamp(_token_expiry).strftime("%H:%M UTC")
               if _token_expiry else "—")

    pend_rows = ""
    for pid, o in pend.items():
        age   = int(time.time() - o.get("placed_at", time.time()))
        c     = "#34d399" if o["action"] == "Buy" else "#f87171"
        pend_rows += (
            f"<tr>"
            f"<td style='color:#a78bfa;font-size:.7rem'>{pid}</td>"
            f"<td style='color:{c};font-weight:700'>{o['action']}</td>"
            f"<td>{o['symbol']}</td>"
            f"<td style='color:#fbbf24'>{o.get('stop_price','—')}</td>"
            f"<td style='color:#60a5fa'>#{o.get('tradovate_order_id','—')}</td>"
            f"<td style='color:#fb923c'>{age}s</td>"
            f"<td style='color:#a78bfa'>{o.get('atm_name','—')}</td>"
            f"</tr>"
        )
    if not pend_rows:
        pend_rows = "<tr><td colspan='7' style='color:#555870;text-align:center;padding:12px'>No pending orders</td></tr>"

    log_rows = ""
    ac = {"Buy":"#34d399","Sell":"#f87171","cancel":"#fbbf24"}
    for e in logs:
        c = ac.get(e["action"], "#dde0f0")
        log_rows += (
            f"<tr>"
            f"<td style='color:#555870'>{e['time'][11:19]}</td>"
            f"<td style='color:{c};font-weight:700'>{e['action']}</td>"
            f"<td style='color:#a78bfa;font-size:.7rem'>{e['orderId']}</td>"
            f"<td>{e['symbol']}</td>"
            f"<td style='color:#555870;font-size:.72rem'>{e['result'][:200]}</td>"
            f"</tr>"
        )
    if not log_rows:
        log_rows = "<tr><td colspan='5' style='color:#555870;text-align:center;padding:12px'>No signals yet</td></tr>"

    return f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ ATM v12</title><meta http-equiv="refresh" content="5">
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
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:16px}}
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
<h1>MNQ ATM Bridge <span style="font-size:.82rem;color:#60a5fa">v12</span></h1>
<p class="sub">Refresh 5s · {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<div class="row">
  <span class="badge {'bd' if USE_DEMO else 'bl'}">{'DEMO' if USE_DEMO else 'LIVE'}</span>
  <span class="badge {'bg' if tok_ok else 'bw'}">TOKEN {'✅' if tok_ok else '⚠️'} · {exp_str}</span>
</div>
<div class="info">
  <b>Flow:</b> Pine signal → Stop order + ATM (1 webhook) → Tradovate fills → ATM activates
  <br>
  <b>ATM:</b> <code style="color:#60a5fa">{DEFAULT_ATM}</code>
  &nbsp;&nbsp;<b>Symbol:</b> <code style="color:#60a5fa">{DEFAULT_SYMBOL}</code>
  &nbsp;&nbsp;<b>Long:</b> <span style="color:#34d399">close &gt; open + 2t</span>
  &nbsp;&nbsp;<b>Short:</b> <span style="color:#f87171">close &lt; open − 2t</span>
  <br>
  <b>Cancel:</b> <span style="color:#fbbf24">2-bar expiry · conflict auto-cancel</span>
  &nbsp;&nbsp;<b>SL/TP/Trail:</b> <span style="color:#34d399">Tradovate ATM only</span>
</div>
<div class="cards">
  <div class="card"><div class="lbl">Pending</div>
    <div class="val" style="color:{'#fb923c' if pend else '#34d399'}">{len(pend)}</div></div>
  <div class="card"><div class="lbl">Signals</div>
    <div class="val" style="color:#fbbf24">{len(signal_log)}</div></div>
  <div class="card"><div class="lbl">ATM</div>
    <div class="val" style="font-size:.85rem;color:#a78bfa;margin-top:6px">{DEFAULT_ATM}</div></div>
  <div class="card"><div class="lbl">Bot</div>
    <div class="val" style="color:#34d399">ON</div></div>
</div>
<h2>Pending Stop Orders ({len(pend)})</h2>
<table>
  <tr><th>Pine ID</th><th>Dir</th><th>Symbol</th><th>Stop Px</th>
      <th>TV Order ID</th><th>Age</th><th>ATM</th></tr>
  {pend_rows}
</table>
<h2>Signal Log</h2>
<table>
  <tr><th>Time</th><th>Action</th><th>Order ID</th><th>Symbol</th><th>Result</th></tr>
  {log_rows}
</table>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"MNQ ATM Bridge v12 | {port} | {'DEMO' if USE_DEMO else 'LIVE'} | ATM={DEFAULT_ATM}")
    app.run(host="0.0.0.0", port=port, debug=False)
