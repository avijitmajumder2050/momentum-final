"""
trailing_engine.py
==================
Continuous trailing stop-loss engine.

Run cycle (every TRAIL_POLL_SECS seconds)
-----------------------------------------
For each ACTIVE trade:

  1. Fetch live LTP from Angel API (never from CSV).

  2. PHASE 1 — BREAKEVEN  (fires once)
     Condition : LTP >= entry * (1 + BREAKEVEN_PCT)   default +1%
     Action    : Move SL to entry price
     SubAction : BREAKEVEN

  3. PHASE 2 — TRAILING   (fires repeatedly)
     step          = max(1, round(ltp * TRAIL_STEP_PCT))  default 0.2%
     candidate_sl  = ltp - step
     Condition : candidate_sl > Last_SL (only move UP, never down)
     Action    : Modify GTT SL via Angel API → update Last_SL in CSV
     SubAction : TRAILING

  4. TARGET CHECK
     Condition : LTP >= Target_Price
     Action    : close_trade (TARGET_HIT)

  5. SL CHECK
     Condition : LTP <= Last_SL
     Action    : close_trade (SL_HIT)

  6. TIME EXIT
     Condition : Entry_Time + TIME_EXIT_MINS elapsed AND Status == ACTIVE
     Action    : cancel GTT, place market SELL, close_trade (TIME_EXIT)

Idempotency
-----------
- SL only moves UP — a lower candidate_sl is always ignored.
- Trailing flag is set True in CSV so restart-safe.
- All operations are wrapped in try/except per trade — one bad symbol
  never blocks others.

SubAction values written to CSV
---------------------------------
ENTRY / BREAKEVEN / TRAILING / TARGET_HIT / SL_HIT /
MANUAL_CANCEL / TIME_EXIT
"""

import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

from trade_s3 import load_active, update_trailing_sl, close_trade, update_trade

log             = logging.getLogger(__name__)
TRAIL_POLL_SECS = int(os.getenv("TRAIL_POLL_SECS", "10"))
BREAKEVEN_PCT   = float(os.getenv("BREAKEVEN_PCT", "0.01"))
TRAIL_STEP_PCT  = float(os.getenv("TRAIL_STEP_PCT", "0.002"))
TIME_EXIT_MINS  = int(os.getenv("TIME_EXIT_MINS", "10"))


def calc_step(ltp: float) -> float:
    return max(1.0, round(ltp * TRAIL_STEP_PCT, 2))


def _ist_now() -> datetime:
    import pytz
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def _market_open() -> bool:
    n = _ist_now()
    mins = n.hour * 60 + n.minute
    return n.weekday() < 5 and 555 <= mins <= 930


# ─────────────────────────────────────────────
# TRADE PROCESS
# ─────────────────────────────────────────────
def process_trade(broker, trade: dict) -> None:

    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]
    token    = trade["Angel_Token"]

    try:
        ltp = broker.get_ltp("NSE", symbol, token)
    except Exception as e:
        log.warning("[Trail] LTP failed %s: %s", symbol, e)
        return

    try:
        entry    = float(trade["Entry_Price"])
        target   = float(trade["Target_Price"])
        last_sl  = float(trade["Last_SL"] or trade["SL_Price"])
        qty      = int(trade["Qty"])
        gtt_id   = trade.get("GTT_ID", "")
    except Exception as e:
        log.error("[Trail] Parse error %s: %s", symbol, e)
        return

    # ── TIME EXIT ─────────────────────────────────────────────
    try:
        entry_time = datetime.strptime(trade["Entry_Time"], "%Y-%m-%d %H:%M:%S")

        if datetime.now() >= entry_time + timedelta(minutes=TIME_EXIT_MINS):

            log.info("[Trail] TIME EXIT %s", symbol)

            if gtt_id:
                broker.cancel_gtt(gtt_id, symbol, token)

            broker.place_sell_market_order(symbol, token, qty)
            close_trade(order_id, "TIME_EXIT")
            return

    except Exception:
        pass

    # ── TARGET HIT ───────────────────────────────────────────
    if ltp >= target:
        log.info("[Trail] TARGET HIT %s", symbol)
        close_trade(order_id, "TARGET_HIT")
        update_trade(order_id, {"SubAction": "TARGET_HIT"})
        return

    # ── SL HIT ───────────────────────────────────────────────
    if ltp <= last_sl:
        log.info("[Trail] SL HIT %s", symbol)
        close_trade(order_id, "SL_HIT")
        update_trade(order_id, {"SubAction": "SL_HIT"})
        return

    # ── BREAKEVEN ────────────────────────────────────────────
    if ltp >= entry * (1 + BREAKEVEN_PCT) and last_sl < entry:

        new_sl = round(entry, 2)
        log.info("[Trail] BREAKEVEN %s → %.2f", symbol, new_sl)

        _apply_sl_update(
            broker, order_id, symbol, token,
            qty, gtt_id, new_sl, "BREAKEVEN", target
        )
        return

    # ── TRAILING ─────────────────────────────────────────────
    step = calc_step(ltp)
    candidate_sl = round(ltp - step, 2)

    if candidate_sl > last_sl:

        log.info("[Trail] TRAIL %s %.2f → %.2f",
                 symbol, last_sl, candidate_sl)

        _apply_sl_update(
            broker, order_id, symbol, token,
            qty, gtt_id, candidate_sl, "TRAILING", target
        )


# ─────────────────────────────────────────────
# SL UPDATE
# ─────────────────────────────────────────────
def _apply_sl_update(
    broker,
    order_id: str,
    symbol: str,
    token: str,
    qty: int,
    gtt_id: str,
    new_sl: float,
    sub_action: str,
    target_price: float,
) -> None:

    if gtt_id:
        res = broker.modify_gtt_sl(
            gtt_id,
            symbol,
            token,
            qty,
            new_sl,
            target_price
        )

        if res["status"] != "success":
            log.warning("[Trail] GTT modify failed %s: %s",
                        symbol, res.get("message"))
    else:
        log.warning("[Trail] No GTT_ID for %s", symbol)

    update_trailing_sl(order_id, new_sl)
    update_trade(order_id, {
        "SubAction": sub_action,
        "Trailing_Active": "True"
    })


# ─────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────
class TrailingEngine:

    def __init__(self):
        self.running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self.running:
            return

        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True
        )
        self._thread.start()

        log.info("[Trail] Engine started")

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._run_cycle()
            except Exception as e:
                log.error("[Trail] Cycle error: %s", e, exc_info=True)

            time.sleep(TRAIL_POLL_SECS)

    def _run_cycle(self):
        if not _market_open():
            return

        from angel_broker import get_broker
        broker = get_broker()

        trades = load_active()

        for trade in trades:
            try:
                process_trade(broker, trade)
            except Exception as e:
                log.error("[Trail] Trade error %s: %s",
                          trade.get("Symbol"), e)


_engine = TrailingEngine()

def get_engine():
    return _engine

def start_engine():
    _engine.start()