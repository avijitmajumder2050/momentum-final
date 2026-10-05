"""
breakout_engine.py
==================
Breakout detection + risk-based ranking engine.

Logic
-----
1. Load watchlist from S3.
2. Fetch live LTP via get_bulk_ltp (batch call).
3. For each symbol:
     Breakout = YES  iff  LTP > Entry_Price
     Breakout_Strength = (LTP - Entry_Price) / Entry_Price * 100
     Risk_Percent      = (Entry_Price - SL_Price) / Entry_Price * 100
     Score             = Breakout_Strength / Risk_Percent   (higher = better)
4. Rank: sort all breakout symbols by Score DESC; assign Rank 1,2,3,...
   Non-breakout symbols get Rank 0.
5. Write updated Breakout / Risk_Percent / Rank / Action back to S3 watchlist.
6. Return enriched rows with live LTP attached (in-memory only, NOT written to CSV).
"""
import logging
from datetime import datetime
from typing import List, Dict

from watchlist_s3 import load_watchlist, save_watchlist

log = logging.getLogger(__name__)


def run_breakout_engine(broker) -> List[Dict]:
    """
    Full breakout scan cycle.

    Parameters
    ----------
    broker : AngelBroker singleton

    Returns
    -------
    List of watchlist rows enriched with live `ltp` field (in-memory only).
    """
    data = load_watchlist()
    log.info("[Breakout]   watchlist rows loaded = %d", len(data))
    if not data:
        log.warning("[Breakout]   watchlist is EMPTY — nothing to scan")
        return []

    # ── Step 1: build instruments list for bulk LTP ───────────────────────────
    instruments = [
        {"symboltoken": r["Angel_Token"], "tradingsymbol": r["Symbol"]}
        for r in data if r.get("Angel_Token")
    ]
    log.info("[Breakout]   fetching bulk LTP for %d instruments", len(instruments))
    live_data = broker.get_bulk_ltp(instruments) if instruments else {}
    log.info("[Breakout]   LTP resolved for %d/%d instruments",
             len(live_data), len(instruments))

    # ── Step 2: compute breakout + scoring ───────────────────────────────────
    log.info("[Breakout] ── PER-SYMBOL ANALYSIS" )
    scored = []
    for row in data:
        sym   = row["Symbol"]
        quote = live_data.get(sym, {})
        ltp   = float(quote.get("ltp", 0) or 0)

        try:
            entry = float(row.get("Entry_Price") or 0)
            sl    = float(row.get("SL_Price")    or 0)
        except (ValueError, TypeError):
            entry = sl = 0

        # Risk percent (static — based on entry vs SL)
        risk_pct = round(abs(entry - sl) / entry * 100, 2) if entry > 0 else 0

        # Breakout detection: LTP strictly above entry
        if ltp > entry > 0:
            strength = round((ltp - entry) / entry * 100, 4)
            score    = round(strength / risk_pct, 4) if risk_pct > 0 else 0
            breakout = "YES"
        else:
            strength = 0.0
            score    = 0.0
            breakout = "NO"

        # Preserve AUTO_BUYED — do not downgrade to WATCH
        current_action = row.get("Action","WATCH")
        if breakout == "YES" and current_action == "WATCH":
            current_action = "BUY"
        elif breakout == "NO" and current_action == "BUY":
            current_action = "WATCH"

        row["Breakout"]     = breakout
        row["Risk_Percent"] = str(risk_pct)
        row["Action"]       = current_action
        row["Last_Updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log.info(
            "[Breakout]   %-20s  ltp=%-8.2f  entry=%-8.2f  sl=%-8.2f  "
            "risk=%-5.2f%%  strength=%-7.4f%%  score=%-8.4f  breakout=%s  action=%s",
            sym, ltp, entry, sl, risk_pct, strength, score, breakout, current_action,
        )
        scored.append({
            "row":      row,
            "ltp":      ltp,
            "score":    score,
            "breakout": breakout,
        })

    # ── Step 3: rank breakout symbols by score DESC ───────────────────────────
    breakout_items = sorted(
        [s for s in scored if s["breakout"] == "YES"],
        key=lambda x: x["score"],
        reverse=True,
    )
    for rank, item in enumerate(breakout_items, start=1):
        item["row"]["Rank"] = str(rank)

    # Non-breakout symbols get Rank 0
    for item in scored:
        if item["breakout"] == "NO":
            item["row"]["Rank"] = "0"

    # ── Step 4: write updated watchlist back to S3 ────────────────────────────
    updated_rows = [s["row"] for s in scored]
    save_watchlist(updated_rows)

    # ── Step 5: return enriched rows (with live ltp, NOT written to CSV) ──────
    enriched = []
    for s in scored:
        r        = dict(s["row"])
        r["ltp"] = s["ltp"]           # in-memory only
        r["score"] = s["score"]
        enriched.append(r)

    log.info(
        "[Breakout] %d symbols scanned — %d breakouts",
        len(enriched),
        sum(1 for s in scored if s["breakout"] == "YES"),
    )
    return enriched


def get_top_candidate(broker) -> Dict | None:
    """
    Return the single highest-ranked breakout candidate that has not been
    traded today.  Returns None if no eligible candidate.
    """
    from trade_s3 import count_auto_trades_today, auto_traded_symbols_today
    from trade_executor import MAX_AUTO_TRADES_PER_DAY
    log.info("[Breakout] get_top_candidate called")
    if count_auto_trades_today() >= MAX_AUTO_TRADES_PER_DAY:
        log.info("[Breakout] get_top_candidate: daily auto limit reached")
        return None
    enriched = run_breakout_engine(broker)
    traded_syms = auto_traded_symbols_today()
    candidates = [
        r for r in enriched
        if r.get("Breakout")  == "YES"
        and r.get("Action")   != "AUTO_BUYED"
        and r["Symbol"].upper() not in traded_syms
    ]
    log.info("[Breakout] get_top_candidate: eligible=%d", len(candidates))
    if not candidates:
        log.info("[Breakout] get_top_candidate: no eligible candidates")
        return None
    # Already sorted by Rank in the CSV; use score for safety
    top = min(candidates, key=lambda r: int(r.get("Rank") or 999))
    log.info("[Breakout] get_top_candidate: selected %s  rank=%s  score=%s",
             top["Symbol"], top.get("Rank"), top.get("score"))
    return min(candidates, key=lambda r: int(r.get("Rank") or 999))
