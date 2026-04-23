"""
breakout_monitor.py
===================
Background auto-buy monitor (FIXED)

Fixes
-----
1. No infinite retry (max 2 attempts)
2. Rank1 → Rank2 → STOP
3. Reset only on new breakout
4. Broker login once (rate limit fix)
"""

import os, time, logging, threading
from datetime import datetime
from typing import Optional
from trade_s3 import already_traded_today

log       = logging.getLogger(__name__)
POLL_SECS = int(os.getenv("MONITOR_INTERVAL","15"))


def _ist_now():
    import pytz, datetime as dt
    return dt.datetime.now(pytz.timezone("Asia/Kolkata"))

def _market_open() -> bool:
    n = _ist_now(); m = n.hour*60+n.minute
    return n.weekday() < 5 and 555 <= m <= 1440   # testing

def _after_931() -> bool:
    n = _ist_now(); m = n.hour*60+n.minute
    return n.weekday() < 5 and 571 <= m <= 1440   # testing


class BreakoutMonitor:
    def __init__(self):
        self.auto_buy_enabled = True
        self.running          = False
        self._thread: Optional[threading.Thread] = None

        self.status = {
            "last_poll":None,
            "breakouts":[],
            "errors":[],
            "last_trade":None
        }

        # 🔥 NEW STATE
        self.attempted_symbols = set()
        self._last_breakouts   = set()

        # 🔥 FIX: create broker only once
        from angel_broker import get_broker
        self.broker = get_broker()

    def start(self):
        if self.running: return
        self.running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="breakout-monitor",
            daemon=True
        )
        self._thread.start()
        log.info("[Monitor] Started (poll=%ds)", POLL_SECS)

    def stop(self):
        self.running = False

    def _loop(self):
        while self.running:
            try:
                self._poll()
            except Exception as e:
                log.error("[Monitor] Poll error: %s", e, exc_info=True)
                self.status["errors"].append(str(e))
            time.sleep(POLL_SECS)

    def _poll(self):
        if not _market_open():
            return

        from breakout_engine import run_breakout_engine
        from trade_executor  import execute_trade

        broker   = self.broker
        enriched = run_breakout_engine(broker)

        self.status["last_poll"] = datetime.now().isoformat()

        # ─────────────────────────────
        # Breakouts
        # ─────────────────────────────
        current_breakouts = set(
            r["Symbol"] for r in enriched if r.get("Breakout")=="YES"
        )
        self.status["breakouts"] = list(current_breakouts)

        # 🔥 reset only if new breakout
        if current_breakouts != self._last_breakouts:
            log.info("[Monitor] New breakout detected → reset attempts")
            self.attempted_symbols.clear()

        self._last_breakouts = current_breakouts

        # ─────────────────────────────
        # Logging
        # ─────────────────────────────
        traded_today = already_traded_today()
        log.info(
            "[Monitor] auto_buy_enabled=%s | after_931=%s | already_traded_today=%s",
            self.auto_buy_enabled,
            _after_931(),
            traded_today
        )

        # ─────────────────────────────
        # Guards
        # ─────────────────────────────
        if not self.auto_buy_enabled or not _after_931():
            return

        if traded_today:
            log.info("[Monitor] Skipping auto-buy (already traded today)")
            return

        # ─────────────────────────────
        # Candidate selection (NO get_top_candidate)
        # ─────────────────────────────
        candidates = [
            r for r in enriched
            if r.get("Breakout") == "YES"
            and r.get("Action") != "AUTO_BUYED"
            and r["Symbol"] not in self.attempted_symbols
        ]

        candidates = sorted(
            candidates,
            key=lambda r: int(r.get("Rank") or 999)
        )

        if not candidates:
            return

        # ─────────────────────────────
        # Try max 2 candidates
        # ─────────────────────────────
        max_attempts = 2
        attempt_count = 0

        for candidate in candidates:
            if attempt_count >= max_attempts:
                break

            sym   = candidate["Symbol"]
            token = candidate["Angel_Token"]
            entry = float(candidate["Entry_Price"])
            sl    = float(candidate["SL_Price"])
            tgt   = float(candidate["Target_Price"])

            log.info("[Monitor] Trying auto-buy rank=%s", candidate.get("Rank"))

            result = execute_trade(
                broker, sym, token, entry, sl, tgt, is_auto=True
            )

            # 🔥 mark attempted
            self.attempted_symbols.add(sym)
            attempt_count += 1

            if result["success"]:
                self.status["last_trade"] = {
                    "symbol": sym,
                    "order_id": result["order_id"],
                    "time": datetime.now().isoformat(),
                }
                log.info("[Monitor] Auto-buy SUCCESS")
                return
            else:
                log.warning("[Monitor] Auto-buy FAILED → trying next")

        # ─────────────────────────────
        # Stop after attempts
        # ─────────────────────────────
        if attempt_count > 0:
            log.warning("[Monitor] All candidates failed → waiting for new breakout")


_monitor = BreakoutMonitor()

def get_monitor() -> BreakoutMonitor:
    return _monitor

def start_monitor() -> None:
    _monitor.start()