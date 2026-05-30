"""
trailing_engine.py
==================
Production-grade continuous trailing stop-loss engine for Angel One.

Architecture (v2 — GTT-free, next-day SL)
------------------------------------------
ALL trailing CALCULATIONS are unchanged:
  Phase 1  : Breakeven at +3%
  Phase 2  : Ladder trailing  (dynamic step by price tier)

EXIT BEHAVIOR CHANGES:
  SL breach    → mark_sl_pending()  (NO immediate sell)
  Target hit   → target_execution_engine handles this

The engine continues polling while Status in (ACTIVE, SL_TRIGGER_PENDING).
"""

import os
import time
import logging
import threading
import math
from datetime import datetime
from typing import Optional

from trade_s3 import (
    load_active,
    update_trailing_sl,
    close_trade,
    update_trade,
    mark_sl_pending,
    get_remaining_qty,
)
from exit_utils import get_next_trading_day

log              = logging.getLogger(__name__)
TRAIL_POLL_SECS  = int(os.getenv("TRAIL_POLL_SECS", "10"))
BREAKEVEN_PCT    = 0.03   # 3% — fixed per requirement


# ─────────────────────────────────────────────
# HELPERS  (UNCHANGED — do not touch)
# ─────────────────────────────────────────────
def get_dynamic_step(ltp: float) -> int:
    """Ladder-based step logic (Requirement 5 — unchanged)."""
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
    n    = _ist_now()
    mins = n.hour * 60 + n.minute
    return n.weekday() < 5 and 555 <= mins <= 930


# ─────────────────────────────────────────────
# CORE TRADE PROCESSOR
# ─────────────────────────────────────────────
def process_trade(broker, trade: dict) -> None:
    """
    Process a single active trade for one poll cycle.

    SL management  : next-day confirmation (no immediate exit)
    Target detection: delegated to target_execution_engine.process_target()
    Trailing calc  : UNCHANGED Phase 1 + Phase 2
    """
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]
    token    = trade["Angel_Token"]

    # ── 1. Fetch live LTP ─────────────────────────────────────────────────────
    try:
        ltp_resp = broker.get_ltp("NSE", symbol, token)
        ltp = (
            float(ltp_resp)
            if isinstance(ltp_resp, (int, float))
            else float(ltp_resp["data"]["ltp"])
        )
    except Exception as e:
        log.warning("[Trail] LTP failed %s: %s", symbol, e)
        return

    # ── 2. Parse trade fields ─────────────────────────────────────────────────
    try:
        entry   = float(trade["Entry_Price"])
        target  = float(trade["Target_Price"])
        last_sl = float(trade["Last_SL"] or trade["SL_Price"])
        qty     = get_remaining_qty(trade)
        be_done = str(trade.get("Trailing_Active", "False")).lower() == "true"
        status  = trade.get("Status", "ACTIVE")
    except Exception as e:
        log.error("[Trail] Parse error %s: %s", symbol, e)
        return

    log.debug(
        "[Trail] %s  ltp=%.2f  entry=%.2f  last_sl=%.2f  target=%.2f  "
        "qty=%d  be_done=%s  status=%s",
        symbol, ltp, entry, last_sl, target, qty, be_done, status,
    )

    # Guard: nothing to trail on zero qty
    if qty <= 0:
        log.info("[Trail] %s remaining_qty=0 — skipping", symbol)
        return

    # ── 3. TARGET CHECK — delegated to target engine ──────────────────────────
    # Import here to avoid circular imports
    from target_execution_engine import process_target
    if ltp >= target:
        log.info(
            "[Trail] TARGET ZONE detected %s  ltp=%.2f >= target=%.2f  "
            "→ delegating to target_execution_engine",
            symbol, ltp, target,
        )
        process_target(broker, trade, ltp)
        return   # target engine takes full control this cycle

    # ── 4. SL CHECK — NEXT-DAY CONFIRMATION (no immediate exit) ──────────────
    if ltp < last_sl:
        if status == "ACTIVE":
            # First breach — schedule next-day validation
            next_day = get_next_trading_day()
            log.warning(
                "[Trail] SL BREACH %s  ltp=%.2f < sl=%.2f  "
                "→ marking SL_TRIGGER_PENDING  next_validation=%s",
                symbol, ltp, last_sl, next_day,
            )
            mark_sl_pending(
                order_id        = order_id,
                pending_sl      = last_sl,
                triggered_price = ltp,
                scheduled_exit_date = next_day,
            )
        else:
            # Already pending — exit_scheduler.py handles this; just log
            log.info(
                "[Trail] %s still below SL_TRIGGER_PENDING  ltp=%.2f sl=%.2f  "
                "— exit_scheduler will validate on %s",
                symbol, ltp, last_sl,
                trade.get("Scheduled_Exit_Date", "?"),
            )
        return   # DO NOT continue trailing when breached

    # ── 5. SL RECOVERY — restore if LTP climbed back above SL ────────────────
    # If status was SL_TRIGGER_PENDING but price recovered, restore_active
    # is handled by exit_scheduler at next-day open. Nothing to do here.

    # ── 6. TRAILING CALCULATIONS (UNCHANGED) ─────────────────────────────────
    # Only runs when:  status == ACTIVE  AND  ltp >= last_sl  AND  ltp < target

    # ── Phase 1: BREAKEVEN ────────────────────────────────────────────────────
    if not be_done:
        if ltp >= entry * (1 + BREAKEVEN_PCT):
            new_sl = round(entry, 2)
            log.info(
                "[Trail] BREAKEVEN %s  ltp=%.2f >= entry*1.03=%.2f  "
                "→ SL moved to Entry %.2f",
                symbol, ltp, entry * (1 + BREAKEVEN_PCT), new_sl,
            )
            _apply_sl_update(broker, trade, new_sl, "BREAKEVEN")
        return   # stay in Phase 1 until breakeven triggers; transition next cycle

    # ── Phase 2: LADDER MOMENTUM TRAILING ────────────────────────────────────
    step        = get_dynamic_step(ltp)
    price_move  = ltp - entry
    steps_moved = math.floor(price_move / step)

    # Formula: Entry + (StepsMoved - 2) * Step  — provides momentum cushion
    candidate_sl = entry + (steps_moved - 2) * step
    candidate_sl = round(float(candidate_sl), 2)

    # Idempotency: only move SL UP, never down
    if candidate_sl > last_sl:
        log.info(
            "[Trail] LADDER SHIFT %s  step=%d  price_move=%.2f  "
            "steps=%d  SL %.2f → %.2f",
            symbol, step, price_move, steps_moved, last_sl, candidate_sl,
        )
        _apply_sl_update(broker, trade, candidate_sl, "TRAILING")


