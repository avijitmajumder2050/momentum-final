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
ORDER_TIMEOUT = 1800


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _divider(label: str = "") -> None:
    """Emit a visible section divider to the log."""
    if label:
        log.info("[Executor] ── %s %s", label, "─" * max(0, 48 - len(label)))
    else:
        log.info("[Executor] %s", "─" * 52)
# MAIN EXECUTION
# ─────────────────────────────────────────────
def execute_trade(
    broker,
    symbol: str,
    angel_token: str,
    entry_price: float,
    sl_price: float,
    target_price: float,
    is_auto: bool = True, 
) -> dict:

    
     # ── TRADE REQUEST ─────────────────────────────────────────────────────
    _divider("TRADE REQUEST")
    log.info("[Executor]   symbol       = %s", symbol)
    log.info("[Executor]   angel_token  = %s", angel_token)
    log.info("[Executor]   entry_price  = %.2f", entry_price)
    log.info("[Executor]   sl_price     = %.2f", sl_price)
    log.info("[Executor]   target_price = %.2f", target_price)
    log.info("[Executor]   risk_reward  = 1 : %.2f",
             (target_price - entry_price) / max(entry_price - sl_price, 0.01))
    log.info("[Executor]   mode         = %s", "AUTO" if is_auto else "MANUAL")
    log.info(
        "[Executor] %s entry=%.2f sl=%.2f tgt=%.2f auto=%s",
        symbol, entry_price, sl_price, target_price, is_auto
    )

    # ── Rule: one trade per day ─────────────────────────────
    if  is_auto and already_traded_today():
         log.warning("[Executor]   BLOCKED — auto trade already taken today")
         return {
        "success": False,
        "error": "Auto trade already taken today"
    }

    # ── Funds check ─────────────────────────────────────────
    _divider("FUNDS CHECK")
    funds = broker.get_funds()
    balance = funds.get("available_balance", 0)
    log.info("[Executor]   funds_response   = %s", funds)
    log.info("[Executor]   available_balance = ₹%.2f", balance)

    if balance <= 0:
        log.warning("[Executor]   BLOCKED — zero balance")
        return {"success": False, "error": "Zero balance"}

    margin = broker.get_margin(symbol)
    log.info("[Executor]   margin_leverage  = %.2f×", margin)

    # ── Position sizing ───────────────────────────────────────────────────
    _divider("POSITION SIZING")
    sizing = calculate(balance, entry_price, sl_price, margin)
    if not sizing["success"]:
        log.warning("[Executor]   SIZING FAILED — %s", sizing["message"])
        return {"success": False, "error": sizing["message"]}

    qty = sizing["quantity"]
    log.info("[Executor] %s qty=%d risk=₹%.2f leverage=%.1f×",
             symbol, qty, sizing["risk_per_trade"], margin)
    
    _divider("LIMIT EXECUTION LOGIC")

    ltp = broker.get_ltp_with_retry("NSE", symbol, angel_token)
    if not ltp or ltp <= 0:
        log.warning("[Executor] LTP unavailable → using entry_price fallback")
        ltp = entry_price

    buffer = max(1, round(entry_price * 0.001, 2))   # dynamic buffer

    # 🔥 KEY LOGIC
    limit_price = max(entry_price, ltp + buffer)

    log.info("[Executor]   LTP            = %.2f", ltp)
    log.info("[Executor]   entry_price    = %.2f", entry_price)
    log.info("[Executor]   buffer         = %.2f", buffer)
    log.info("[Executor]   final_limit    = %.2f", limit_price)

    if limit_price > ltp:
        log.info("[Executor]   ⚡ aggressive limit → instant fill expected")
    else:
        log.info("[Executor]   ⏳ passive limit → may wait")

    # ── Place BUY order ─────────────────────────────────────
    order = broker.place_limit_order(
        trading_symbol=symbol,
        token=angel_token,
        qty=qty,
        price=limit_price,
        transaction="BUY",
    )

  # ── ORDER RESPONSE ────────────────────────────────────────────────────
    _divider("PLACE ORDER")
    log.info("[Executor]   order_response = %s", order)
    if order["status"] != "success":
         log.error("[Executor]   ORDER FAILED — %s", order.get("message"))
         return {"success":False,"error":f"Order placement failed: {order.get('message')}"}

    order_id = order["order_id"]
    log.info("[Executor] Order placed order_id=%s — waiting for COMPLETE...", order_id)

    # ── Wait for COMPLETE ───────────────────────────────────
