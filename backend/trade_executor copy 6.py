"""
trade_executor.py
=================
Orchestrates the full trade entry flow:

1. Validate one-trade-per-day rule.
2. Calculate position size.
3. Place LIMIT BUY order.
4. Poll order status until COMPLETE or timeout (30s).
5. ONLY if COMPLETE → fetch REAL position (entry price + qty)
6. ONLY if COMPLETE → place GTT (target + SL).
7. ONLY if COMPLETE → write row to trade CSV.
8. Mark watchlist symbol as AUTO_BUYED.
"""

import time
import logging
from typing import Tuple

from position_sizing import calculate
from trade_s3 import open_trade, already_traded_today
from watchlist_s3 import mark_auto_buyed

log = logging.getLogger(__name__)

ORDER_POLL_SECS = 2
ORDER_TIMEOUT = 30


# ─────────────────────────────────────────────
# MAIN EXECUTION
# ─────────────────────────────────────────────
def execute_trade(
    broker,
    symbol: str,
    angel_token: str,
    entry_price: float,
    sl_price: float,
    target_price: float,
    is_auto: bool = False,
) -> dict:

    log.info(
        "[Executor] %s entry=%.2f sl=%.2f tgt=%.2f auto=%s",
        symbol, entry_price, sl_price, target_price, is_auto
    )

    # ── Rule: one trade per day ─────────────────────────────
    if  is_auto and already_traded_today():
         return {
        "success": False,
        "error": "Auto trade already taken today"
    }

    # ── Funds check ─────────────────────────────────────────
    funds = broker.get_funds()
    balance = funds.get("available_balance", 0)

    if balance <= 0:
        return {"success": False, "error": "Zero balance"}

    margin = broker.get_margin(symbol)

    sizing = calculate(balance, entry_price, sl_price, margin)
    if not sizing["success"]:
        return {"success": False, "error": sizing["message"]}

    qty = sizing["quantity"]
    log.info("[Executor] %s qty=%d risk=₹%.2f leverage=%.1f×",
             symbol, qty, sizing["risk_per_trade"], margin)

    # ── Place BUY order ─────────────────────────────────────
    order = broker.place_limit_order(
        trading_symbol=symbol,
        token=angel_token,
        qty=qty,
        price=entry_price,
        transaction="BUY",
    )

    if order["status"] != "success":
         return {"success":False,"error":f"Order placement failed: {order.get('message')}"}

    order_id = order["order_id"]
    log.info("[Executor] Order placed order_id=%s — waiting for COMPLETE...", order_id)

    # ── Wait for COMPLETE ───────────────────────────────────
    status = wait_for_order_completion(broker, order_id)

    if status != "COMPLETE":
        return {"success": False, "error": f"Order not complete: {status}"}

    log.info("[Executor] Order COMPLETE → fetching position")

    # ── GET REAL EXECUTED POSITION ──────────────────────────
    try:
        real_entry, real_qty = fetch_entry_from_position(
            broker, symbol, angel_token
        )
    except Exception as e:
        return {"success": False, "error": str(e)}

    log.info("[Executor] REAL Entry=%.2f Qty=%d", real_entry, real_qty)

    # ── Place GTT ───────────────────────────────────────────
    gtt_id = ""

    gtt = broker.place_gtt_order(
        trading_symbol=symbol,
        token=angel_token,
        qty=real_qty,
        entry_price=real_entry,
        sl_price=sl_price,
        target_price=target_price,
    )

    if gtt["status"] == "success":
        gtt_id = gtt.get("gtt_id", "")
    else:
        log.warning("[Executor] GTT failed (non-fatal): %s", gtt.get("message"))

    # ── Save trade ──────────────────────────────────────────
    trade = open_trade(
        order_id=order_id,
        symbol=symbol,
        angel_token=angel_token,
        entry_price=real_entry,
        sl_price=sl_price,
        target_price=target_price,
        qty=real_qty,
        gtt_id=gtt_id,
    )

    
    if is_auto:
       mark_auto_buyed(symbol)

    return {
        "success": True,
        "order_id": order_id,
        "trade": trade
    }


# ─────────────────────────────────────────────
# ORDER POLLING
# ─────────────────────────────────────────────
def wait_for_order_completion(broker, order_id: str) -> str:

    elapsed = 0
    while elapsed < ORDER_TIMEOUT:
        time.sleep(ORDER_POLL_SECS)
        elapsed += ORDER_POLL_SECS

        status = broker.get_order_status(order_id)

        log.debug("[Order] %s → %s", order_id, status)

        if status in ("COMPLETE", "REJECTED", "CANCELLED"):
            return status

    return "TIMEOUT"


# ─────────────────────────────────────────────
# POSITION FETCH (REAL ENTRY PRICE)
# ─────────────────────────────────────────────
def fetch_entry_from_position(
    broker,
    symbol: str,
    token: str,
    retries: int = 8,
    delay: float = 1.5
) -> Tuple[float, int]:

    for _ in range(retries):

        res = broker.get_positions()

        if res and res.get("status") is True:

            for p in res.get("data", []):

                if (
                    p.get("symboltoken") == str(token)
                    or p.get("tradingsymbol") == symbol
                ):

                    qty = int(p.get("netqty", 0))
                    if qty == 0:
                        continue

                    entry = float(
                        p.get("buyavgprice") or p.get("avgnetprice")
                    )

                    return entry, qty

        time.sleep(delay)

    raise Exception(f"Position not found for {symbol}")