# ─────────────────────────────────────────────
# SL UPDATE  (no GTT — pure persistence)
# ─────────────────────────────────────────────
def _apply_sl_update(broker, trade: dict, new_sl: float, sub_action: str) -> None:
    """
    Persist new SL to trade_s3.

    GTT is NOT used in the new architecture — the strategy engine owns all exits.
    broker parameter retained for future hooks (e.g. notifications, alerts).
    """
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]

    # Persist new SL
    update_trailing_sl(order_id, new_sl)
    update_trade(order_id, {
        "SubAction":       sub_action,
        "Trailing_Active": "True",
        "Last_SL":         str(new_sl),
    })

    log.info(
        "[Trail] SL UPDATED %s  %.2f → %.2f  action=%s  (no GTT — engine owns exits)",
        symbol, float(trade.get("Last_SL") or 0), new_sl, sub_action,
    )


# ─────────────────────────────────────────────
# ENGINE CORE
# ─────────────────────────────────────────────
class TrailingEngine:
    """
    Background thread — polls all live trades every TRAIL_POLL_SECS seconds.
    Processes ACTIVE and SL_TRIGGER_PENDING rows (trailing must continue).
    """

    def __init__(self):
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.status  = {"active_trades": 0, "last_run": None}
        log.info("[Trail] TrailingEngine initialised  poll=%ds", TRAIL_POLL_SECS)

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="trailing-engine",
            daemon=True,
        )
        self._thread.start()
        log.info("[Trail] Engine started")

    def stop(self):
        log.info("[Trail] stop() called")
        self.running = False

    def _loop(self):
        while self.running:
            try:
                if _market_open():
                    self._run_cycle()
                else:
                    log.debug("[Trail] market closed — skipping cycle")
            except Exception as e:
                log.error("[Trail] Engine loop error: %s", e, exc_info=True)
            time.sleep(TRAIL_POLL_SECS)

    def _run_cycle(self):
        from angel_broker import get_broker
        broker = get_broker()

        # load_active() returns ACTIVE + SL_TRIGGER_PENDING
        trades = load_active()
        self.status["active_trades"] = len(trades)
        self.status["last_run"]      = datetime.now().strftime("%H:%M:%S")

        log.debug("[Trail] cycle: %d live trades", len(trades))

        for trade in trades:
            try:
                process_trade(broker, trade)
            except Exception as e:
                log.error(
                    "[Trail] Trade error %s: %s",
                    trade.get("Symbol"), e,
                    exc_info=True,
                )


# ── Singleton ─────────────────────────────────────────────────────────────────
_engine = TrailingEngine()

def get_engine()   -> TrailingEngine: return _engine
def start_engine() -> None:           _engine.start()
