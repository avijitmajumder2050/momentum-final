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
PUT  /api/watchlist/<symbol>         EDIT entry/sl/target     ← NEW
DEL  /api/watchlist/<symbol>    delete symbol
POST /api/watchlist/scan        trigger manual breakout scan
POST /api/watchlist/cleanup          delete stale next-day breakouts ← NEW
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
from log_config import setup_logging
setup_logging()               # stdout + local file + S3 sync thread

from angel_broker     import get_broker
from watchlist_s3     import (load_watchlist, add_symbol, delete_symbol,
                               get_symbol, update_row,edit_symbol,cleanup_old_breakouts)
from trade_s3         import (load_trades, load_active, close_trade,
                               update_trade, already_traded_today,get_trade)
from breakout_engine  import run_breakout_engine
from trade_executor   import execute_trade
from breakout_monitor import get_monitor, start_monitor
from trailing_engine  import get_engine, start_engine


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
    payload = {
        "status":           "ok",
        "monitor_running":  mon.running,
        "auto_buy_enabled": mon.auto_buy_enabled,
        "engine_running":   eng.running,
        "breakouts_live":   mon.status.get("breakouts", []),
        "active_trades":    eng.status.get("active_trades", 0),
        "last_poll":        mon.status.get("last_poll"),
        "last_trade":       mon.status.get("last_trade"),
    }
    log.debug("[API /health] %s", payload)
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
    log.info("[API] GET /api/watchlist")
    try:
        broker   = get_broker()
        enriched = run_breakout_engine(broker)
        log.info("[API] /api/watchlist → %d rows", len(enriched))
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

    log.info("[API] POST /api/watchlist  symbol=%s  entry=%.2f  sl=%.2f  tgt=%.2f",
             symbol, entry_price, sl_price, target_price)
    if not symbol:
        return jsonify({"success":False,"error":"symbol required"}),400
    if sl_price >= entry_price:
        log.warning("[API] /api/watchlist REJECTED — SL %.2f >= entry %.2f",
                    sl_price, entry_price)
        return jsonify({"success":False,"error":"SL must be below entry"}),400
    if target_price <= entry_price:
        log.warning("[API] /api/watchlist REJECTED — target %.2f <= entry %.2f",
                    target_price, entry_price)
        return jsonify({"success":False,"error":"Target must be above entry"}),400

    broker      = get_broker()
    angel_token = broker.get_token(symbol) 
    if not angel_token:
        log.warning("[API] /api/watchlist REJECTED — token not found for %s", symbol)
        return jsonify({
        "success": False,
        "error": f"Token not found for {symbol}. Check token CSV."
    }), 400
        log.warning("[add] Token not found for %s in CSV map", symbol)

    row = add_symbol(symbol, angel_token, entry_price, sl_price, target_price)
    log.info("[API] /api/watchlist  ADDED %s", row)
    return jsonify({"success":True,"row":row})

# ── Watchlist — EDIT  (PUT /api/watchlist/<symbol>) ───────────────────────────
@app.put("/api/watchlist/<symbol>")
def api_edit_symbol(symbol: str):
    """
    Update only price fields (entry / SL / target).
    Token, Breakout, Rank, Action, Breakout_Date are preserved.
    Body: {entry_price, sl_price, target_price}
    """
    b            = request.json or {}
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))
    symbol       = symbol.upper().strip()

    if not entry_price or not sl_price or not target_price:
        return jsonify({"success": False,
                        "error": "entry_price, sl_price, target_price required"}), 400
    if sl_price >= entry_price:
        return jsonify({"success": False, "error": "SL must be below entry"}), 400
    if target_price <= entry_price:
        return jsonify({"success": False, "error": "Target must be above entry"}), 400

    row = edit_symbol(symbol, entry_price, sl_price, target_price)
    if not row:
        return jsonify({"success": False,
                        "error": f"{symbol} not found in watchlist"}), 404
    return jsonify({"success": True, "row": row})
    
@app.delete("/api/watchlist/<symbol>")
def api_delete_symbol(symbol: str):
    symbol = symbol.upper()
    log.info("[API] DELETE /api/watchlist/%s", symbol)
    ok = delete_symbol(symbol.upper())
    log.info("[API] /api/watchlist/%s  deleted=%s", symbol, ok)
    return jsonify({"success":ok})


@app.post("/api/watchlist/scan")
def api_scan():
    """Trigger a manual breakout scan and return enriched rows."""
    log.info("[API] POST /api/watchlist/scan — manual scan triggered")
    try:
        broker   = get_broker()
        enriched = run_breakout_engine(broker)
        log.info("[API] /api/watchlist/scan → %d rows", len(enriched))
        return jsonify({"success":True,"data":enriched})
    except Exception as e:
        log.error("[API] /api/watchlist/scan ERROR: %s", e, exc_info=True)
        return jsonify({"success":False,"error":str(e)}),500