# ── Wait for COMPLETE ───────────────────────────────────
    _divider("ORDER TRACKING")
    log.info("[Executor]   order_id=%s  timeout=%ds  poll=%ds",
             order_id, ORDER_TIMEOUT, ORDER_POLL_SECS)
    status = wait_for_order_completion(broker, order_id)

    log.info("[Executor]   final_status = %s", status)
    if status != "COMPLETE":
        log.error("[Executor]   ORDER NOT COMPLETE — status=%s", status)
        return {"success": False, "error": f"Order not complete: {status}"}

    log.info("[Executor] Order COMPLETE → fetching position")

    # ── GET REAL EXECUTED POSITION ──────────────────────────
    _divider("POSITION FETCH")
    log.info("[Executor]   fetching actual fill for symbol=%s token=%s",
             symbol, angel_token)
    try:
        real_entry, real_qty = fetch_entry_from_position(
            broker, symbol, angel_token
        )
    except Exception as e:
        log.error("[Executor]   POSITION FETCH FAILED — %s", e)
        return {"success": False, "error": str(e)}

    log.info("[Executor] REAL Entry=%.2f Qty=%d", real_entry, real_qty)

    # ── Place GTT ───────────────────────────────────────────
    _divider("PLACE GTT")
    gtt_id = ""

    gtt = broker.place_gtt_order(
        trading_symbol=symbol,
        token=angel_token,
        qty=real_qty,
        entry_price=real_entry,
        sl_price=sl_price,
        target_price=target_price,
    )

    log.info("[Executor]   raw_response = %s", gtt)
    if gtt["status"] == "success":
        gtt_id = gtt.get("gtt_id", "")
        log.info("[Executor]   gtt_id       = %s", gtt_id)
    else:
        log.warning("[Executor] GTT failed (non-fatal): %s", gtt.get("message"))

    # ── Save trade ──────────────────────────────────────────
    _divider("TRADE SAVED")
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
    log.info("[Executor]   trade_row = %s", trade)
    
    if is_auto:
       mark_auto_buyed(symbol)
       log.info("[Executor]   watchlist marked AUTO_BUYED for %s", symbol)
    
    _divider("TRADE COMPLETE")
    log.info("[Executor]   symbol=%s  order_id=%s  qty=%d  entry=%.2f  gtt=%s",
             symbol, order_id, real_qty, real_entry, gtt_id or "N/A")
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

def fetch_entry_from_position(
    broker,
    symbol: str,
    token: str,
    retries: int = 8,
    delay: float = 1.5
) -> Tuple[float, int]:

    for attempt in range(1, retries + 1):

        res = broker.get_positions()   # <-- this is LIST

        log.info(
            "[Executor] positions fetch attempt=%d total=%d",
            attempt, len(res) if res else 0
        )

        if not res:
            time.sleep(delay)
            continue

        for p in res:
            try:
                sym = p.get("tradingsymbol")
                tkn = str(p.get("symboltoken"))
                qty = int(p.get("netqty", 0))

                # 🔍 debug visibility
                log.debug(
                    "[Executor] checking → sym=%s token=%s qty=%d",
                    sym, tkn, qty
                )

                # ✅ Match + only ACTIVE BUY position
                if (
                    (tkn == str(token) or sym == symbol)
                    and qty > 0
                ):
                    entry = float(
                        p.get("buyavgprice") or
                        p.get("avgnetprice") or 0
                    )

                    if entry > 0:
                        log.info(
                            "[Executor] MATCH FOUND → %s qty=%d entry=%.2f",
                            sym, qty, entry
                        )
                        return entry, qty

            except Exception as e:
                log.warning("[Executor] position parse error: %s", e)

        log.warning("[Executor] no match yet → retrying...")
        time.sleep(delay)

    raise Exception(f"Position not found for {symbol}")