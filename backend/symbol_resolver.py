"""
symbol_resolver.py — re-resolve a stock's Angel token when NSE changes its series
==================================================================================
NSE moves stocks between series (EQ ↔ BE/BZ/…) under surveillance; each series
has its own token, so a stored token like DIACABS-EQ/18543 stops returning
quotes when the stock moves to DIACABS-BE/18545 (and back again).

Option A — heal on failure:
  When a quote fails, look the stock's base name up in Angel's scrip master.
  * Stored token still listed  → not a series change (network/rate limit) → no-op.
  * Token gone, other series listed → return the new (symbol, token) and persist
    it into the watchlist row / trade row so LTP, trailing, SL and sells work.

The 35 MB scrip master is reduced to a small NSE-equity map and cached in memory;
it is re-downloaded at most every MASTER_MAX_AGE, or early (throttled) when a
token isn't found — Angel rebuilds the master once a day before market open.
"""
import json
import logging
import re
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

SCRIP_MASTER_URL = ("https://margincalculator.angelbroking.com/OpenAPI_File/files/"
                    "OpenAPIScripMaster.json")
SERIES_PREF      = ("EQ", "BE", "BZ", "SM", "ST")   # preferred order when several exist
MASTER_MAX_AGE   = 6 * 3600                          # normal refresh interval
FORCE_MIN_GAP    = 20 * 60                           # min gap between early refreshes

_lock  = threading.Lock()
_state = {"ts": 0.0, "by_name": {}, "tokens": set()}   # by_name: NAME -> {series: (symbol, token)}


def _download() -> None:
    t0 = time.time()
    with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=60) as r:
        text = r.read().decode("utf-8")
    by_name: Dict[str, Dict[str, Tuple[str, str]]] = {}
    tokens = set()
    for m in re.finditer(r"\{[^{}]*\}", text):
        o = m.group(0)
        if '"exch_seg":"NSE"' not in o or '"instrumenttype":""' not in o:
            continue
        d = json.loads(o)
        sym = d.get("symbol", "")
        if "-" not in sym:
            continue
        series = sym.rsplit("-", 1)[1]
        if series not in SERIES_PREF:
            continue
        by_name.setdefault(d["name"].upper(), {})[series] = (sym, str(d["token"]))
        tokens.add(str(d["token"]))
    del text
    _state.update(ts=time.time(), by_name=by_name, tokens=tokens)
    log.info("[Resolver] scrip master loaded: %d NSE equity names in %.1fs",
             len(by_name), time.time() - t0)


def _ensure(force: bool = False) -> bool:
    age = time.time() - _state["ts"]
    if age < MASTER_MAX_AGE and not (force and age >= FORCE_MIN_GAP):
        return bool(_state["tokens"])
    with _lock:
        age = time.time() - _state["ts"]
        if age < MASTER_MAX_AGE and not (force and age >= FORCE_MIN_GAP):
            return bool(_state["tokens"])
        try:
            _download()
        except Exception as e:
            log.error("[Resolver] scrip master download failed: %s", e)
            _state["ts"] = time.time() - MASTER_MAX_AGE + FORCE_MIN_GAP  # back off
    return bool(_state["tokens"])


def resolve(symbol: str, token: str) -> Optional[Tuple[str, str]]:
    """
    Return (new_symbol, new_token) if the stored token is dead and the stock now
    trades under another series; None if nothing should change.
    """
    token = str(token or "")
    if not _ensure():
        return None
    if token and token in _state["tokens"]:
        return None                       # token still valid → quote failed for another reason
    if not _ensure(force=True):           # maybe today's master isn't loaded yet
        return None
    if token and token in _state["tokens"]:
        return None
    base = symbol.upper().rsplit("-", 1)[0] if "-" in symbol else symbol.upper()
    listing = _state["by_name"].get(base, {})
    for s in SERIES_PREF:
        if s in listing:
            new_sym, new_tok = listing[s]
            if (new_sym, new_tok) != (symbol, token):
                log.warning("[Resolver] %s/%s no longer listed → now %s/%s",
                            symbol, token, new_sym, new_tok)
                return new_sym, new_tok
            return None
    log.error("[Resolver] %s/%s: no NSE equity listing for %s (suspended/delisted?)",
              symbol, token, base)
    return None


def _remember_token(broker, old_sym: str, new_sym: str, new_tok: str) -> None:
    old = broker.token_map.get(old_sym.upper(), {})
    broker.token_map[new_sym.upper()] = {"token": new_tok, "margin": old.get("margin", 1.0)}


# ── Persisting heals ──────────────────────────────────────────────────────────
def heal_watchlist_row(broker, row: dict) -> bool:
    """Update a watchlist row (dict mutated + S3) if its token is dead. True if changed."""
    from watchlist_s3 import update_row
    found = resolve(row["Symbol"], row.get("Angel_Token", ""))
    if not found:
        return False
    old_sym = row["Symbol"]
    new_sym, new_tok = found
    if update_row(old_sym, {"Symbol": new_sym, "Angel_Token": new_tok}):
        row["Symbol"], row["Angel_Token"] = new_sym, new_tok
        _remember_token(broker, old_sym, new_sym, new_tok)
        log.warning("[Resolver] watchlist %s → %s/%s", old_sym, new_sym, new_tok)
        return True
    return False


def heal_trade(broker, trade: dict) -> bool:
    """Update an open trade (dict mutated + S3) if its token is dead. True if changed."""
    from trade_s3 import update_trade
    found = resolve(trade["Symbol"], trade.get("Angel_Token", ""))
    if not found:
        return False
    old_sym = trade["Symbol"]
    new_sym, new_tok = found
    if update_trade(trade["Order_ID"], {"Symbol": new_sym, "Angel_Token": new_tok}):
        trade["Symbol"], trade["Angel_Token"] = new_sym, new_tok
        _remember_token(broker, old_sym, new_sym, new_tok)
        log.warning("[Resolver] trade %s %s → %s/%s",
                    trade["Order_ID"], old_sym, new_sym, new_tok)
        return True
    return False


def trade_ltp(broker, trade: dict, retries: int = 2) -> Optional[float]:
    """LTP for an open trade; on failure re-resolve the token once and retry."""
    ltp = broker.get_ltp_with_retry("NSE", trade["Symbol"], trade["Angel_Token"], retries=retries)
    if ltp:
        return ltp
    if heal_trade(broker, trade):
        return broker.get_ltp_with_retry("NSE", trade["Symbol"], trade["Angel_Token"], retries=retries)
    return ltp