# ── Watchlist — CLEANUP ────────────────────────────────────────────────────────
@app.post("/api/watchlist/cleanup")
def api_cleanup():
    """
    Delete watchlist rows where Breakout=YES and Breakout_Date < today.
    Called by the frontend once per day (on page load) to auto-purge
    yesterday's breakout symbols that were never traded.
    """
    try:
        deleted = cleanup_old_breakouts()
        log.info("[API] cleanup → deleted %s", deleted)
        return jsonify({"success": True, "deleted": deleted, "count": len(deleted)})
    except Exception as e:
        log.error("[API] cleanup error: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500
	
# ── Active trades with live LTP + P&L ────────────────────────────────────────
@app.get("/api/trades/active")
def api_active_trades():
    """
    Returns ACTIVE trades with live LTP and unrealized P&L.
    LTP is fetched live from Angel API — never from CSV.
    """
    try:
        trades = load_active()
        log.info("[API] /api/trades/active  active_count=%d", len(trades))
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
    log.info("[API] GET /api/trades/all")
    trades = load_trades()
    log.info("[API] /api/trades/all  total=%d", len(trades))
    return jsonify({"success":True,"data":load_trades()})


# ── Manual trade ──────────────────────────────────────────────────────────────
@app.post("/api/trade/buy")
def api_manual_buy():
    """
    Body: {symbol, entry_price, sl_price, target_price, qty?}
    Uses token from S3 map. Sizes by live balance.
    qty is optional — if provided and > 0 it overrides auto position-sizing.
    Writes to CSV ONLY if order is COMPLETE.
    """
    b = request.json or {}
    symbol       = b.get("symbol","").upper().strip()
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))
    override_qty = int(b.get("qty",            0))   # 0 = auto-size

    log.info("[API] POST /api/trade/buy  symbol=%s  entry=%.2f  sl=%.2f  tgt=%.2f  qty=%s",
             symbol, entry_price, sl_price, target_price,
             override_qty if override_qty > 0 else "auto")
    if not symbol:
        return jsonify({"success":False,"error":"symbol required"}),400

    broker      = get_broker()
    angel_token = broker.get_token(symbol)
    if not angel_token:
        return jsonify({"success":False,"error":f"Token not found for {symbol}"}),400

    result = execute_trade(broker, symbol, angel_token,
                           entry_price, sl_price, target_price, is_auto=False,  override_qty=override_qty)
    log.info("[API] /api/trade/buy  result = %s", result)
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
    Body: {qty?: int}
      qty absent or 0  → FULL EXIT
        1. Cancel GTT (both legs).
        2. Place market SELL for all qty.
        3. Mark trade CLOSED (MANUAL_CANCEL).

      qty > 0 and < trade qty  → PARTIAL EXIT
        1. Place market SELL for the requested qty.
        2. Modify GTT to remaining qty (keeps SL + target active).
        3. Update trade Qty to remaining.

      qty >= trade qty  → treated as full exit.
    """
    log.info("[API] POST /api/trade/exit/%s  body=%s", order_id, request.json)
    from trade_s3 import get_trade
    trade = get_trade(order_id)
    if not trade:
        return jsonify({"success":False,"error":"Trade not found"}),404
    if trade["Status"] != "ACTIVE":
        log.warning("[API] /api/trade/exit/%s — trade already CLOSED (status=%s)",
                    order_id, trade["Status"])
        return jsonify({"success":False,"error":"Trade already closed"}),400

    broker = get_broker()
    sym    = trade["Symbol"]
    token  = trade["Angel_Token"]
    gtt_id   = trade.get("GTT_ID") or trade.get("GTT_SL_ID") or ""
    total_qty = int(trade["Qty"])
    sl_price  = float(trade["SL_Price"])
    tgt_price = float(trade["Target_Price"])

    # ── Determine exit qty ────────────────────────────────────────────────────
    requested = int((request.json or {}).get("qty", 0))
    is_partial = 0 < requested < total_qty
    exit_qty   = requested if is_partial else total_qty

    log.info("[API] exit/%s  sym=%s  total_qty=%d  exit_qty=%d  partial=%s  gtt=%s",
             order_id, sym, total_qty, exit_qty, is_partial, gtt_id)

    # ── FULL EXIT ─────────────────────────────────────────────────────────────
    if not is_partial:
        # 1. Cancel GTT (if any)
        if gtt_id:
            log.info("[API] full exit — cancelling GTT id=%s", gtt_id)
            res = broker.cancel_gtt(gtt_id, sym, token)
            log.info("[API] GTT cancel response: %s", res)
        else:
            log.info("[API] full exit — no GTT to cancel")

        # 2. Market SELL all qty
        sell = broker.place_sell_market_order(sym, token, total_qty)
        log.info("[API] full exit SELL response: %s", sell)

        # 3. Close trade record
        close_trade(order_id, "MANUAL_CANCEL")
        log.info("[API] trade %s CLOSED (full)", order_id)

        return jsonify({
            "success":    True,
            "exit_type":  "full",
            "exit_qty":   total_qty,
            "sell_order": sell,
        })

    # ── PARTIAL EXIT ──────────────────────────────────────────────────────────
    remaining_qty = total_qty - exit_qty

    # 1. Market SELL partial qty
    sell = broker.place_sell_market_order(sym, token, exit_qty)
    log.info("[API] partial exit SELL %d × %s  response: %s", exit_qty, sym, sell)

    # 2. Modify GTT qty to remaining
    if gtt_id:
        log.info("[API] modifying GTT id=%s  old_qty=%d → new_qty=%d",
                 gtt_id, total_qty, remaining_qty)
        gtt_res = broker.modify_gtt_qty(
            gtt_id, sym, token, remaining_qty, sl_price, tgt_price
        )
        log.info("[API] GTT modify qty response: %s", gtt_res)
    else:
        log.warning("[API] partial exit — no GTT_ID, skipping GTT modify")
        gtt_res = {"status": "skipped", "message": "no GTT_ID"}

    # 3. Update trade Qty to remaining
    update_trade(order_id, {
        "Qty":       str(remaining_qty),
        "SubAction": f"PARTIAL_EXIT_{exit_qty}",
    })
    log.info("[API] trade %s qty updated → %d remaining", order_id, remaining_qty)

    return jsonify({
        "success":       True,
        "exit_type":     "partial",
        "exit_qty":      exit_qty,
        "remaining_qty": remaining_qty,
        "sell_order":    sell,
        "gtt_modify":    gtt_res,
    })

    


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
    log.info("[API] GET /api/pnl")
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
