"""
exit_scheduler.py
=================
Closing-basis SL + next-day confirmation engine.

Responsibilities
----------------
0. EVENING SESSION (after SL_EOD_CHECK_TIME, default 15:35 IST):
     For each ACTIVE trade, closing LTP < Last_SL → mark SL_TRIGGER_PENDING
     with Scheduled_Exit_Date = next trading day.  Intraday dips are ignored.
1. Poll every 60 seconds (configurable via EXIT_SCHEDULER_INTERVAL env var).
2. Load all SL_TRIGGER_PENDING trades.
3. After 9:20 AM on the Scheduled_Exit_Date:
     a. Fetch live LTP.
     b. IF ltp < pending_sl  → MARKET SELL remaining qty → CLOSE trade.
     c. IF ltp >= pending_sl → restore ACTIVE, clear pending state,
                               resume trailing.
4. Restart-safe: reads state from S3 on every poll.
5. Thread-safe: uses a per-order lock to prevent duplicate exits.
6. Idempotent: checks Scheduled_Exit_Date before acting.

Design notes
------------
- Never places a sell before 9:20 AM (avoids opening spike).
- If LTP fetch fails, backs off and retries next cycle (conservative).
- One MARKET SELL per pending trade — no retries on sell failure
  (prevents double-sell); logs error and leaves state for manual review.
"""

import os
import time
import logging
import threading
from datetime import datetime
from typing import Optional

from trade_s3 import (
    load_active,
    mark_sl_pending,
    load_sl_pending,
    restore_active,
    close_trade,
    update_trade,
    record_partial_booking,
    get_remaining_qty,
)
from exit_utils import (
    ist_today,
    after_920,
    after_eod_check,
    get_next_trading_day,
    divider,
    market_open,
)

log                     = logging.getLogger(__name__)
SCHEDULER_POLL_SECS     = int(os.getenv("EXIT_SCHEDULER_INTERVAL", "60"))
_order_locks: dict      = {}            # per-order_id mutex
_order_locks_mutex      = threading.Lock()


def _get_order_lock(order_id: str) -> threading.Lock:
    """Return (creating if needed) a per-order mutex."""
    with _order_locks_mutex:
        if order_id not in _order_locks:
            _order_locks[order_id] = threading.Lock()
        return _order_locks[order_id]


# ─────────────────────────────────────────────
# VALIDATION LOGIC
# ─────────────────────────────────────────────
def _validate_pending_trade(broker, trade: dict) -> None:
    """
    Validate a single SL_TRIGGER_PENDING trade.
    Must only be called after 9:20 AM on or after Scheduled_Exit_Date.
    """
    order_id       = trade["Order_ID"]
    symbol         = trade["Symbol"]
    token          = trade["Angel_Token"]
    pending_sl     = float(trade.get("Pending_SL") or trade["Last_SL"])
    sched_date     = trade.get("Scheduled_Exit_Date", "")
    rem_qty        = get_remaining_qty(trade)
    today_str      = ist_today().strftime("%Y-%m-%d")

    divider(f"SL VALIDATE {symbol}")
    log.info(
        "[Scheduler] %s  pending_sl=%.2f  sched_date=%s  today=%s  rem_qty=%d",
        symbol, pending_sl, sched_date, today_str, rem_qty,
    )

    # ── Date guard ────────────────────────────────────────────────────────────
    if sched_date > today_str:
        log.info(
            "[Scheduler] %s  not yet due (sched=%s, today=%s) — skipping",
            symbol, sched_date, today_str,
        )
        return

    # ── Qty guard ─────────────────────────────────────────────────────────────
    if rem_qty <= 0:
        log.warning("[Scheduler] %s  rem_qty=0 — closing without sell", symbol)
        close_trade(order_id, "SL_HIT_NO_QTY")
        return

    # ── Prevent concurrent duplicate execution ───────────────────────────────
    order_lock = _get_order_lock(order_id)
    if not order_lock.acquire(blocking=False):
        log.warning("[Scheduler] %s  lock busy — skip this cycle", symbol)
        return

    try:
        _execute_sl_validation(broker, trade, pending_sl, rem_qty, symbol, token, order_id)
    finally:
        order_lock.release()


