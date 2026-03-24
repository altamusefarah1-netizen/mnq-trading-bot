"""
MNQ Trading Bot - Webhook Server v3
=====================================
TradingView → Webhook → Tradovate
Uses manual token + auto-refresh via p-ticket flow
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
import os
import time
import threading
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

app  = Flask(__name__)
CORS(app)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "4")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET", "2a6d2b20-2e52-4cf5-b722-b1d6b5d3cd69")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"
# Manual token — set this in Railway Variables after browser login
MANUAL_TOKEN         = os.environ.get("TRADOVATE_TOKEN", "")

DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
BASE_URL  = DEMO_URL if USE_DEMO else LIVE_URL

# ─── SYMBOL CONVERTER ────────────────────────────────────────────────────────
def get_tradovate_symbol(tv_symbol):
    env_sym = os.environ.get("TRADOVATE_SYMBOL", "")
    if env_sym:
        return env_sym
    m = re.match(r"([A-Z]+)(\d{4})$", tv_symbol)
    if m:
        return f"{m.group(1)}{m.group(2)[-1]}"
    return tv_symbol

# ─── GLOBALS ─────────────────────────────────────────────────────────────────
access_token  = MANUAL_TOKEN  # start with manual token if provided
token_expiry  = time.time() + 4800 if MANUAL_TOKEN else 0
open_trades   = {}
trade_log     = []
account_id    = None

# ─── AUTH ────────────────────────────────────────────────────────────────────
def get_token():
    global access_token, token_expiry

    # 1. Use manual token if still valid
    if access_token and time.time() < token_expiry - 60:
        return access_token

    # 2. Try p-ticket renewal (if we have an existing token)
    if access_token:
        log.info("Trying token renewal via renew endpoint...")
        try:
            r = requests.post(
                f"{BASE_URL}/auth/renewaccesstoken",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15
            )
            d = r.json()
            log.info(f"Renew response: {d}")
            if "accessToken" in d:
                access_token = d["accessToken"]
                token_expiry = time.time() + d.get("expirationTime", 4800)
                log.info("✅ Token renewed")
                return access_token
        except Exception as e:
            log.error(f"Renew error: {e}")

    # 3. Try direct login (works if no CAPTCHA triggered)
    log.info("Trying direct login...")
    try:
        r = requests.post(f"{BASE_URL}/auth/accesstokenrequest", json={
            "name":       TRADOVATE_USERNAME,
            "password":   TRADOVATE_PASSWORD,
            "appId":      TRADOVATE_APP_ID,
            "appVersion": "1.0",
            "cid":        TRADOVATE_APP_ID,
            "sec":        TRADOVATE_APP_SECRET,
        }, timeout=15)
        d = r.json()
        log.info(f"Direct login response: {d}")
        if "accessToken" in d:
            access_token = d["accessToken"]
            token_expiry = time.time() + d.get("expirationTime", 4800)
            log.info("✅ Direct login OK")
            return access_token
        if "p-captcha" in d:
            log.warning("⚠️ CAPTCHA required — use manual token")
            # Return existing token even if expired, better than nothing
            return access_token if access_token else None
    except Exception as e:
        log.error(f"Login error: {e}")

    return access_token if access_token else None


def get_account_id():
    global account_id
    if account_id:
        return account_id
    tok = get_token()
    if not tok:
        return None
    try:
        r = requests.get(f"{BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {tok}"}, timeout=15)
        accounts = r.json()
        log.info(f"Accounts: {accounts}")
        if isinstance(accounts, list) and accounts:
            account_id = accounts[0]["id"]
            log.info(f"✅ Account ID: {account_id}")
            return account_id
    except Exception as e:
        log.error(f"Account error: {e}")
    return None


def hdrs():
    tok = get_token()
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

# ─── ORDERS ──────────────────────────────────────────────────────────────────
def place_order(symbol, side, qty, order_type, price=None,
                stop_price=None, tp_ticks=None, sl_ticks=None,
                tick_size=0.25, trade_id=None):
    acc = get_account_id()
    if not acc:
        return {"error": "No account — set TRADOVATE_TOKEN in Railway Variables"}

    sym = get_tradovate_symbol(symbol)
    log.info(f"Symbol: {symbol} → {sym}")

    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc,
        "action":      side,
        "symbol":      sym,
        "orderQty":    qty,
        "orderType":   order_type,
        "isAutomated": True,
    }
    if order_type == "Stop"  and stop_price: body["stopPrice"] = round(stop_price, 2)
    if order_type == "Limit" and price:      body["price"]     = round(price, 2)

    log.info(f"Placing: {body}")
    try:
        r   = requests.post(f"{BASE_URL}/order/placeorder",
                headers=hdrs(), json=body, timeout=15)
        res = r.json()
        log.info(f"Order result: {res}")

        if "orderId" in res and trade_id:
            open_trades[trade_id] = {
                "orderId":    res["orderId"],
                "symbol":     sym,
                "tvSymbol":   symbol,
                "side":       side,
                "qty":        qty,
                "entryPrice": stop_price or price or 0,
                "tpTicks":    tp_ticks,
                "slTicks":    sl_ticks,
                "tickSize":   tick_size,
                "status":     "pending",
                "openTime":   datetime.now().isoformat(),
                "trailArmed": False,
            }
        return res
    except Exception as e:
        log.error(f"Order error: {e}")
        return {"error": str(e)}


def place_bracket(trade_id, symbol, side, qty, entry_price, tp_ticks, sl_ticks, tick_size=0.25):
    acc = get_account_id()
    if not acc or not entry_price:
        return
    sym        = get_tradovate_symbol(symbol)
    close_side = "Buy" if side == "Sell" else "Sell"
    mult       = -1 if side == "Sell" else 1
    tp_price   = round(entry_price + mult * tp_ticks * tick_size, 2)
    sl_price   = round(entry_price - mult * sl_ticks * tick_size, 2)

    for otype, pkey, pval in [("Limit","price",tp_price), ("Stop","stopPrice",sl_price)]:
        b = {"accountSpec": TRADOVATE_USERNAME, "accountId": acc,
             "action": close_side, "symbol": sym, "orderQty": qty,
             "orderType": otype, pkey: pval, "isAutomated": True}
        try:
            r = requests.post(f"{BASE_URL}/order/placeorder",
                headers=hdrs(), json=b, timeout=15).json()
            log.info(f"{otype} result: {r}")
            if trade_id in open_trades:
                k1 = "tpOrderId" if otype=="Limit" else "slOrderId"
                k2 = "tpPrice"   if otype=="Limit" else "slPrice"
                open_trades[trade_id].update({k1: r.get("orderId"), k2: pval, "status": "open"})
        except Exception as e:
            log.error(f"Bracket {otype} error: {e}")


def cancel_order(order_id):
    try:
        r = requests.post(f"{BASE_URL}/order/cancelorder",
            headers=hdrs(), json={"orderId": order_id}, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def close_position(symbol, qty, side):
    acc   = get_account_id()
    sym   = get_tradovate_symbol(symbol)
    cside = "Buy" if side == "Sell" else "Sell"
    try:
        r = requests.post(f"{BASE_URL}/order/placeorder", headers=hdrs(),
            json={"accountSpec": TRADOVATE_USERNAME, "accountId": acc,
                  "action": cside, "symbol": sym, "orderQty": qty,
                  "orderType": "Market", "isAutomated": True}, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# ─── WEBHOOK ─────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        log.info(f"📨 Webhook: {data}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401

        action     = data.get("action", "")
        symbol     = data.get("symbol", "MNQM2026")
        qty        = int(data.get("qty", 1))
        order_type = data.get("orderType", "Market")
        tp_ticks   = int(data.get("tpTicks", 120))
        sl_ticks   = int(data.get("slTicks", 40))
        stop_off   = int(data.get("stopOffset", 5))
        trail_en   = bool(data.get("trailEnable", False))
        trail_st   = int(data.get("trailStart", 30))
        trail_dist = int(data.get("trailDist", 15))
        trade_id   = data.get("tradeId", f"T_{int(time.time())}")
        tick_size  = float(data.get("tickSize", 0.25))
        price      = float(data.get("price", 0))
        result     = {}

        if action == "short_entry":
            if order_type == "Market":
                result = place_order(symbol=symbol, side="Sell", qty=qty,
                    order_type="Market", price=price, tp_ticks=tp_ticks,
                    sl_ticks=sl_ticks, tick_size=tick_size, trade_id=trade_id)
                if "orderId" in result:
                    def do_bracket():
                        time.sleep(3)
                        place_bracket(trade_id, symbol, "Sell", qty,
                                      price, tp_ticks, sl_ticks, tick_size)
                    threading.Thread(target=do_bracket).start()
            else:
                sp = round(price - (stop_off * tick_size), 2)
                result = place_order(symbol=symbol, side="Sell", qty=qty,
                    order_type="Stop", stop_price=sp, tp_ticks=tp_ticks,
                    sl_ticks=sl_ticks, tick_size=tick_size, trade_id=trade_id)
                if "orderId" in result and trade_id in open_trades:
                    open_trades[trade_id].update({
                        "trailEnable": trail_en,
                        "trailStartTicks": trail_st,
                        "trailDistanceTicks": trail_dist})

        elif action == "long_entry":
            result = place_order(symbol=symbol, side="Buy", qty=qty,
                order_type="Market", price=price, tp_ticks=tp_ticks,
                sl_ticks=sl_ticks, tick_size=tick_size, trade_id=trade_id)
            if "orderId" in result:
                def do_bracket_long():
                    time.sleep(3)
                    place_bracket(trade_id, symbol, "Buy", qty,
                                  price, tp_ticks, sl_ticks, tick_size)
                threading.Thread(target=do_bracket_long).start()

        elif action == "close_all":
            closed = []
            for tid, t in list(open_trades.items()):
                if t["status"] == "open":
                    close_position(t["symbol"], t["qty"], t["side"])
                    open_trades[tid]["status"] = "closed"
                    closed.append(tid)
            result = {"closed": closed}

        elif action == "cancel_pending":
            cancelled = []
            for tid, t in list(open_trades.items()):
                if t["status"] == "pending" and tid == trade_id:
                    cancel_order(t["orderId"])
                    open_trades[tid]["status"] = "cancelled"
                    cancelled.append(tid)
            result = {"cancelled": cancelled}

        elif action == "session_end":
            for tid, t in list(open_trades.items()):
                if t["status"] in ("open", "pending"):
                    if t["status"] == "open":
                        close_position(t["symbol"], t["qty"], t["side"])
                    else:
                        cancel_order(t["orderId"])
                    open_trades[tid]["status"] = "session_closed"
            result = {"message": "Session closed"}

        trade_log.append({"time": datetime.now().isoformat(), "action": action,
                          "tradeId": trade_id, "symbol": symbol, "result": result})
        log.info(f"✅ {action} → {result}")
        return jsonify({"status": "ok", "action": action, "result": result})

    except Exception as e:
        log.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ─── SET TOKEN ENDPOINT ───────────────────────────────────────────────────────
@app.route("/set-token", methods=["POST"])
def set_token():
    """Update access token without redeploying."""
    global access_token, token_expiry, account_id
    data = request.get_json(force=True)
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    new_token = data.get("token", "")
    if not new_token:
        return jsonify({"error": "token required"}), 400
    access_token = new_token
    token_expiry = time.time() + 4800
    account_id   = None  # reset so it re-fetches
    log.info("✅ Token updated via /set-token")
    return jsonify({"status": "ok", "message": "Token updated"})

# ─── TEST AUTH ────────────────────────────────────────────────────────────────
@app.route("/test-auth")
def test_auth():
    tok = get_token()
    acc = get_account_id()
    token_status = "✅ Token set" if tok else "❌ No token"
    acct_status  = f"✅ Account: {acc}" if acc else "❌ No account"
    return jsonify({
        "token_status":   token_status,
        "account_status": acct_status,
        "token_preview":  (tok[:20] + "...") if tok else None,
        "use_demo":       USE_DEMO,
        "base_url":       BASE_URL,
        "username":       TRADOVATE_USERNAME,
    })

# ─── DASHBOARD ───────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def dashboard():
    oc  = sum(1 for t in open_trades.values() if t["status"] == "open")
    pc  = sum(1 for t in open_trades.values() if t["status"] == "pending")
    tok = access_token
    tok_ok = bool(tok and time.time() < token_expiry - 60)

    html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot</title><meta http-equiv="refresh" content="10">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#0f0f1a;color:#e0e0f0;padding:20px}}
h1{{font-size:1.4rem;color:#a78bfa;margin-bottom:4px}}
.sub{{font-size:0.8rem;color:#6b7280;margin-bottom:16px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.75rem;font-weight:600}}
.demo{{background:#1e3a5f;color:#60a5fa}} .warn{{background:#3b2a00;color:#fbbf24}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:16px 0 24px}}
.card{{background:#1a1a2e;border:1px solid #2d2d4e;border-radius:10px;padding:16px}}
.card .lbl{{font-size:.75rem;color:#6b7280;margin-bottom:6px}}
.card .val{{font-size:1.5rem;font-weight:700}}
.g{{color:#34d399}}.b{{color:#60a5fa}}.y{{color:#fbbf24}}.o{{color:#fb923c}}.r{{color:#f87171}}
.alert-box{{background:#2a1a00;border:1px solid #ca8a04;border-radius:8px;padding:14px;margin-bottom:20px}}
.alert-box h3{{color:#fbbf24;font-size:.9rem;margin-bottom:6px}}
.alert-box p{{font-size:.8rem;color:#d97706;line-height:1.6}}
.code{{background:#0f0f1a;padding:6px 10px;border-radius:4px;font-family:monospace;font-size:.78rem;color:#a78bfa;margin-top:6px;word-break:break-all}}
table{{width:100%;border-collapse:collapse;background:#1a1a2e;border-radius:10px;overflow:hidden;margin-bottom:24px}}
th{{background:#2d2d4e;padding:10px 14px;text-align:left;font-size:.75rem;color:#9ca3af}}
td{{padding:9px 14px;font-size:.82rem;border-top:1px solid #2d2d4e}}
.s-open{{color:#34d399}}.s-pending{{color:#fbbf24}}.s-closed,.s-session-closed{{color:#6b7280}}.s-cancelled{{color:#ef4444}}
h2{{font-size:1rem;color:#a78bfa;margin-bottom:10px}}
</style></head><body>
<h1>MNQ Trading Bot</h1>
<p class="sub">Auto-refresh 10s | {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<span class="badge demo">{'DEMO' if USE_DEMO else 'LIVE'} MODE</span>
&nbsp;
<span class="badge {'demo' if tok_ok else 'warn'}">TOKEN: {'✅ ACTIVE' if tok_ok else '⚠️ EXPIRED / MISSING'}</span>
"""
    if not tok_ok:
        html += """<br><br>
<div class="alert-box">
<h3>⚠️ Token Required — Orders Cannot Execute</h3>
<p>Tradovate token waa in la cusbooneysiiyo. Tallaabada:</p>
<p>1. trader.tradovate.com → login → F12 → Network → accesstoken raadi<br>
2. Token-ka koobi → Railway Variables → TRADOVATE_TOKEN ku dar<br>
3. Ama /set-token endpoint isticmaal (hoose eeg)</p>
<div class="code">POST https://web-production-314c3.up.railway.app/set-token<br>
{{"secret":"mnq_secret_2024","token":"eyJ...your_token_here"}}</div>
</div>"""

    html += f"""
<div class="cards">
<div class="card"><div class="lbl">Open</div><div class="val b">{oc}</div></div>
<div class="card"><div class="lbl">Pending</div><div class="val o">{pc}</div></div>
<div class="card"><div class="lbl">Signals</div><div class="val y">{len(trade_log)}</div></div>
<div class="card"><div class="lbl">Status</div><div class="val g">ONLINE</div></div>
</div>
<h2>Trades</h2>
<table><tr><th>ID</th><th>Symbol</th><th>Side</th><th>Entry</th><th>TP</th><th>SL</th><th>Status</th><th>Time</th></tr>"""

    if open_trades:
        for tid, t in list(open_trades.items())[-20:]:
            sc = f"s-{t['status'].replace('_','-')}"
            html += f"<tr><td>{tid}</td><td>{t['symbol']}</td><td>{t['side']}</td><td>{t.get('entryPrice','—')}</td><td>{t.get('tpPrice','—')}</td><td>{t.get('slPrice','—')}</td><td class='{sc}'>{t['status'].upper()}</td><td>{t.get('openTime','')[:19]}</td></tr>"
    else:
        html += "<tr><td colspan='8' style='color:#6b7280;text-align:center'>No trades yet</td></tr>"

    html += "</table><h2>Signal Log</h2><table><tr><th>Time</th><th>Action</th><th>Trade ID</th><th>Symbol</th><th>Result</th></tr>"
    for e in reversed(trade_log[-30:]):
        html += f"<tr><td>{e['time'][:19]}</td><td>{e['action']}</td><td>{e['tradeId']}</td><td>{e['symbol']}</td><td style='color:#6b7280;font-size:.75rem'>{str(e.get('result',''))[:80]}</td></tr>"
    html += "</table></body></html>"
    return html


@app.route("/status")
def status():
    tok_ok = bool(access_token and time.time() < token_expiry - 60)
    open_c = sum(1 for t in open_trades.values() if t["status"] == "open")
    pend_c = sum(1 for t in open_trades.values() if t["status"] == "pending")
    return jsonify({
        "status":        "online",
        "mode":          "DEMO" if USE_DEMO else "LIVE",
        "token_active":  tok_ok,
        "open_trades":   open_c,
        "pending":       pend_c,
        "total_signals": len(trade_log),
        "time":          datetime.now().isoformat(),
    })


@app.route("/trades")
def get_trades():
    return jsonify(open_trades)


@app.route("/logs")
def get_logs():
    return jsonify(trade_log[-50:])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"🚀 MNQ Bot | Port:{port} | {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
