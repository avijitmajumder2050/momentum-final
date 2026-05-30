"""
trade_s3.py
===========
S3-backed active + closed trade ledger.

Bucket : dhan-trading-data
Key    : angel/angel_active_trade.csv

Schema (v2 — GTT-free, next-day SL, partial booking)
------
Order_ID | Symbol | Angel_Token | Entry_Price | SL_Price | Target_Price |
Qty | Status | Entry_Time | Exit_Time | Exit_Reason |
GTT_ID(legacy) | Last_SL | Trailing_Active | SubAction | Mode |
Entry_Date | Pending_SL | SL_Triggered_Time | SL_Triggered_Price |
Exit_Scheduled | Scheduled_Exit_Date |
Initial_Qty | Remaining_Qty | Booked_Qty |
Partial_Booked | Partial_Book_Time | Partial_Book_Price | Partial_Book_OrderId |
Last_Target_Action | Last_Target_Time
"""
import io, csv, os, logging, threading
from datetime import datetime, date
from typing import List, Dict, Optional
import boto3
from botocore.exceptions import ClientError

log        = logging.getLogger(__name__)
S3_BUCKET  = os.getenv("S3_BUCKET", "dhan-trading-data")
S3_KEY     = "angel/angel_active_trade.csv"
_write_lock = threading.Lock()          # thread-safe writes

# ── Schema ───────────────────────────────────────────────────────────────────
HEADERS = [
    # Core identity
    "Order_ID", "Symbol", "Angel_Token",
    # Prices
    "Entry_Price", "SL_Price", "Target_Price",
    # Quantity (v2)
    "Initial_Qty", "Remaining_Qty", "Booked_Qty", "Qty",          # Qty kept for legacy reads
    # State
    "Status",                      # ACTIVE | SL_TRIGGER_PENDING | EXIT_SCHEDULED | CLOSED
    "Mode",                        # AUTO | MANUAL
    # Timestamps
    "Entry_Date",                  # YYYY-MM-DD — used for same-day vs overnight rule
    "Entry_Time",
    "Exit_Time",
    "Exit_Reason",
    # Trailing
    "Last_SL",
    "Trailing_Active",
    "SubAction",
    # ── Delayed SL fields ────────────────────────────────────────────────────
    "Pending_SL",                  # The SL level that was breached
    "SL_Triggered_Time",           # ISO timestamp of breach
    "SL_Triggered_Price",          # LTP at moment of breach
    "Exit_Scheduled",              # True / False
    "Scheduled_Exit_Date",         # YYYY-MM-DD — next trading day
    # ── Partial booking fields ───────────────────────────────────────────────
    "Partial_Booked",              # True / False
    "Partial_Book_Time",
    "Partial_Book_Price",
    "Partial_Book_OrderId",
    # ── Target tracking ──────────────────────────────────────────────────────
    "Last_Target_Action",          # PARTIAL_SOLD | FULL_SOLD
    "Last_Target_Time",
    # Legacy / GTT (kept for backward compat — no longer written)
    "GTT_ID",
]

_DEFAULTS = {h: "" for h in HEADERS}


def _s3():
    return boto3.client("s3", region_name=os.getenv("AWS_REGION", "ap-south-1"))


# ── Raw I/O ───────────────────────────────────────────────────────────────────
def _read() -> List[Dict]:
    try:
        obj  = _s3().get_object(Bucket=S3_BUCKET, Key=S3_KEY)
        rows = list(csv.DictReader(io.StringIO(obj["Body"].read().decode())))
        for r in rows:
            for h in HEADERS:
                r.setdefault(h, "")
        return rows
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return []
        raise


def _write(data: List[Dict]) -> None:
    with _write_lock:
        buf = io.StringIO()
        w   = csv.DictWriter(buf, fieldnames=HEADERS, extrasaction="ignore")
        w.writeheader()
        w.writerows(data)
        _s3().put_object(
            Bucket=S3_BUCKET,
            Key=S3_KEY,
            Body=buf.getvalue().encode(),
            ContentType="text/csv",
        )
    log.debug("[Trades] Saved %d rows", len(data))


# ── Public loaders ────────────────────────────────────────────────────────────
def load_trades()  -> List[Dict]: return _read()
def save_trades(d) -> None:        _write(d)

