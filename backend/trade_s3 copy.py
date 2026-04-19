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
GTT_Target_ID | GTT_SL_ID | Last_SL | Trailing_Active

Status values : ACTIVE / CLOSED
Exit_Reason   : TARGET_HIT / SL_HIT / MANUAL_CANCEL / TIME_EXIT / ""

Rules
-----
- LTP is NEVER stored here — always fetched live
- A new row is only written when order_status == "COMPLETE"
- Once CLOSED the row is immutable (never updated again)
- Sub-action tracking: SubAction field added for trailing state labels
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
    "GTT_Target_ID","GTT_SL_ID","Last_SL","Trailing_Active","SubAction",
]


def _s3():
    return boto3.client("s3", region_name=os.getenv("AWS_REGION","ap-south-1"))


def _read() -> List[Dict]:
    try:
        obj  = _s3().get_object(Bucket=S3_BUCKET, Key=S3_KEY)
        rows = list(csv.DictReader(io.StringIO(obj["Body"].read().decode())))
        for r in rows:
            for h in HEADERS: r.setdefault(h,"")
        return rows
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return []
        raise


def _write(data: List[Dict]) -> None:
    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=HEADERS, extrasaction="ignore")
    w.writeheader(); w.writerows(data)
    _s3().put_object(Bucket=S3_BUCKET, Key=S3_KEY,
                     Body=buf.getvalue().encode(), ContentType="text/csv")
    log.debug("[Trades] Saved %d rows", len(data))


def load_trades()      -> List[Dict]: return _read()
def save_trades(data)  -> None:       _write(data)
def load_active()      -> List[Dict]: return [r for r in _read() if r["Status"]=="ACTIVE"]
def load_closed()      -> List[Dict]: return [r for r in _read() if r["Status"]=="CLOSED"]


def open_trade(
    order_id:      str,
    symbol:        str,
    angel_token:   str,
    entry_price:   float,
    sl_price:      float,
    target_price:  float,
    qty:           int,
    gtt_target_id: str = "",
    gtt_sl_id:     str = "",
) -> Dict:
    """
    Append a new ACTIVE trade row.
    Called ONLY after confirming order_status == "COMPLETE".
    """
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
        "GTT_Target_ID": gtt_target_id,
        "GTT_SL_ID":     gtt_sl_id,
        "Last_SL":       str(sl_price),
        "Trailing_Active":"False",
        "SubAction":     "ENTRY",
    }
    data = _read()
    data.append(row)
    _write(data)
    log.info("[Trades] Opened %s order_id=%s qty=%d entry=%.2f",
             symbol, order_id, qty, entry_price)
    return row


def update_trade(order_id: str, fields: dict) -> bool:
    """Update mutable fields for an ACTIVE trade. Ignores CLOSED trades."""
    data = _read()
    for row in data:
        if row["Order_ID"] == str(order_id) and row["Status"] == "ACTIVE":
            row.update(fields)
            _write(data); return True
    log.warning("[Trades] update_trade: %s not found or already CLOSED", order_id)
    return False


def close_trade(order_id: str, exit_reason: str) -> bool:
    """
    Mark a trade CLOSED.  Once closed it is immutable.
    exit_reason: TARGET_HIT / SL_HIT / MANUAL_CANCEL / TIME_EXIT
    """
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
    log.warning("[Trades] close_trade: %s not found or already CLOSED", order_id)
    return False


def get_trade(order_id: str) -> Optional[Dict]:
    for r in _read():
        if r["Order_ID"] == str(order_id):
            return r
    return None


def already_traded_today(symbol: str) -> bool:
    """One trade per day per symbol — checks entry date in all trades."""
    today = datetime.now().strftime("%Y-%m-%d")
    for r in _read():
        if r["Symbol"].upper() == symbol.upper():
            if r.get("Entry_Time","").startswith(today):
                return True
    return False


def update_trailing_sl(order_id: str, new_sl: float, gtt_sl_id: str = "") -> bool:
    """
    Update Last_SL, set Trailing_Active=True, SubAction=TRAILING.
    Called by trailing_engine after a successful GTT modify.
    """
    fields = {
        "Last_SL":        str(round(new_sl,2)),
        "Trailing_Active":"True",
        "SubAction":      "TRAILING",
    }
    if gtt_sl_id:
        fields["GTT_SL_ID"] = gtt_sl_id
    return update_trade(order_id, fields)
