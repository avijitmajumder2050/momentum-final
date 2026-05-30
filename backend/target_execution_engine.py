"""
target_execution_engine.py
===========================
Handles ALL target execution for Angel One trades.

Rules
-----
SAME-DAY target (entry_date == today):
  → MARKET SELL 50% remaining qty
  → trade stays ACTIVE
  → trailing continues on remaining 50%
  → partial booking fires ONLY ONCE per trade

NEXT-DAY target (entry_date != today):
  → MARKET SELL 100% remaining qty
  → trade CLOSED

GTT is NOT used. This engine places direct market sell orders.

Called from trailing_engine.process_trade() when ltp >= target.
"""

import logging
import math
from datetime import datetime

from trade_s3 import (
    record_partial_booking,
    close_trade,
    update_trade,
    get_remaining_qty,
)
from exit_utils import is_same_day_trade, divider

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────
def process_target(broker, trade: dict, ltp: float) -> None:
    """
    Called when ltp >= target_price.
    Routes to same-day or next-day logic based on entry date.
    Idempotent: safe to call multiple times in same cycle.
    """
    order_id   = trade["Order_ID"]
    symbol     = trade["Symbol"]
    token      = trade["Angel_Token"]
    entry_date = trade.get("Entry_Date", "")
    target     = float(trade["Target_Price"])
    rem_qty    = get_remaining_qty(trade)

    divider(f"TARGET HIT {symbol}")
    log.info(
        "[Target] %s  ltp=%.2f  target=%.2f  rem_qty=%d  entry_date=%s",
        symbol, ltp, target, rem_qty, entry_date,
    )

    # Guard: nothing left to sell
    if rem_qty <= 0:
        log.warning("[Target] %s  rem_qty=0 — nothing to sell", symbol)
        return

    # Already partially booked — check if we should now do full close
    already_partial = str(trade.get("Partial_Booked", "False")).lower() == "true"

    if is_same_day_trade(entry_date):
        _handle_same_day_target(broker, trade, ltp, rem_qty, already_partial)
    else:
        _handle_next_day_target(broker, trade, ltp, rem_qty)


# ─────────────────────────────────────────────
# SAME-DAY TARGET (ENTRY DATE == TODAY)
# ─────────────────────────────────────────────
def _handle_same_day_target(
    broker,
    trade:           dict,
    ltp:             float,
    rem_qty:         int,
    already_partial: bool,
) -> None:
    """
    Same-day: sell exactly 50% once.
    Trade NEVER fully closes on same-day first target hit.
    Trailing continues on remaining half.
    """
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]
    token    = trade["Angel_Token"]

    # ── Idempotency: only one partial booking per trade ───────────────────────
    if already_partial:
        log.info(
            "[Target] %s  same-day target hit again — "
            "partial already done, trailing continues (no action)",
            symbol,
        )
        return

    # ── Calculate sell qty: floor(50%) — never sell 0 ────────────────────────
    sell_qty = max(1, math.floor(rem_qty * 0.5))

    log.info(
        "[Target] SAME-DAY PARTIAL  %s  rem_qty=%d  sell_qty=%d  price=%.2f",
        symbol, rem_qty, sell_qty, ltp,
    )

    # ── Place MARKET SELL ─────────────────────────────────────────────────────
    sell_result = broker.place_sell_market_order(
        trading_symbol=symbol,
        token=token,
        qty=sell_qty,
    )

    log.info("[Target]   sell_order_response = %s", sell_result)

    if sell_result.get("status") != "success":
        log.error(
            "[Target] SELL FAILED %s  qty=%d  err=%s",
            symbol, sell_qty, sell_result.get("message"),
        )
        return

    sell_order_id = sell_result.get("order_id", "")

    # ── Record partial booking in S3 ──────────────────────────────────────────
    updated = record_partial_booking(
        order_id      = order_id,
        sold_qty      = sell_qty,
        sell_price    = ltp,
        sell_order_id = sell_order_id,
    )

    log.info(
        "[Target] PARTIAL BOOK DONE  %s  sold=%d  rem=%s  sell_order=%s",
        symbol, sell_qty,
        updated.get("Remaining_Qty") if updated else "?",
        sell_order_id,
    )

    # Trade stays ACTIVE — trailing engine will continue on remaining qty


# ─────────────────────────────────────────────
# NEXT-DAY TARGET (ENTRY DATE != TODAY)
# ─────────────────────────────────────────────
def _handle_next_day_target(
    broker,
    trade:   dict,
    ltp:     float,
    rem_qty: int,
) -> None:
    """
    Overnight trade hit target — sell ALL remaining qty and close.
    """
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]
    token    = trade["Angel_Token"]

    log.info(
        "[Target] NEXT-DAY FULL EXIT  %s  rem_qty=%d  price=%.2f",
        symbol, rem_qty, ltp,
    )

    # ── Place MARKET SELL for full remaining qty ──────────────────────────────
    sell_result = broker.place_sell_market_order(
        trading_symbol=symbol,
        token=token,
        qty=rem_qty,
    )

    log.info("[Target]   sell_order_response = %s", sell_result)

    if sell_result.get("status") != "success":
        log.error(
            "[Target] FULL SELL FAILED %s  qty=%d  err=%s",
            symbol, rem_qty, sell_result.get("message"),
        )
        return

    sell_order_id = sell_result.get("order_id", "")

    # ── Close trade in S3 ────────────────────────────────────────────────────
    update_trade(order_id, {
        "Last_Target_Action": "FULL_SOLD",
        "Last_Target_Time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "SubAction":          "TARGET_HIT",
    })
    close_trade(order_id, "TARGET_HIT")

    log.info(
        "[Target] TRADE CLOSED  %s  sell_order=%s  qty=%d  price=%.2f",
        symbol, sell_order_id, rem_qty, ltp,
    )