def _execute_sl_validation(
    broker,
    trade:      dict,
    pending_sl: float,
    rem_qty:    int,
    symbol:     str,
    token:      str,
    order_id:   str,
) -> None:
    """Core validation: fetch LTP and decide exit vs restore."""

    # ── Fetch LTP ─────────────────────────────────────────────────────────────
    try:
        ltp = broker.get_ltp_with_retry("NSE", symbol, token, retries=5)
    except Exception as e:
        log.error("[Scheduler] LTP fetch failed %s: %s — deferring", symbol, e)
        return

    if not ltp or ltp <= 0:
        log.error("[Scheduler] %s  LTP unavailable — deferring exit", symbol)
        return

    log.info(
        "[Scheduler] %s  ltp=%.2f  pending_sl=%.2f",
        symbol, ltp, pending_sl,
    )

    # ── Decision ──────────────────────────────────────────────────────────────
    if ltp < pending_sl:
        # Price still below SL → execute MARKET SELL
        log.warning(
            "[Scheduler] SL CONFIRMED  %s  ltp=%.2f < sl=%.2f  "
            "→ MARKET SELL qty=%d",
            symbol, ltp, pending_sl, rem_qty,
        )
        _execute_sl_exit(broker, trade, ltp, rem_qty, pending_sl)
    else:
        # Price recovered → restore ACTIVE
        log.info(
            "[Scheduler] SL RECOVERED  %s  ltp=%.2f >= sl=%.2f  "
            "→ restoring ACTIVE",
            symbol, ltp, pending_sl,
        )
        restore_active(order_id)
        log.info("[Scheduler] %s  ACTIVE restored — trailing continues", symbol)


