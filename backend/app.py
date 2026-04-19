"""
app.py
======
Momentum Trading Platform — Flask REST API

Boot order
----------
1. ssm_config.bootstrap()
2. start_monitor()  — breakout + auto-buy thread
3. start_engine()   — trailing SL thread
4. Flask serves

Routes
------
GET  /health
GET  /api/watchlist             all watchlist rows + live LTP
POST /api/watchlist             add symbol
DEL  /api/watchlist/<symbol>    delete symbol
POST /api/watchlist/scan        trigger manual breakout scan
GET  /api/trades/active         active trades + live LTP + P&L
GET  /api/trades/all            all trades (active + closed)
POST /api/trade/buy             manual trade execution
POST /api/trade/exit/<order_id> manual exit
POST /api/auto_buy_toggle       arm/disarm auto-buy
GET  /api/auto_buy_status       monitor + engine status
GET  /api/pnl                   today P&L + balance
GET  /api/search_symbol?q=X     Angel token lookup from CSV map
"""
import os, logging
from flask import Flask, request, jsonify
from flask_cors import CORS

from ssm_config import bootstrap
bootstrap()

from angel_broker     import get_broker
from watchlist_s3     import (load_watchlist, add_symbol, delete_symbol,
                               get_symbol, update_row)
from trade_s3         import (load_trades, load_active, close_trade,
                               update_trade, already_traded_today)
from breakout_engine  import run_breakout_engine
from trade_executor   import execute_trade
from breakout_monitor import get_monitor, start_monitor
from trailing_engine  import get_engine, start_engine

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*":{"origins":"*"}})

start_monitor()
start_engine()


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    mon = get_monitor()
    eng = get_engine()
    return jsonify({
        "status":           "ok",
        "monitor_running":  mon.running,
        "auto_buy_enabled": mon.auto_buy_enabled,
        "engine_running":   eng.running,
        "breakouts_live":   mon.status.get("breakouts",[]),
        "active_trades":    eng.status.get("active_trades",0),
        "last_poll":        mon.status.get("last_poll"),
        "last_trade":       mon.status.get("last_trade"),
    })


# ── Watchlist ─────────────────────────────────────────────────────────────────
@app.get("/api/watchlist")
def api_watchlist():
    """
    Returns watchlist rows enriched with live LTP.
    LTP is fetched here — never from CSV.
    """
    try:
        broker   = get_broker()
        enriched = run_breakout_engine(broker)
        return jsonify({"success":True,"data":enriched})
    except Exception as e:
        log.error("[watchlist] %s", e, exc_info=True)
        return jsonify({"success":False,"error":str(e)}), 500


@app.post("/api/watchlist")
def api_add_symbol():
    """
    Body: {symbol, entry_price, sl_price, target_price}
    Angel_Token resolved from S3 token map.
    """
    b = request.json or {}
    symbol       = b.get("symbol","").upper().strip()
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))

    if not symbol:
        return jsonify({"success":False,"error":"symbol required"}),400
    if sl_price >= entry_price:
        return jsonify({"success":False,"error":"SL must be below entry"}),400
    if target_price <= entry_price:
        return jsonify({"success":False,"error":"Target must be above entry"}),400

    broker      = get_broker()
    angel_token = broker.get_token(symbol) or ""
    if not angel_token:
        log.warning("[add] Token not found for %s in CSV map", symbol)

    row = add_symbol(symbol, angel_token, entry_price, sl_price, target_price)
    return jsonify({"success":True,"row":row})


@app.delete("/api/watchlist/<symbol>")
def api_delete_symbol(symbol: str):
    ok = delete_symbol(symbol.upper())
    return jsonify({"success":ok})


@app.post("/api/watchlist/scan")
def api_scan():
    """Trigger a manual breakout scan and return enriched rows."""
    try:
        broker   = get_broker()
        enriched = run_breakout_engine(broker)
        return jsonify({"success":True,"data":enriched})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)}),500


# ── Active trades with live LTP + P&L ────────────────────────────────────────
@app.get("/api/trades/active")
def api_active_trades():
    """
    Returns ACTIVE trades with live LTP and unrealized P&L.
    LTP is fetched live from Angel API — never from CSV.
    """
    try:
        trades = load_active()
        if not trades:
            return jsonify({"success":True,"data":[]})

        broker      = get_broker()
        instruments = [
            {"symboltoken":t["Angel_Token"],"tradingsymbol":t["Symbol"]}
            for t in trades if t.get("Angel_Token")
        ]
        live = broker.get_bulk_ltp(instruments) if instruments else {}

        enriched = []
        for t in trades:
            row     = dict(t)
            quote   = live.get(t["Symbol"],{})
            ltp     = float(quote.get("ltp",0) or 0)
            entry   = float(t.get("Entry_Price",0) or 0)
            qty     = int(t.get("Qty",0) or 0)
            pnl     = round((ltp - entry) * qty, 2) if ltp and entry and qty else 0
            row["ltp"]         = ltp    # in-memory only
            row["live_pnl"]    = pnl
            row["pct_change"]  = round((ltp-entry)/entry*100,2) if entry else 0
            enriched.append(row)

        return jsonify({"success":True,"data":enriched})
    except Exception as e:
        log.error("[active_trades] %s", e, exc_info=True)
        return jsonify({"success":False,"error":str(e)}),500


