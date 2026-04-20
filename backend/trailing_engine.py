"""
trailing_engine.py
==================
Production-grade continuous trailing stop-loss engine for Angel One.

Logic Sequence:
1. PHASE 1: BREAKEVEN - If LTP >= Entry * 1.03, move SL to Entry.
2. PHASE 2: LOOSE TRAILING - Formula: SL = Entry + (floor(PriceMove/Step) - 2) * Step.
"""

import os
import time
import logging
import threading
import math
from datetime import datetime, timedelta
from typing import Optional

# Assumption: trade_s3 handles local/cloud persistence for restart-safety
from trade_s3 import load_active, update_trailing_sl, close_trade, update_trade

log             = logging.getLogger(__name__)
TRAIL_POLL_SECS = int(os.getenv("TRAIL_POLL_SECS", "10"))
BREAKEVEN_PCT   = 0.03  # Fixed at 3% as per requirements
TIME_EXIT_MINS  = int(os.getenv("TIME_EXIT_MINS", "10"))

def get_dynamic_step(ltp: float) -> int:
    """Requirement 5: Ladder-based step logic."""
    if ltp < 200:
        return 1
    elif 200 <= ltp < 500:
        return 2
    else:
        return 5

def _ist_now() -> datetime:
    import pytz
    return datetime.now(pytz.timezone("Asia/Kolkata"))

def _market_open() -> bool:
    n = _ist_now()
    mins = n.hour * 60 + n.minute
    return n.weekday() < 5 and 555 <= mins <= 930

# ─────────────────────────────────────────────
# TRADE PROCESS LOGIC
# ─────────────────────────────────────────────
def process_trade(broker, trade: dict) -> None:
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]
    token    = trade["Angel_Token"]

    # 1. Fetch live LTP (Requirement 2)
    try:
        ltp_resp = broker.get_ltp("NSE", symbol, token)
        # Handle dict or float response from wrapper
        ltp = float(ltp_resp) if isinstance(ltp_resp, (int, float)) else float(ltp_resp['data']['ltp'])
    except Exception as e:
        log.warning("[Trail] LTP failed %s: %s", symbol, e)
        return

    try:
        entry    = float(trade["Entry_Price"])
        target   = float(trade["Target_Price"])
        last_sl  = float(trade["Last_SL"] or trade["SL_Price"])
        qty      = int(trade["Qty"])
        gtt_id   = trade.get("GTT_ID", "")
        # Restart-safe flag to check if Breakeven phase is complete
        be_done  = str(trade.get("Trailing_Active", "False")).lower() == "true"
    except Exception as e:
        log.error("[Trail] Parse error %s: %s", symbol, e)
        return

    # ── TIME EXIT CHECK ───────────────────────────────────────
    try:
        entry_time = datetime.strptime(trade["Entry_Time"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() >= entry_time + timedelta(minutes=TIME_EXIT_MINS):
            log.info("[Trail] TIME EXIT %s", symbol)
            if gtt_id: broker.cancel_gtt(gtt_id, symbol, token)
            broker.place_sell_market_order(symbol, token, qty)
            close_trade(order_id, "TIME_EXIT")
            return
    except Exception: pass

    # ── TARGET CHECK ─────────────────────────────────────────
    if ltp >= target:
        log.info("[Trail] TARGET HIT %s", symbol)
        close_trade(order_id, "TARGET_HIT")
        update_trade(order_id, {"SubAction": "TARGET_HIT"})
        return

    # ── SL CHECK ─────────────────────────────────────────────
    if ltp <= last_sl:
        log.info("[Trail] SL HIT %s at %.2f", symbol, ltp)
        close_trade(order_id, "SL_HIT")
        update_trade(order_id, {"SubAction": "SL_HIT"})
        return

    # ── PHASE 1: BREAKEVEN (Triggered at 3% Profit) ──────────
    if not be_done:
        if ltp >= entry * (1 + BREAKEVEN_PCT):
            new_sl = round(entry, 2)
            log.info("[Trail] BREAKEVEN %s -> SL set to Entry: %.2f", symbol, new_sl)
            _apply_sl_update(broker, trade, new_sl, "BREAKEVEN")
        return # Transition to trailing phase in next cycle

    # ── PHASE 2: LOOSE MOMENTUM TRAILING ─────────────────────
    step = get_dynamic_step(ltp)
    price_move = ltp - entry
    steps_moved = math.floor(price_move / step)

    # Formula: Entry + (StepsMoved - 2) * Step
    # This provides the "Loose" cushion for momentum
    candidate_sl = entry + (steps_moved - 2) * step
    candidate_sl = round(float(candidate_sl), 2)

    # Idempotency: Only move UP. If candidate is below Entry (Step 1), ignore.
    if candidate_sl > last_sl:
        log.info("[Trail] LADDER SHIFT %s: SL %.2f -> %.2f", symbol, last_sl, candidate_sl)
        _apply_sl_update(broker, trade, candidate_sl, "TRAILING")

# ─────────────────────────────────────────────
# BROKER INTEGRATION
# ─────────────────────────────────────────────
def _apply_sl_update(broker, trade, new_sl: float, sub_action: str) -> None:
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]

    if trade.get("GTT_ID"):
        res = broker.modify_gtt_sl(
            trade["GTT_ID"],
            symbol,
            trade["Angel_Token"],
            trade["Qty"],
            new_sl,
            trade["Target_Price"]
        )
        
        # Handle Angel API returning dict or failure response
        if isinstance(res, dict) and res.get("status") != "success":
            log.warning("[Trail] GTT modify failed %s: %s", symbol, res.get("message"))
            return
    else:
        log.warning("[Trail] No GTT_ID for %s", symbol)
        return

    # Update persistence to ensure restart-safety
    update_trailing_sl(order_id, new_sl)
    update_trade(order_id, {
        "SubAction": sub_action,
        "Trailing_Active": "True",  # Sets flag for Phase 2
        "Last_SL": new_sl
    })

# ─────────────────────────────────────────────
# ENGINE CORE
# ─────────────────────────────────────────────
class TrailingEngine:
    def __init__(self):
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.status = {"active_trades": 0, "last_run": None}

    def start(self):
        if self.running: return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("[Trail] Engine started")

    def _loop(self):
        while self.running:
            try:
                if _market_open():
                    self._run_cycle()
            except Exception as e:
                log.error("[Trail] Engine loop error: %s", e, exc_info=True)
            time.sleep(TRAIL_POLL_SECS)

    def _run_cycle(self):
        from angel_broker import get_broker
        broker = get_broker()
        trades = load_active()
        
        self.status["active_trades"] = len(trades)
        self.status["last_run"] = datetime.now().strftime("%H:%M:%S")

        for trade in trades:
            try:
                process_trade(broker, trade)
            except Exception as e:
                log.error("[Trail] Trade error %s: %s", trade.get("Symbol"), e)

_engine = TrailingEngine()
def get_engine(): return _engine
def start_engine(): _engine.start()