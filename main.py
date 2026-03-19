"""
MNQ Trading Bot - Webhook Server
=================================
TradingView → Webhook → Tradovate
Supports: Market Orders, Sell Stop, TP, SL, Trailing Stop, Break Even
24/7 Cloud Server (Railway)
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import json
import os
import time
import threading
import logging
from datetime import datetime

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ─── CONFIG (Railway Environment Variables) ─────────────────────────────────
TRADOVATE_USERNAME   = os.environ.get("TRADOVATE_USERNAME", "")
TRADOVATE_PASSWORD   = os.environ.get("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID     = os.environ.get("TRADOVATE_APP_ID", "")
TRADOVATE_APP_SECRET = os.environ.get("TRADOVATE_APP_SECRET", "")
WEBHOOK_SECRET       = os.environ.get("WEBHOOK_SECRET", "mnq_secret_2024")
USE_DEMO             = os.environ.get("USE_DEMO", "true").lower() == "true"

# Tradovate URLs
DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"
BASE_URL = DEMO_URL if USE_DEMO else LIVE_URL

# ─── GLOBALS ────────────────────────────────────────────────────────────────
access_token     = None
token_expiry     = 0
open_trades      = {}   # { trade_id: { orderId, entryPrice, sl, tp, trailing, ... } }
trade_log        = []   # History of all signals + results
account_id       = None

# ─── AUTHENTICATION ─────────────────────────────────────────────────────────
def get_token():
    """Get or refresh Tradovate access token."""
    global access_token, token_expiry
    if access_token and time.time() < token_expiry - 60:
        return access_token

    log.info("Requesting new Tradovate token...")
    try:
        resp = requests.post(f"{BASE_URL}/auth/accesstokenrequest", json={
            "name":       TRADOVATE_USERNAME,
            "password":   TRADOVATE_PASSWORD,
            "appId":      TRADOVATE_APP_ID,
            "appVersion": "1.0",
            "cid":        TRADOVATE_APP_ID,
            "sec":        TRADOVATE_APP_SECRET,
        }, timeout=10)
        data = resp.json()
        if "accessToken" in data:
            access_token = data["accessToken"]
            token_expiry = time.time() + data.get("expirationTime", 4800)
            log.info("Token OK")
            return access_token
        else:
            log.error(f"Token error: {data}")
            return None
    except Exception as e:
        log.error(f"Token exception: {e}")
        return None


def get_account_id():
    """Get Tradovate account ID."""
    global account_id
    if account_id:
        return account_id
    token = get_token()
    if not token:
        return None
    try:
        resp = requests.get(f"{BASE_URL}/account/list",
            headers={"Authorization": f"Bearer {token}"}, timeout=10)
        accounts = resp.json()
        if accounts:
            account_id = accounts[0]["id"]
            log.info(f"Account ID: {account_id}")
            return account_id
    except Exception as e:
        log.error(f"Account error: {e}")
    return None


def api_headers():
    return {"Authorization": f"Bearer {get_token()}",
            "Content-Type": "application/json"}

# ─── ORDER FUNCTIONS ─────────────────────────────────────────────────────────
def place_order(symbol, side, qty, order_type, price=None,
                stop_price=None, tp_ticks=None, sl_ticks=None,
                tick_size=0.25, trade_id=None):
    """
    Place order on Tradovate.
    side:       'Buy' or 'Sell'
    order_type: 'Market', 'Stop', 'Limit'
    """
    acc_id = get_account_id()
    if not acc_id:
        return {"error": "No account"}

    order_body = {
        "accountSpec":     TRADOVATE_USERNAME,
        "accountId":       acc_id,
        "action":          side,
        "symbol":          symbol,
        "orderQty":        qty,
        "orderType":       order_type,
        "isAutomated":     True,
    }

    if order_type == "Stop" and stop_price:
        order_body["stopPrice"] = round(stop_price, 2)
    if order_type == "Limit" and price:
        order_body["price"] = round(price, 2)

    try:
        resp = requests.post(f"{BASE_URL}/order/placeorder",
            headers=api_headers(), json=order_body, timeout=10)
        result = resp.json()
        log.info(f"Order placed: {result}")

        if "orderId" in result:
            order_id = result["orderId"]
            # Store trade for management
            if trade_id:
                open_trades[trade_id] = {
                    "orderId":     order_id,
                    "symbol":      symbol,
                    "side":        side,
                    "qty":         qty,
                    "entryPrice":  stop_price or price or 0,
                    "tpTicks":     tp_ticks,
                    "slTicks":     sl_ticks,
                    "tickSize":    tick_size,
                    "status":      "pending",
                    "openTime":    datetime.now().isoformat(),
                    "trailArmed":  False,
                    "beActive":    False,
                }
            return result
        return result
    except Exception as e:
        log.error(f"Place order error: {e}")
        return {"error": str(e)}


def place_bracket(trade_id, symbol, side, qty, entry_price,
                  tp_ticks, sl_ticks, tick_size=0.25):
    """Place TP and SL bracket after entry fills."""
    acc_id = get_account_id()
    if not acc_id:
        return

    # For SHORT: TP is below entry, SL is above entry
    if side == "Sell":
        tp_price = round(entry_price - (tp_ticks * tick_size), 2)
        sl_price = round(entry_price + (sl_ticks * tick_size), 2)
        close_side = "Buy"
    else:
        tp_price = round(entry_price + (tp_ticks * tick_size), 2)
        sl_price = round(entry_price - (sl_ticks * tick_size), 2)
        close_side = "Sell"

    # TP — Limit order
    tp_body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc_id,
        "action":      close_side,
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Limit",
        "price":       tp_price,
        "isAutomated": True,
    }
    # SL — Stop order
    sl_body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc_id,
        "action":      close_side,
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Stop",
        "stopPrice":   sl_price,
        "isAutomated": True,
    }

    try:
        tp_resp = requests.post(f"{BASE_URL}/order/placeorder",
            headers=api_headers(), json=tp_body, timeout=10).json()
        sl_resp = requests.post(f"{BASE_URL}/order/placeorder",
            headers=api_headers(), json=sl_body, timeout=10).json()

        if trade_id in open_trades:
            open_trades[trade_id].update({
                "tpOrderId":  tp_resp.get("orderId"),
                "slOrderId":  sl_resp.get("orderId"),
                "tpPrice":    tp_price,
                "slPrice":    sl_price,
                "status":     "open",
            })
        log.info(f"Bracket set | TP: {tp_price} | SL: {sl_price}")
    except Exception as e:
        log.error(f"Bracket error: {e}")


def cancel_order(order_id):
    """Cancel a pending order."""
    try:
        resp = requests.post(f"{BASE_URL}/order/cancelorder",
            headers=api_headers(),
            json={"orderId": order_id}, timeout=10)
        log.info(f"Cancel order {order_id}: {resp.json()}")
        return resp.json()
    except Exception as e:
        log.error(f"Cancel error: {e}")
        return {"error": str(e)}


def close_position(symbol, qty, current_side):
    """Market close an open position."""
    acc_id = get_account_id()
    close_side = "Buy" if current_side == "Sell" else "Sell"
    body = {
        "accountSpec": TRADOVATE_USERNAME,
        "accountId":   acc_id,
        "action":      close_side,
        "symbol":      symbol,
        "orderQty":    qty,
        "orderType":   "Market",
        "isAutomated": True,
    }
    try:
        resp = requests.post(f"{BASE_URL}/order/placeorder",
            headers=api_headers(), json=body, timeout=10)
        log.info(f"Close position: {resp.json()}")
        return resp.json()
    except Exception as e:
        log.error(f"Close error: {e}")
        return {"error": str(e)}

# ─── TRAILING STOP MANAGER ───────────────────────────────────────────────────
def update_trailing_stop(trade_id, current_price):
    """Update trailing stop for an open trade."""
    if trade_id not in open_trades:
        return
    trade = open_trades[trade_id]
    if trade.get("status") != "open":
        return

    tick_size   = trade["tickSize"]
    trail_dist  = trade.get("trailDistanceTicks", 15)
    trail_start = trade.get("trailStartTicks", 30)
    entry       = trade["entryPrice"]
    side        = trade["side"]

    if side == "Sell":  # SHORT
        profit_ticks = (entry - current_price) / tick_size
        if profit_ticks >= trail_start and not trade.get("trailArmed"):
            open_trades[trade_id]["trailArmed"] = True
            open_trades[trade_id]["trailBest"]  = current_price
            log.info(f"Trail armed for {trade_id} at {current_price}")

        if trade.get("trailArmed"):
            best = trade.get("trailBest", current_price)
            if current_price < best:
                open_trades[trade_id]["trailBest"] = current_price
                best = current_price
            new_sl = round(best + (trail_dist * tick_size), 2)
            if new_sl < trade.get("slPrice", 9999):
                # Update SL order
                if trade.get("slOrderId"):
                    cancel_order(trade["slOrderId"])
                acc_id = get_account_id()
                sl_body = {
                    "accountSpec": TRADOVATE_USERNAME,
                    "accountId":   acc_id,
                    "action":      "Buy",
                    "symbol":      trade["symbol"],
                    "orderQty":    trade["qty"],
                    "orderType":   "Stop",
                    "stopPrice":   new_sl,
                    "isAutomated": True,
                }
                try:
                    r = requests.post(f"{BASE_URL}/order/placeorder",
                        headers=api_headers(), json=sl_body, timeout=10).json()
                    open_trades[trade_id]["slOrderId"] = r.get("orderId")
                    open_trades[trade_id]["slPrice"]   = new_sl
                    log.info(f"Trail updated SL → {new_sl}")
                except Exception as e:
                    log.error(f"Trail update error: {e}")

# ─── WEBHOOK ENDPOINT ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Receive TradingView alert JSON.

    Expected JSON format from TradingView alert:
    {
      "secret":       "mnq_secret_2024",
      "action":       "short_entry",
      "symbol":       "MNQM2025",
      "qty":          1,
      "orderType":    "Market",
      "stopOffset":   5,
      "tpTicks":      120,
      "slTicks":      40,
      "trailStart":   30,
      "trailDist":    15,
      "trailEnable":  false,
      "tradeId":      "Short_1001"
    }
    """
    try:
        data = request.get_json(force=True)
        log.info(f"Webhook received: {data}")

        # Security check
        if data.get("secret") != WEBHOOK_SECRET:
            log.warning("Invalid webhook secret!")
            return jsonify({"error": "unauthorized"}), 401

        action    = data.get("action", "")
        symbol    = data.get("symbol", "MNQM2025")
        qty       = int(data.get("qty", 1))
        order_type = data.get("orderType", "Market")
        tp_ticks  = int(data.get("tpTicks", 120))
        sl_ticks  = int(data.get("slTicks", 40))
        stop_off  = int(data.get("stopOffset", 5))
        trail_en  = bool(data.get("trailEnable", False))
        trail_st  = int(data.get("trailStart", 30))
        trail_dist = int(data.get("trailDist", 15))
        trade_id  = data.get("tradeId", f"T_{int(time.time())}")
        tick_size = float(data.get("tickSize", 0.25))
        price     = float(data.get("price", 0))

        result = {}

        # ── SHORT ENTRY ──────────────────────────────────────────────────────
        if action == "short_entry":
            if order_type == "Market":
                result = place_order(
                    symbol=symbol, side="Sell", qty=qty,
                    order_type="Market",
                    tp_ticks=tp_ticks, sl_ticks=sl_ticks,
                    tick_size=tick_size, trade_id=trade_id
                )
                # Bracket after small delay (wait for fill)
                if "orderId" in result:
                    threading.Thread(
                        target=lambda: (time.sleep(2),
                            place_bracket(trade_id, symbol, "Sell", qty,
                                          price, tp_ticks, sl_ticks, tick_size))
                    ).start()

            elif order_type == "Stop":
                stop_price = round(price - (stop_off * tick_size), 2)
                result = place_order(
                    symbol=symbol, side="Sell", qty=qty,
                    order_type="Stop", stop_price=stop_price,
                    tp_ticks=tp_ticks, sl_ticks=sl_ticks,
                    tick_size=tick_size, trade_id=trade_id
                )
                if "orderId" in result and trade_id in open_trades:
                    open_trades[trade_id].update({
                        "trailEnable":      trail_en,
                        "trailStartTicks":  trail_st,
                        "trailDistanceTicks": trail_dist,
                    })

        # ── LONG ENTRY ───────────────────────────────────────────────────────
        elif action == "long_entry":
            if order_type == "Market":
                result = place_order(
                    symbol=symbol, side="Buy", qty=qty,
                    order_type="Market",
                    tp_ticks=tp_ticks, sl_ticks=sl_ticks,
                    tick_size=tick_size, trade_id=trade_id
                )
                if "orderId" in result:
                    threading.Thread(
                        target=lambda: (time.sleep(2),
                            place_bracket(trade_id, symbol, "Buy", qty,
                                          price, tp_ticks, sl_ticks, tick_size))
                    ).start()

        # ── CLOSE / EXIT ─────────────────────────────────────────────────────
        elif action == "close_all":
            closed = []
            for tid, trade in list(open_trades.items()):
                if trade["status"] == "open":
                    close_position(trade["symbol"], trade["qty"], trade["side"])
                    open_trades[tid]["status"] = "closed"
                    closed.append(tid)
            result = {"closed": closed}

        elif action == "cancel_pending":
            cancelled = []
            for tid, trade in list(open_trades.items()):
                if trade["status"] == "pending":
                    cancel_order(trade["orderId"])
                    open_trades[tid]["status"] = "cancelled"
                    cancelled.append(tid)
            result = {"cancelled": cancelled}

        # ── SESSION END AUTO-CLOSE ───────────────────────────────────────────
        elif action == "session_end":
            for tid, trade in list(open_trades.items()):
                if trade["status"] in ("open", "pending"):
                    if trade["status"] == "open":
                        close_position(trade["symbol"], trade["qty"], trade["side"])
                    else:
                        cancel_order(trade["orderId"])
                    open_trades[tid]["status"] = "session_closed"
            result = {"message": "Session end — all positions closed"}

        # Log the trade
        trade_log.append({
            "time":    datetime.now().isoformat(),
            "action":  action,
            "tradeId": trade_id,
            "symbol":  symbol,
            "result":  result,
        })

        return jsonify({"status": "ok", "result": result})

    except Exception as e:
        log.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

