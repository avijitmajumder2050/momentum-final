"""
trade_s3.py
===========
S3-backed active + closed trade ledger.

Bucket : dhan-trading-data
Key    : angel/angel_active_trade.csv

Schema
------
Order_ID | Symbol | Angel_Token | Entry_Price | SL_Price | Target_Price |
Qty | Status | Entry_Time | Exit_Time | Exit_Reason |
GTT_ID | Last_SL | Trailing_Active | SubAction
"""
import io, csv, os, logging
from datetime import datetime
from typing import List, Dict, Optional
import boto3
from botocore.exceptions import ClientError

log       = logging.getLogger(__name__)
S3_BUCKET = os.getenv("S3_BUCKET", "dhan-trading-data")
S3_KEY    = "angel/angel_active_trade.csv"

HEADERS = [
    "Order_ID","Symbol","Angel_Token","Entry_Price","SL_Price","Target_Price",
    "Qty","Status","Entry_Time","Exit_Time","Exit_Reason",
    "GTT_ID","Last_SL","Trailing_Active","SubAction",
    "Mode",   # 🔥 ADD THIS
]


def _s3():
    return boto3.client("s3", region_name=os.getenv("AWS_REGION","ap-south-1"))


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
    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=HEADERS, extrasaction="ignore")
    w.writeheader()
    w.writerows(data)

    _s3().put_object(
        Bucket=S3_BUCKET,
        Key=S3_KEY,
        Body=buf.getvalue().encode(),
        ContentType="text/csv"
    )

    log.debug("[Trades] Saved %d rows", len(data))


def load_trades() -> List[Dict]:
    return _read()


def save_trades(data) -> None:
    _write(data)


def load_active() -> List[Dict]:
    return [r for r in _read() if r["Status"] == "ACTIVE"]


def load_closed() -> List[Dict]:
    return [r for r in _read() if r["Status"] == "CLOSED"]


# ─────────────────────────────────────────────
# OPEN TRADE
# ─────────────────────────────────────────────
def open_trade(
    order_id:      str,
    symbol:        str,
    angel_token:   str,
    entry_price:   float,
    sl_price:      float,
    target_price:  float,
    qty:           int,
    gtt_id:        str = "",
    mode: str = "AUTO",   # 🔥 ADD
) -> Dict:

    row = {
        "Order_ID":      order_id,
        "Symbol":        symbol.upper(),
        "Angel_Token":   angel_token,
        "Entry_Price":   str(entry_price),
        "SL_Price":      str(sl_price),
        "Target_Price":  str(target_price),
        "Qty":           str(qty),
        "Status":        "ACTIVE",
        "Entry_Time":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Exit_Time":     "",
        "Exit_Reason":   "",
        "GTT_ID":        gtt_id,
        "Last_SL":       str(sl_price),
        "Trailing_Active":"False",
        "SubAction":     "ENTRY",
        "Mode": mode,   # 🔥 ADD
    }

    data = _read()
    data.append(row)
    _write(data)

    log.info("[Trades] Opened %s order_id=%s qty=%d entry=%.2f",
             symbol, order_id, qty, entry_price)

    return row


# ─────────────────────────────────────────────
# UPDATE TRADE
# ─────────────────────────────────────────────
def update_trade(order_id: str, fields: dict) -> bool:
    data = _read()

    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] == "ACTIVE":
            row.update(fields)
            _write(data)
            return True

    log.warning("[Trades] update_trade: %s not found or CLOSED", order_id)
    return False


# ─────────────────────────────────────────────
# CLOSE TRADE
# ─────────────────────────────────────────────
def close_trade(order_id: str, exit_reason: str) -> bool:
    data = _read()

    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] == "ACTIVE":
            row["Status"]      = "CLOSED"
            row["Exit_Time"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row["Exit_Reason"] = exit_reason
            row["SubAction"]   = exit_reason

            _write(data)
            log.info("[Trades] Closed %s reason=%s", order_id, exit_reason)
            return True

    return False


# ─────────────────────────────────────────────
# TRAILING UPDATE
# ─────────────────────────────────────────────
def update_trailing_sl(order_id: str, new_sl: float) -> bool:
    return update_trade(order_id, {
        "Last_SL":        str(round(new_sl, 2)),
        "Trailing_Active":"True",
        "SubAction":      "TRAILING",
    })


# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────
def get_trade(order_id: str) -> Optional[Dict]:
    for r in _read():
        if r["Order_ID"] == str(order_id):
            return r
    return None


def get_active_trade_by_symbol(symbol: str) -> Optional[Dict]:
    """Return the first ACTIVE trade row for this symbol, or None."""
    for r in _read():
        if r["Symbol"].upper() == symbol.upper() and r["Status"] == "ACTIVE":
            log.info("[Trades] found active trade for %s  order_id=%s  qty=%s",
                     symbol, r["Order_ID"], r["Qty"])
            return r
    return None


def add_qty_to_trade(
    order_id:      str,
    added_qty:     int,
    new_avg_entry: float,
    new_gtt_id:    str = "",
) -> Optional[Dict]:
    """
    Pyramid: update existing row in-place.
      Qty          += added_qty
      Entry_Price   = new weighted average
      GTT_ID        = new_gtt_id  (if provided)
      SubAction     = ADD_QTY+N
    No new row is written.
    """
    data = _read()
    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] == "ACTIVE":
            old_qty   = int(row["Qty"])
            total_qty = old_qty + added_qty
            row["Qty"]         = str(total_qty)
            row["Entry_Price"] = str(round(new_avg_entry, 2))
            row["SubAction"]   = f"ADD_QTY+{added_qty}"
            if new_gtt_id:
                row["GTT_ID"]  = new_gtt_id
            _write(data)
            log.info("[Trades] add_qty  order_id=%s  old=%d  added=%d  total=%d  avg=%.2f",
                     order_id, old_qty, added_qty, total_qty, new_avg_entry)
            return row
    log.warning("[Trades] add_qty_to_trade: %s not found or CLOSED", order_id)
    return None
def already_traded_today() -> bool:
    """
    Returns True if ANY trade (ACTIVE or CLOSED) was opened today.
    Used for AUTO BUY restriction (1 trade per day).
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