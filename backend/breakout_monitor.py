"""
breakout_monitor.py
===================
Background auto-buy monitor.

Poll cycle
----------
1. run_breakout_engine() — refresh LTP, compute breakout + ranks.
2. If auto_buy_enabled AND time ≥ 09:31:
   a. get_top_candidate() — highest-ranked breakout not traded today.
   b. execute_trade() — size, order, confirm, GTT, write CSV.
3. Status dict updated for /health endpoint.
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
    return n.weekday() < 5 and 555 <= m <= 930

def _after_931() -> bool:
    n = _ist_now(); m = n.hour*60+n.minute
    return n.weekday() < 5 and 571 <= m <= 930


class BreakoutMonitor:
    def __init__(self):
        self.auto_buy_enabled = True   # ✅ default ON
        self.running          = False
        self._thread: Optional[threading.Thread] = None
        self.status = {"last_poll":None,"breakouts":[],"errors":[],"last_trade":None}

    def start(self):
        if self.running: return
        self.running = True
        self._thread = threading.Thread(target=self._loop,
                                        name="breakout-monitor", daemon=True)
        self._thread.start()
        log.info("[Monitor] Started (poll=%ds)", POLL_SECS)

    def stop(self):  self.running = False

    def _loop(self):
        while self.running:
            try:    self._poll()
            except Exception as e:
                log.error("[Monitor] Poll error: %s", e, exc_info=True)
                self.status["errors"].append(str(e))
            time.sleep(POLL_SECS)

    def _poll(self):
        if not _market_open(): return
        from angel_broker  import get_broker
        from breakout_engine import run_breakout_engine, get_top_candidate
        from trade_executor  import execute_trade

        broker   = get_broker()
        enriched = run_breakout_engine(broker)

        self.status["last_poll"] = datetime.now().isoformat()
        self.status["breakouts"] = [
            r["Symbol"] for r in enriched if r.get("Breakout")=="YES"
        ]

        if not self.auto_buy_enabled or not _after_931():
            return
        # 🔥 HARD BLOCK → already traded today
        if already_traded_today():
            log.info("[Monitor] Skipping auto-buy (already traded today)")
            return
        candidate = get_top_candidate(broker)
        if not candidate:
            return

        sym   = candidate["Symbol"]
        token = candidate["Angel_Token"]
        entry = float(candidate["Entry_Price"])
        sl    = float(candidate["SL_Price"])
        tgt   = float(candidate["Target_Price"])

        log.info("[Monitor] Auto-buying rank=%s %s entry=%.2f",
                 candidate.get("Rank"), sym, entry)

        result = execute_trade(broker, sym, token, entry, sl, tgt, is_auto=True)
        if result["success"]:
            self.status["last_trade"] = {
                "symbol":sym,"order_id":result["order_id"],
                "time":datetime.now().isoformat(),
            }
            log.info("[Monitor] Auto-buy SUCCESS %s order_id=%s",
                     sym, result["order_id"])
        else:
            log.warning("[Monitor] Auto-buy FAILED %s: %s",
                        sym, result.get("error"))


_monitor = BreakoutMonitor()
def get_monitor()  -> BreakoutMonitor: return _monitor
def start_monitor() -> None:           _monitor.start()