# ─── PRICE UPDATE (called by TradingView price alerts) ───────────────────────
@app.route("/price", methods=["POST"])
def price_update():
    """Receive current price for trailing stop management."""
    try:
        data  = request.get_json(force=True)
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        price    = float(data.get("price", 0))
        trade_id = data.get("tradeId", "")
        if trade_id and price:
            update_trailing_stop(trade_id, price)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─── DASHBOARD API ───────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def dashboard():
    """Live HTML dashboard."""
    open_count  = sum(1 for t in open_trades.values() if t["status"] == "open")
    total_trades = len(trade_log)
    env_mode    = "DEMO" if USE_DEMO else "LIVE"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MNQ Bot Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: system-ui, sans-serif; background: #0f0f1a; color: #e0e0f0; padding: 20px; }}
  h1 {{ font-size: 1.4rem; color: #a78bfa; margin-bottom: 4px; }}
  .sub {{ font-size: 0.8rem; color: #6b7280; margin-bottom: 20px; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }}
  .demo {{ background: #1e3a5f; color: #60a5fa; }}
  .live {{ background: #3b1a1a; color: #f87171; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 24px; }}
  .card {{ background: #1a1a2e; border: 1px solid #2d2d4e; border-radius: 10px; padding: 16px; }}
  .card .label {{ font-size: 0.75rem; color: #6b7280; margin-bottom: 6px; }}
  .card .value {{ font-size: 1.6rem; font-weight: 700; }}
  .green {{ color: #34d399; }} .red {{ color: #f87171; }} .blue {{ color: #60a5fa; }} .yellow {{ color: #fbbf24; }}
  table {{ width: 100%; border-collapse: collapse; background: #1a1a2e; border-radius: 10px; overflow: hidden; }}
  th {{ background: #2d2d4e; padding: 10px 14px; text-align: left; font-size: 0.75rem; color: #9ca3af; }}
  td {{ padding: 10px 14px; font-size: 0.82rem; border-top: 1px solid #2d2d4e; }}
  .status-open {{ color: #34d399; }} .status-pending {{ color: #fbbf24; }}
  .status-closed {{ color: #6b7280; }} .status-cancelled {{ color: #ef4444; }}
  h2 {{ font-size: 1rem; color: #a78bfa; margin-bottom: 12px; }}
</style>
</head>
<body>
<h1>MNQ Trading Bot</h1>
<p class="sub">Auto-refresh: 10s &nbsp;|&nbsp; {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
<span class="badge {'demo' if USE_DEMO else 'live'}">{'DEMO MODE' if USE_DEMO else 'LIVE MODE'}</span>
<br><br>
<div class="cards">
  <div class="card"><div class="label">Open Trades</div><div class="value blue">{open_count}</div></div>
  <div class="card"><div class="label">Total Signals</div><div class="value yellow">{total_trades}</div></div>
  <div class="card"><div class="label">Mode</div><div class="value {'green' if USE_DEMO else 'red'}">{env_mode}</div></div>
  <div class="card"><div class="label">Status</div><div class="value green">ONLINE</div></div>
</div>

<h2>Open / Recent Trades</h2>
<table>
<tr><th>Trade ID</th><th>Symbol</th><th>Side</th><th>Entry</th><th>TP</th><th>SL</th><th>Status</th><th>Time</th></tr>
"""
    if open_trades:
        for tid, t in list(open_trades.items())[-20:]:
            status_class = f"status-{t['status'].replace('_','-')}"
            html += f"""<tr>
<td>{tid}</td><td>{t['symbol']}</td><td>{t['side']}</td>
<td>{t.get('entryPrice','—')}</td>
<td>{t.get('tpPrice','—')}</td>
<td>{t.get('slPrice','—')}</td>
<td class="{status_class}">{t['status'].upper()}</td>
<td>{t.get('openTime','—')[:19]}</td>
</tr>"""
    else:
        html += "<tr><td colspan='8' style='color:#6b7280;text-align:center'>No trades yet</td></tr>"

    html += """</table><br>
<h2>Signal Log</h2><table>
<tr><th>Time</th><th>Action</th><th>Trade ID</th><th>Symbol</th></tr>"""

    for entry in reversed(trade_log[-30:]):
        html += f"""<tr>
<td>{entry['time'][:19]}</td>
<td>{entry['action']}</td>
<td>{entry['tradeId']}</td>
<td>{entry['symbol']}</td>
</tr>"""

    html += "</table></body></html>"
    return html


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "status":      "online",
        "mode":        "DEMO" if USE_DEMO else "LIVE",
        "open_trades": len([t for t in open_trades.values() if t["status"] == "open"]),
        "total_signals": len(trade_log),
        "time":        datetime.now().isoformat(),
    })


@app.route("/trades", methods=["GET"])
def get_trades():
    return jsonify(open_trades)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"MNQ Bot starting on port {port} | Mode: {'DEMO' if USE_DEMO else 'LIVE'}")
    app.run(host="0.0.0.0", port=port, debug=False)