@app.get("/api/trades/all")
def api_all_trades():
    return jsonify({"success":True,"data":load_trades()})


# ── Manual trade ──────────────────────────────────────────────────────────────
@app.post("/api/trade/buy")
def api_manual_buy():
    """
    Body: {symbol, entry_price, sl_price, target_price}
    Uses token from S3 map. Sizes by live balance.
    Writes to CSV ONLY if order is COMPLETE.
    """
    b = request.json or {}
    symbol       = b.get("symbol","").upper().strip()
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))

    if not symbol:
        return jsonify({"success":False,"error":"symbol required"}),400

    broker      = get_broker()
    angel_token = broker.get_token(symbol)
    if not angel_token:
        return jsonify({"success":False,"error":f"Token not found for {symbol}"}),400

    result = execute_trade(broker, symbol, angel_token,
                           entry_price, sl_price, target_price, is_auto=False)
    if result["success"]:
        return jsonify(result)
    return jsonify(result), 400


# ── Manual exit ───────────────────────────────────────────────────────────────
@app.post("/api/trade/exit/<order_id>")
def api_manual_exit(order_id: str):
    """
    Immediate manual exit:
    1. Cancel both GTT legs.
    2. Place market SELL.
    3. Mark trade CLOSED with MANUAL_CANCEL.
    """
    from trade_s3 import get_trade
    trade = get_trade(order_id)
    if not trade:
        return jsonify({"success":False,"error":"Trade not found"}),404
    if trade["Status"] != "ACTIVE":
        return jsonify({"success":False,"error":"Trade already closed"}),400

    broker = get_broker()
    sym    = trade["Symbol"]
    token  = trade["Angel_Token"]
    qty    = int(trade["Qty"])

    if trade.get("GTT_Target_ID"):
        broker.cancel_gtt(trade["GTT_Target_ID"], sym, token)
    if trade.get("GTT_SL_ID"):
        broker.cancel_gtt(trade["GTT_SL_ID"], sym, token)

    sell = broker.place_sell_market_order(sym, token, qty)
    close_trade(order_id, "MANUAL_CANCEL")

    return jsonify({"success":True,"sell_order":sell})


# ── Auto-buy control ──────────────────────────────────────────────────────────
@app.post("/api/auto_buy_toggle")
def api_auto_buy_toggle():
    enabled = bool((request.json or {}).get("enabled",False))
    get_monitor().auto_buy_enabled = enabled
    log.info("[API] Auto-buy %s", "ENABLED" if enabled else "DISABLED")
    return jsonify({"success":True,"auto_buy_enabled":enabled})


@app.get("/api/auto_buy_status")
def api_auto_buy_status():
    mon = get_monitor()
    eng = get_engine()
    return jsonify({
        "success":True,
        "auto_buy_enabled":  mon.auto_buy_enabled,
        "breakouts_live":    mon.status.get("breakouts",[]),
        "last_trade":        mon.status.get("last_trade"),
        "active_trades":     eng.status.get("active_trades",0),
        "trailing_last_run": eng.status.get("last_run"),
    })


# ── P&L ───────────────────────────────────────────────────────────────────────
@app.get("/api/pnl")
def api_pnl():
    try:
        broker = get_broker()
        return jsonify({
            "success":True,
            "pnl":               broker.get_today_pnl(),
            "available_balance": broker.get_funds().get("available_balance",0),
        })
    except Exception as e:
        return jsonify({"success":False,"error":str(e)}),500


# ── Symbol search (from S3 token map) ────────────────────────────────────────
@app.get("/api/search_symbol")
def api_search_symbol():
    """Search the in-memory token_map — no Angel API call needed."""
    q = request.args.get("q","").upper().strip()
    if len(q) < 2:
        return jsonify({"success":False,"error":"q must be >= 2 chars"}),400
    broker  = get_broker()
    results = [
        {"symbol":k,"token":v["token"],"margin":v["margin"]}
        for k,v in broker.token_map.items()
        if q in k
    ][:15]
    return jsonify({"success":True,"data":results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")), debug=False)