def load_active() -> List[Dict]:
    """Return ACTIVE and SL_TRIGGER_PENDING rows (both are 'live' trades)."""
    return [r for r in _read() if r["Status"] in ("ACTIVE", "SL_TRIGGER_PENDING")]

def load_sl_pending() -> List[Dict]:
    return [r for r in _read() if r["Status"] == "SL_TRIGGER_PENDING"]

def load_closed() -> List[Dict]:
    return [r for r in _read() if r["Status"] == "CLOSED"]


# ── OPEN TRADE ────────────────────────────────────────────────────────────────
def open_trade(
    order_id:     str,
    symbol:       str,
    angel_token:  str,
    entry_price:  float,
    sl_price:     float,
    target_price: float,
    qty:          int,
    gtt_id:       str = "",        # kept for signature compat — not used
    mode:         str = "AUTO",
) -> Dict:
    today = datetime.now().strftime("%Y-%m-%d")
    row = {
        **_DEFAULTS,
        "Order_ID":          order_id,
        "Symbol":            symbol.upper(),
        "Angel_Token":       angel_token,
        "Entry_Price":       str(entry_price),
        "SL_Price":          str(sl_price),
        "Target_Price":      str(target_price),
        # Qty fields
        "Initial_Qty":       str(qty),
        "Remaining_Qty":     str(qty),
        "Booked_Qty":        "0",
        "Qty":               str(qty),          # legacy mirror
        # State
        "Status":            "ACTIVE",
        "Mode":              mode,
        # Timestamps
        "Entry_Date":        today,
        "Entry_Time":        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # Trailing
        "Last_SL":           str(sl_price),
        "Trailing_Active":   "False",
        "SubAction":         "ENTRY",
        # Partial
        "Partial_Booked":    "False",
        # Exit scheduling
        "Exit_Scheduled":    "False",
        # GTT legacy
        "GTT_ID":            "",               # never used in new arch
    }

    data = _read()
    data.append(row)
    _write(data)

    log.info("[Trades] Opened %s order_id=%s qty=%d entry=%.2f mode=%s",
             symbol, order_id, qty, entry_price, mode)
    return row


# ── UPDATE TRADE ──────────────────────────────────────────────────────────────
def update_trade(order_id: str, fields: dict) -> bool:
    data = _read()
    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] not in ("CLOSED",):
            row.update(fields)
            _write(data)
            return True
    log.warning("[Trades] update_trade: %s not found or CLOSED", order_id)
    return False


