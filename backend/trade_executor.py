"""
trade_executor.py
=================
Orchestrates the full trade entry flow:

1. Validate one-trade-per-day rule.
2. Calculate position size.
3. Place LIMIT BUY order.
4. Poll order status until COMPLETE or timeout (30s).
5. ONLY if COMPLETE → place GTT (target + SL).
6. ONLY if COMPLETE → write row to trade CSV.
7. Mark watchlist symbol as AUTO_BUYED.

This is the single entry-point for both auto and manual trades.
"""
import time
import logging
from typing import Optional

from position_sizing import calculate
from trade_s3        import open_trade, already_traded_today
from watchlist_s3    import mark_auto_buyed

log             = logging.getLogger(__name__)
ORDER_POLL_SECS = 2
ORDER_TIMEOUT   = 30   # seconds to wait for COMPLETE status


def execute_trade(
    broker,
    symbol:       str,
    angel_token:  str,
    entry_price:  float,
    sl_price:     float,
    target_price: float,
    is_auto:      bool = False,
) -> dict:
    """
    Full trade entry flow.

    Returns
    -------
    {"success":True,  "order_id":"...", "trade": {...}}
    {"success":False, "error":"..."}
    """
    log.info("[Executor] %s entry=%.2f sl=%.2f tgt=%.2f auto=%s",
             symbol, entry_price, sl_price, target_price, is_auto)

    # ── Rule: one trade per day per symbol ────────────────────────────────────
    if already_traded_today(symbol):
        msg = f"Already traded {symbol} today — skipping"
        log.warning("[Executor] %s", msg)
        return {"success":False,"error":msg}

    # ── Position sizing ───────────────────────────────────────────────────────
    funds   = broker.get_funds()
    balance = funds.get("available_balance", 0)
    if balance <= 0:
        return {"success":False,"error":"Zero available balance"}

    margin   = broker.get_margin(symbol)
    sizing   = calculate(balance, entry_price, sl_price, margin)
    if not sizing["success"]:
        return {"success":False,"error":sizing["message"]}

    qty = sizing["quantity"]
    log.info("[Executor] %s qty=%d risk=₹%.2f leverage=%.1f×",
             symbol, qty, sizing["risk_per_trade"], margin)

    # ── Place LIMIT BUY order ─────────────────────────────────────────────────
    order = broker.place_limit_order(
        trading_symbol = symbol,
        token          = angel_token,
        qty            = qty,
        price          = entry_price,
        transaction    = "BUY",
    )
    if order["status"] != "success":
        return {"success":False,"error":f"Order placement failed: {order.get('message')}"}

    order_id = order["order_id"]
    log.info("[Executor] Order placed order_id=%s — waiting for COMPLETE...", order_id)

    # ── Poll until COMPLETE or timeout ────────────────────────────────────────
    elapsed  = 0
    status   = "PENDING"
    while elapsed < ORDER_TIMEOUT:
        time.sleep(ORDER_POLL_SECS)
        elapsed += ORDER_POLL_SECS
        status   = broker.get_order_status(order_id)
        log.debug("[Executor] %s status=%s elapsed=%ds", order_id, status, elapsed)
        if status == "COMPLETE":
            break
        if status in ("REJECTED","CANCELLED"):
            return {"success":False,
                    "error":f"Order {order_id} was {status} — not storing trade"}

    if status != "COMPLETE":
        return {"success":False,
                "error":f"Order {order_id} not COMPLETE after {ORDER_TIMEOUT}s (status={status})"}

    log.info("[Executor] Order COMPLETE — placing GTT and saving trade")

    # ── Place GTT (target + SL) ───────────────────────────────────────────────
    gtt_id = ""
   
    gtt = broker.place_gtt_order(
        trading_symbol = symbol,
        token          = angel_token,
        qty            = qty,
        entry_price    = entry_price,
        sl_price       = sl_price,
        target_price   = target_price,
    )
    if gtt["status"] == "success":
        gtt_id = gtt.get("gtt_id","")
        
    else:
        log.warning("[Executor] GTT failed (non-fatal): %s", gtt.get("message"))

    # ── Write to trade CSV (ONLY because order is COMPLETE) ──────────────────
    trade = open_trade(
        order_id      = order_id,
        symbol        = symbol,
        angel_token   = angel_token,
        entry_price   = entry_price,
        sl_price      = sl_price,
        target_price  = target_price,
        qty           = qty,
        gtt_id = gtt_id,
        
    )

    # ── Mark watchlist symbol ─────────────────────────────────────────────────
    mark_auto_buyed(symbol)

    return {"success":True,"order_id":order_id,"trade":trade}
