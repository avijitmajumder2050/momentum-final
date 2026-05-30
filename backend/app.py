"""
app.py
======
Momentum Trading Platform — Flask REST API  (v2 — GTT-free)

Boot order
----------
1. ssm_config.bootstrap()
2. start_monitor()      — breakout + auto-buy thread
3. start_engine()       — trailing SL thread
4. start_scheduler()    — next-day SL validation thread   ← NEW
5. Flask serves

Routes
------
GET  /health
GET  /api/watchlist             all watchlist rows + live LTP
POST /api/watchlist             add symbol
PUT  /api/watchlist/<symbol>    edit entry/sl/target
DEL  /api/watchlist/<symbol>    delete symbol
POST /api/watchlist/scan        trigger manual breakout scan
POST /api/watchlist/cleanup     delete stale next-day breakouts
GET  /api/trades/active         active trades + live LTP + P&L
GET  /api/trades/all            all trades (active + closed)
POST /api/trade/buy             manual trade execution
POST /api/trade/exit/<order_id> manual exit (full or partial)
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
setup_logging()

from angel_broker        import get_broker
from watchlist_s3        import (load_watchlist, add_symbol, delete_symbol,
                                  get_symbol, update_row, edit_symbol,
                                  cleanup_old_breakouts)
from trade_s3            import (load_trades, load_active, close_trade,
                                  update_trade, already_traded_today,
                                  get_trade, record_partial_booking,
                                  get_remaining_qty)
from breakout_engine     import run_breakout_engine
from trade_executor      import execute_trade
from breakout_monitor    import get_monitor, start_monitor
from trailing_engine     import get_engine, start_engine
from exit_scheduler      import get_scheduler, start_scheduler   # ← NEW

log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ── Boot sequence ─────────────────────────────────────────────────────────────
start_monitor()
start_engine()
start_scheduler()   # ← next-day SL validation thread


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    mon  = get_monitor()
    eng  = get_engine()
    sched= get_scheduler()
    return jsonify({
        "status":              "ok",
        "monitor_running":     mon.running,
        "auto_buy_enabled":    mon.auto_buy_enabled,
        "engine_running":      eng.running,
        "scheduler_running":   sched.running,
        "breakouts_live":      mon.status.get("breakouts", []),
        "active_trades":       eng.status.get("active_trades", 0),
        "sl_pending_trades":   sched.status.get("pending_count", 0),
        "last_poll":           mon.status.get("last_poll"),
        "last_trade":          mon.status.get("last_trade"),
    })


# ── Watchlist ─────────────────────────────────────────────────────────────────
@app.get("/api/watchlist")
def api_watchlist():
    log.info("[API] GET /api/watchlist")
    try:
        broker   = get_broker()
        enriched = run_breakout_engine(broker)
        return jsonify({"success": True, "data": enriched})
    except Exception as e:
        log.error("[watchlist] %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.post("/api/watchlist")
def api_add_symbol():
    b            = request.json or {}
    symbol       = b.get("symbol", "").upper().strip()
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))

    if not symbol:
        return jsonify({"success": False, "error": "symbol required"}), 400
    if sl_price >= entry_price:
        return jsonify({"success": False, "error": "SL must be below entry"}), 400
    if target_price <= entry_price:
        return jsonify({"success": False, "error": "Target must be above entry"}), 400

    broker      = get_broker()
    angel_token = broker.get_token(symbol)
    if not angel_token:
        return jsonify({"success": False,
                        "error": f"Token not found for {symbol}. Check token CSV."}), 400

    row = add_symbol(symbol, angel_token, entry_price, sl_price, target_price)
    return jsonify({"success": True, "row": row})


@app.put("/api/watchlist/<symbol>")
def api_edit_symbol(symbol: str):
    b            = request.json or {}
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))
    symbol       = symbol.upper().strip()

    if not (entry_price and sl_price and target_price):
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
    ok = delete_symbol(symbol.upper())
    return jsonify({"success": ok})


@app.post("/api/watchlist/scan")
def api_scan():
    try:
        broker   = get_broker()
        enriched = run_breakout_engine(broker)
        return jsonify({"success": True, "data": enriched})
    except Exception as e:
        log.error("[API] scan ERROR: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.post("/api/watchlist/cleanup")
def api_cleanup():
    try:
        deleted = cleanup_old_breakouts()
        return jsonify({"success": True, "deleted": deleted, "count": len(deleted)})
    except Exception as e:
        log.error("[API] cleanup error: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


# ── Trades ────────────────────────────────────────────────────────────────────
@app.get("/api/trades/active")
def api_active_trades():
    log.info("[API] GET /api/trades/active")
    try:
        broker = get_broker()
        rows   = load_active()         # ACTIVE + SL_TRIGGER_PENDING

        enriched = []
        for t in rows:
            r   = dict(t)
            sym = t["Symbol"]
            tok = t["Angel_Token"]
            try:
                ltp = broker.get_ltp_with_retry("NSE", sym, tok, retries=2)
                r["ltp"] = ltp or 0
                entry    = float(t["Entry_Price"])
                rem_qty  = get_remaining_qty(t)
                r["unrealized_pnl"] = round((ltp - entry) * rem_qty, 2) if ltp else 0
            except Exception:
                r["ltp"] = 0
                r["unrealized_pnl"] = 0
            enriched.append(r)

        return jsonify({"success": True, "data": enriched})
    except Exception as e:
        log.error("[API] active_trades: %s", e, exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@app.get("/api/trades/all")
def api_all_trades():
    return jsonify({"success": True, "data": load_trades()})


# ── Manual BUY ────────────────────────────────────────────────────────────────
@app.post("/api/trade/buy")
def api_manual_buy():
    b            = request.json or {}
    symbol       = b.get("symbol", "").upper().strip()
    entry_price  = float(b.get("entry_price",  0))
    sl_price     = float(b.get("sl_price",     0))
    target_price = float(b.get("target_price", 0))
    override_qty = int(b.get("qty",            0))

    if not symbol:
        return jsonify({"success": False, "error": "symbol required"}), 400

    broker      = get_broker()
    angel_token = broker.get_token(symbol)
    if not angel_token:
        return jsonify({"success": False,
                        "error": f"Token not found for {symbol}"}), 400

    result = execute_trade(
        broker, symbol, angel_token,
        entry_price, sl_price, target_price,
        is_auto=False, override_qty=override_qty,
    )
    return jsonify(result) if result["success"] else (jsonify(result), 400)


# ── Manual EXIT ───────────────────────────────────────────────────────────────
@app.post("/api/trade/exit/<order_id>")
def api_manual_exit(order_id: str):
    """
    Immediate manual exit (full or partial).

    Full exit  : MARKET SELL all remaining qty → CLOSED
    Partial    : MARKET SELL requested qty → record_partial_booking → ACTIVE

    GTT is no longer used — engine owns all exits.
    """
    log.info("[API] POST /api/trade/exit/%s  body=%s", order_id, request.json)

    trade = get_trade(order_id)
    if not trade:
        return jsonify({"success": False, "error": "Trade not found"}), 404
    if trade["Status"] == "CLOSED":
        return jsonify({"success": False, "error": "Trade already closed"}), 400

    broker    = get_broker()
    sym       = trade["Symbol"]
    token     = trade["Angel_Token"]
    rem_qty   = get_remaining_qty(trade)

    requested  = int((request.json or {}).get("qty", 0))
    is_partial = 0 < requested < rem_qty
    exit_qty   = requested if is_partial else rem_qty

    if exit_qty <= 0:
        return jsonify({"success": False, "error": "No remaining qty to exit"}), 400

    log.info("[API] exit/%s  sym=%s  rem_qty=%d  exit_qty=%d  partial=%s",
             order_id, sym, rem_qty, exit_qty, is_partial)

    # ── Place MARKET SELL ─────────────────────────────────────────────────────
    sell = broker.place_sell_market_order(sym, token, exit_qty)
    log.info("[API] sell response: %s", sell)

    if not is_partial:
        # Full exit
        close_trade(order_id, "MANUAL_CANCEL")
        log.info("[API] trade %s CLOSED (full manual exit)", order_id)
        return jsonify({
            "success":   True,
            "exit_type": "full",
            "exit_qty":  exit_qty,
            "sell_order": sell,
        })

    # Partial exit — use record_partial_booking for clean accounting
    ltp = 0.0
    try:
        ltp = broker.get_ltp_with_retry("NSE", sym, token, retries=2) or 0.0
    except Exception:
        pass

    sell_price    = ltp or float(trade["Entry_Price"])
    sell_order_id = sell.get("order_id", "")

    updated = record_partial_booking(order_id, exit_qty, sell_price, sell_order_id)
    log.info("[API] trade %s partial exit done  rem=%s",
             order_id, updated.get("Remaining_Qty") if updated else "?")

    return jsonify({
        "success":       True,
        "exit_type":     "partial",
        "exit_qty":      exit_qty,
        "remaining_qty": get_remaining_qty(updated) if updated else (rem_qty - exit_qty),
        "sell_order":    sell,
    })


# ── Auto-buy control ──────────────────────────────────────────────────────────
@app.post("/api/auto_buy_toggle")
def api_auto_buy_toggle():
    enabled = bool((request.json or {}).get("enabled", False))
    get_monitor().auto_buy_enabled = enabled
    log.info("[API] Auto-buy %s", "ENABLED" if enabled else "DISABLED")
    return jsonify({"success": True, "auto_buy_enabled": enabled})


@app.get("/api/auto_buy_status")
def api_auto_buy_status():
    mon   = get_monitor()
    eng   = get_engine()
    sched = get_scheduler()
    return jsonify({
        "success":           True,
        "auto_buy_enabled":  mon.auto_buy_enabled,
        "breakouts_live":    mon.status.get("breakouts", []),
        "last_trade":        mon.status.get("last_trade"),
        "active_trades":     eng.status.get("active_trades", 0),
        "trailing_last_run": eng.status.get("last_run"),
        "sl_pending":        sched.status.get("pending_count", 0),
        "scheduler_last_run":sched.status.get("last_run"),
    })


# ── P&L ───────────────────────────────────────────────────────────────────────
@app.get("/api/pnl")
def api_pnl():
    try:
        broker = get_broker()
        return jsonify({
            "success":           True,
            "pnl":               broker.get_today_pnl(),
            "available_balance": broker.get_funds().get("available_balance", 0),
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ── Symbol search ─────────────────────────────────────────────────────────────
@app.get("/api/search_symbol")
def api_search_symbol():
    q = request.args.get("q", "").upper().strip()
    if len(q) < 2:
        return jsonify({"success": False, "error": "q must be >= 2 chars"}), 400
    broker  = get_broker()
    results = [
        {"symbol": k, "token": v["token"], "margin": v["margin"]}
        for k, v in broker.token_map.items()
        if q in k
    ][:15]
    return jsonify({"success": True, "data": results})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