# ── CLOSE TRADE ───────────────────────────────────────────────────────────────
def close_trade(order_id: str, exit_reason: str) -> bool:
    data = _read()
    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] != "CLOSED":
            row["Status"]     = "CLOSED"
            row["Exit_Time"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["Exit_Reason"]= exit_reason
            row["SubAction"]  = exit_reason
            _write(data)
            log.info("[Trades] Closed %s reason=%s", order_id, exit_reason)
            return True
    return False


# ── TRAILING SL UPDATE ────────────────────────────────────────────────────────
def update_trailing_sl(order_id: str, new_sl: float) -> bool:
    return update_trade(order_id, {
        "Last_SL":         str(round(new_sl, 2)),
        "Trailing_Active": "True",
        "SubAction":       "TRAILING",
    })


# ── SL TRIGGER PENDING ────────────────────────────────────────────────────────
def mark_sl_pending(
    order_id:        str,
    pending_sl:      float,
    triggered_price: float,
    scheduled_exit_date: str,     # YYYY-MM-DD
) -> bool:
    """
    Called by trailing_engine when LTP breaches any SL level.
    Does NOT exit immediately — schedules next-day validation.
    """
    return update_trade(order_id, {
        "Status":              "SL_TRIGGER_PENDING",
        "Pending_SL":          str(round(pending_sl, 2)),
        "SL_Triggered_Time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "SL_Triggered_Price":  str(round(triggered_price, 2)),
        "Exit_Scheduled":      "True",
        "Scheduled_Exit_Date": scheduled_exit_date,
        "SubAction":           "SL_TRIGGER_PENDING",
    })


def restore_active(order_id: str) -> bool:
    """
    Called by exit_scheduler when LTP recovers above pending SL next day.
    Clears the pending state and restores ACTIVE.
    """
    return update_trade(order_id, {
        "Status":              "ACTIVE",
        "Pending_SL":          "",
        "SL_Triggered_Time":   "",
        "SL_Triggered_Price":  "",
        "Exit_Scheduled":      "False",
        "Scheduled_Exit_Date": "",
        "SubAction":           "SL_RECOVERED",
    })


# ── PARTIAL BOOKING ───────────────────────────────────────────────────────────
def record_partial_booking(
    order_id:       str,
    sold_qty:       int,
    sell_price:     float,
    sell_order_id:  str,
) -> Optional[Dict]:
    """
    Deduct sold_qty from Remaining_Qty, add to Booked_Qty.
    Marks Partial_Booked = True.
    Returns updated row or None.
    """
    data = _read()
    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] not in ("CLOSED",):
            old_rem  = int(row.get("Remaining_Qty") or row.get("Qty") or 0)
            old_book = int(row.get("Booked_Qty") or 0)

            new_rem  = max(0, old_rem - sold_qty)
            new_book = old_book + sold_qty

            row["Remaining_Qty"]       = str(new_rem)
            row["Booked_Qty"]          = str(new_book)
            row["Qty"]                 = str(new_rem)   # keep legacy in sync
            row["Partial_Booked"]      = "True"
            row["Partial_Book_Time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["Partial_Book_Price"]  = str(round(sell_price, 2))
            row["Partial_Book_OrderId"]= sell_order_id
            row["Last_Target_Action"]  = "PARTIAL_SOLD"
            row["Last_Target_Time"]    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["SubAction"]           = "PARTIAL_BOOK"

            _write(data)
            log.info(
                "[Trades] Partial booked order_id=%s  sold=%d  rem=%d  price=%.2f",
                order_id, sold_qty, new_rem, sell_price,
            )
            return row
    log.warning("[Trades] record_partial_booking: %s not found", order_id)
    return None


# ── PYRAMID ───────────────────────────────────────────────────────────────────
def add_qty_to_trade(
    order_id:      str,
    added_qty:     int,
    new_avg_entry: float,
    new_gtt_id:    str = "",       # kept for compat — not used
) -> Optional[Dict]:
    data = _read()
    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] != "CLOSED":
            old_rem  = int(row.get("Remaining_Qty") or row.get("Qty") or 0)
            old_init = int(row.get("Initial_Qty") or row.get("Qty") or 0)

            new_rem  = old_rem  + added_qty
            new_init = old_init + added_qty

            row["Remaining_Qty"] = str(new_rem)
            row["Initial_Qty"]   = str(new_init)
            row["Qty"]           = str(new_rem)
            row["Entry_Price"]   = str(round(new_avg_entry, 2))
            row["SubAction"]     = f"ADD_QTY+{added_qty}"

            _write(data)
            log.info(
                "[Trades] add_qty order_id=%s  added=%d  new_rem=%d  avg=%.2f",
                order_id, added_qty, new_rem, new_avg_entry,
            )
            return row
    log.warning("[Trades] add_qty_to_trade: %s not found or CLOSED", order_id)
    return None


# ── UTILITIES ─────────────────────────────────────────────────────────────────
def get_trade(order_id: str) -> Optional[Dict]:
    for r in _read():
        if r["Order_ID"] == str(order_id):
            return r
    return None


def get_active_trade_by_symbol(symbol: str) -> Optional[Dict]:
    for r in _read():
        if (
            r["Symbol"].upper() == symbol.upper()
            and r["Status"] in ("ACTIVE", "SL_TRIGGER_PENDING")
        ):
            log.info("[Trades] found active trade for %s  order_id=%s  rem_qty=%s",
                     symbol, r["Order_ID"], r.get("Remaining_Qty"))
            return r
    return None


def already_traded_today() -> bool:
    """
    Returns True if ANY AUTO trade was opened today.
    Prevents multiple auto-buys on the same day.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    for r in _read():
        if (
            r.get("Mode") == "AUTO"
            and r.get("Entry_Time", "").startswith(today)
        ):
            log.info("[Trades] AUTO trade already exists today → BLOCK")
            return True
    log.info("[Trades] No AUTO trade today → ALLOW")
    return False


def get_remaining_qty(trade: Dict) -> int:
    """Safe helper — prefer Remaining_Qty, fallback to Qty."""
    v = trade.get("Remaining_Qty") or trade.get("Qty") or "0"
    try:
        return max(0, int(v))
    except (ValueError, TypeError):
        return 0
