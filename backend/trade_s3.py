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


def already_traded_today(symbol: str) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    for r in _read():
        if r["Symbol"].upper() == symbol.upper():
            if r.get("Entry_Time","").startswith(today):
                return True
    return False