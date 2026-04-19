"""
watchlist_s3.py
===============
S3-backed watchlist CSV.

Bucket : dhan-trading-data
Key    : angel/watchlist.csv

Schema
------
Symbol | Angel_Token | Entry_Price | SL_Price | Target_Price |
Breakout | Risk_Percent | Rank | Action | Last_Updated

Rules
-----
- LTP is NEVER stored here — it is always fetched live
- Breakout  : YES / NO  (set by breakout_engine.py)
- Action    : WATCH / BUY / AUTO_BUYED
- Risk_Percent = (Entry - SL) / Entry * 100
- Rank      : computed by ranking engine, higher = better
"""
import io, csv, os, logging
from datetime import datetime
from typing import List, Dict, Optional
import boto3
from botocore.exceptions import ClientError

log       = logging.getLogger(__name__)
S3_BUCKET = os.getenv("S3_BUCKET", "dhan-trading-data")
S3_KEY    = "angel/watchlist.csv"

HEADERS = [
    "Symbol","Angel_Token","Entry_Price","SL_Price","Target_Price",
    "Breakout","Risk_Percent","Rank","Action","Last_Updated",
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
    log.debug("[Watchlist] Saved %d rows", len(data))


def load_watchlist()       -> List[Dict]: return _read()
def save_watchlist(data)   -> None:       _write(data)


def add_symbol(
    symbol:       str,
    angel_token:  str,
    entry_price:  float,
    sl_price:     float,
    target_price: float,
) -> Dict:
    risk_pct = round(abs(entry_price - sl_price) / entry_price * 100, 2) if entry_price else 0
    row = {
        "Symbol":       symbol.upper().strip(),
        "Angel_Token":  angel_token,
        "Entry_Price":  str(entry_price),
        "SL_Price":     str(sl_price),
        "Target_Price": str(target_price),
        "Breakout":     "NO",
        "Risk_Percent": str(risk_pct),
        "Rank":         "0",
        "Action":       "WATCH",
        "Last_Updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    data = _read()
    # Prevent duplicates
    data = [r for r in data if r["Symbol"].upper() != symbol.upper()]
    data.append(row)
    _write(data)
    log.info("[Watchlist] Added %s entry=%.2f sl=%.2f tgt=%.2f",
             symbol, entry_price, sl_price, target_price)
    return row


def delete_symbol(symbol: str) -> bool:
    data = _read()
    before = len(data)
    data   = [r for r in data if r["Symbol"].upper() != symbol.upper()]
    if len(data) < before:
        _write(data); log.info("[Watchlist] Deleted %s", symbol); return True
    return False


def update_row(symbol: str, fields: dict) -> bool:
    data = _read()
    for row in data:
        if row["Symbol"].upper() == symbol.upper():
            row.update(fields)
            row["Last_Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _write(data); return True
    log.warning("[Watchlist] update_row: %s not found", symbol)
    return False


def mark_auto_buyed(symbol: str) -> None:
    update_row(symbol, {"Action": "AUTO_BUYED", "Breakout": "YES"})


def get_symbol(symbol: str) -> Optional[Dict]:
    for r in _read():
        if r["Symbol"].upper() == symbol.upper():
            return r
    return None
