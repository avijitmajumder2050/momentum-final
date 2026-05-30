"""
exit_utils.py
=============
Shared utilities for exit engines.

Provides:
  - IST clock helpers
  - Market open / after-9:20 checks
  - Next trading day calculator  (skips weekends)
  - Divider logger
"""

import logging
from datetime import datetime, date, timedelta
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# TIMEZONE / CLOCK
# ─────────────────────────────────────────────
def ist_now() -> datetime:
    """Return current datetime in IST (Asia/Kolkata)."""
    import pytz
    return datetime.now(pytz.timezone("Asia/Kolkata"))


def ist_today() -> date:
    return ist_now().date()


def ist_now_str() -> str:
    return ist_now().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────
# MARKET CHECKS
# ─────────────────────────────────────────────
def is_trading_day(d: Optional[date] = None) -> bool:
    """
    Returns True if `d` is a weekday (Mon–Fri).
    Extend this function to incorporate NSE holiday list if needed.
    """
    if d is None:
        d = ist_today()
    return d.weekday() < 5   # 0=Mon … 4=Fri


def market_open() -> bool:
    """Returns True during NSE market hours (9:15 AM – 3:30 PM IST)."""
    n    = ist_now()
    mins = n.hour * 60 + n.minute
    return is_trading_day() and (9 * 60 + 15) <= mins <= (15 * 60 + 30)


def after_920() -> bool:
    """Returns True after 9:20 AM IST on a trading day (avoids opening spike)."""
    n    = ist_now()
    mins = n.hour * 60 + n.minute
    return is_trading_day() and mins >= (9 * 60 + 20)


def after_931() -> bool:
    """After 9:31 AM IST — used by breakout monitor."""
    n    = ist_now()
    mins = n.hour * 60 + n.minute
    return is_trading_day() and mins >= (9 * 60 + 31)


# ─────────────────────────────────────────────
# NEXT TRADING DAY
# ─────────────────────────────────────────────
def get_next_trading_day(from_date: Optional[date] = None) -> str:
    """
    Return the next trading day (Mon–Fri) after `from_date` as YYYY-MM-DD.
    Defaults to today (IST).

    Example:
        Friday → Monday
        Saturday → Monday
        Sunday → Monday
        Monday → Tuesday
    """
    if from_date is None:
        from_date = ist_today()

    next_day = from_date + timedelta(days=1)
    while not is_trading_day(next_day):
        next_day += timedelta(days=1)

    result = next_day.strftime("%Y-%m-%d")
    log.debug("[ExitUtils] next_trading_day from %s → %s", from_date, result)
    return result


def is_same_day_trade(entry_date_str: str) -> bool:
    """
    Returns True if entry_date_str (YYYY-MM-DD) equals today (IST).
    Used for same-day vs overnight target rule.
    """
    today = ist_today().strftime("%Y-%m-%d")
    result = entry_date_str.strip() == today
    log.debug(
        "[ExitUtils] same_day_check  entry_date=%s  today=%s  result=%s",
        entry_date_str, today, result,
    )
    return result


# ─────────────────────────────────────────────
# LOGGING HELPER
# ─────────────────────────────────────────────
def divider(label: str = "", width: int = 52) -> None:
    pad = max(0, width - len(label))
    if label:
        log.info("── %s %s", label, "─" * pad)
    else:
        log.info("─" * width)