def _execute_sl_exit(
    broker,
    trade:      dict,
    ltp:        float,
    rem_qty:    int,
    pending_sl: float,
) -> None:
    """Place MARKET SELL for remaining qty and close the trade."""
    order_id = trade["Order_ID"]
    symbol   = trade["Symbol"]
    token    = trade["Angel_Token"]

    sell_result = broker.place_sell_market_order(
        trading_symbol=symbol,
        token=token,
        qty=rem_qty,
    )

    log.info("[Scheduler]   sell_response = %s", sell_result)

    if sell_result.get("status") != "success":
        log.error(
            "[Scheduler] SELL FAILED %s  qty=%d  err=%s  "
            "— leaving as SL_TRIGGER_PENDING for manual review",
            symbol, rem_qty, sell_result.get("message"),
        )
        # Do NOT close — human intervention required
        update_trade(order_id, {"SubAction": "SL_SELL_FAILED"})
        return

    sell_order_id = sell_result.get("order_id", "")

    # Update and close
    update_trade(order_id, {
        "SubAction":          "SL_HIT",
        "Last_Target_Action": "SL_EXIT",
        "Last_Target_Time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    close_trade(order_id, "SL_HIT")

    log.info(
        "[Scheduler] TRADE CLOSED  %s  sell_order=%s  qty=%d  "
        "exit_price≈%.2f  confirmed_sl=%.2f",
        symbol, sell_order_id, rem_qty, ltp, pending_sl,
    )


# ─────────────────────────────────────────────
# EVENING SESSION — CLOSING-BASIS SL CHECK
# ─────────────────────────────────────────────
_eod_done_for: Optional[str] = None     # IST date the EOD check completed


def _eod_sl_check(broker) -> None:
    """
    After the close: closing LTP < Last_SL → SL_TRIGGER_PENDING (exit next day).
    Runs once per trading day; retried next cycle if any LTP fetch fails.
    Restart-safe: already-pending trades are skipped (only ACTIVE checked).
    """
    global _eod_done_for
    today = ist_today().strftime("%Y-%m-%d")
    if _eod_done_for == today:
        return

    trades = [t for t in load_active() if t.get("Status") == "ACTIVE"]
    divider("EOD SL CHECK (closing basis)")
    log.info("[Scheduler] EOD SL check: %d ACTIVE trades", len(trades))

    all_ok   = True
    next_day = get_next_trading_day()
    for t in trades:
        symbol = t["Symbol"]
        try:
            close   = broker.get_ltp_with_retry("NSE", symbol, t["Angel_Token"], retries=3)
            last_sl = float(t.get("Last_SL") or t["SL_Price"])
        except Exception as e:
            log.error("[Scheduler] EOD %s: %s — retry next cycle", symbol, e)
            all_ok = False
            continue
        if not close:
            log.error("[Scheduler] EOD %s: close price unavailable — retry next cycle", symbol)
            all_ok = False
            continue

        if close < last_sl:
            log.warning(
                "[Scheduler] EOD SL BREACH %s  close=%.2f < sl=%.2f  "
                "→ SL_TRIGGER_PENDING  validate on %s",
                symbol, close, last_sl, next_day,
            )
            mark_sl_pending(
                order_id            = t["Order_ID"],
                pending_sl          = last_sl,
                triggered_price     = close,
                scheduled_exit_date = next_day,
            )
        else:
            log.info("[Scheduler] EOD %s  close=%.2f >= sl=%.2f — holding", symbol, close, last_sl)

    if all_ok:
        _eod_done_for = today
        log.info("[Scheduler] EOD SL check complete for %s", today)


# ─────────────────────────────────────────────
# MAIN POLL CYCLE
# ─────────────────────────────────────────────
def _run_cycle(broker) -> None:
    """
    Called every SCHEDULER_POLL_SECS.
      • after 15:35          → closing-basis SL check (evening session)
      • 09:20 – 15:30        → next-day validation / exit of pending SLs
    """
    if after_eod_check():
        _eod_sl_check(broker)
        return

    if not (after_920() and market_open()):
        log.debug("[Scheduler] outside 09:20–15:30 — no exits")
        return

    pending_trades = load_sl_pending()
    log.info("[Scheduler] cycle: %d SL_TRIGGER_PENDING trades", len(pending_trades))

    for trade in pending_trades:
        try:
            _validate_pending_trade(broker, trade)
        except Exception as e:
            log.error(
                "[Scheduler] Error processing %s: %s",
                trade.get("Symbol"), e,
                exc_info=True,
            )


# ─────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────
class ExitScheduler:
    """
    Background thread for next-day SL validation.
    Restart-safe: all state is in S3.
    """

    def __init__(self):
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.status  = {"last_run": None, "pending_count": 0}
        log.info(
            "[Scheduler] ExitScheduler initialised  poll=%ds",
            SCHEDULER_POLL_SECS,
        )

    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="exit-scheduler",
            daemon=True,
        )
        self._thread.start()
        log.info("[Scheduler] Started  poll=%ds", SCHEDULER_POLL_SECS)

    def stop(self):
        log.info("[Scheduler] stop() called")
        self.running = False

    def _loop(self):
        while self.running:
            try:
                from angel_broker import get_broker
                broker = get_broker()
                _run_cycle(broker)
                self.status["last_run"]      = datetime.now().isoformat()
                self.status["pending_count"] = len(load_sl_pending())
            except Exception as e:
                log.error("[Scheduler] Loop error: %s", e, exc_info=True)
            time.sleep(SCHEDULER_POLL_SECS)


# ── Singleton ─────────────────────────────────────────────────────────────────
_scheduler       = ExitScheduler()
_started         = False
_start_lock      = threading.Lock()


def get_scheduler() -> ExitScheduler:
    return _scheduler


def start_scheduler() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    _scheduler.start()
