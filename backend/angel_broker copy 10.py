"""
angel_broker.py
===============
Angel One SmartAPI wrapper.

Token lookup : S3 CSV  (angel/angel_tokens_dump_margin.csv)
Product type : DYNAMIC — margin==1 → DELIVERY, margin>1 → MARGIN
GTT          : create + MODIFY (for trailing SL updates)

CSV schema: symbol, token, margin
  RELIANCE-EQ, 3045, 5
  INFY-EQ,     1594, 1
"""
import io, os, time, logging, threading
from typing import Optional
import pyotp, boto3
import pandas as pd
from SmartApi import SmartConnect


log          = logging.getLogger(__name__)

BULK_BATCH   = 50
GTT_DAYS     = 365
TOKEN_S3_KEY = "angel/angel_tokens_dump_margin.csv"
_login_lock = threading.Lock()
_last_login_ts = 0
LOGIN_COOLDOWN = 2   # safer than 1 sec
def _div(label: str = "") -> None:
    pad = max(0, 48 - len(label))
    if label:
        log.info("[Broker] ── %s %s", label, "─" * pad)
    else:
        log.info("[Broker] %s", "─" * 52)


class AngelBroker:

    def __init__(self) -> None:
        self.api_key     = os.getenv("apiKey",  "")
        self.totp_secret = os.getenv("totpKey", "")
        self.client_id   = os.getenv("userid",  "")
        self.password    = os.getenv("pin",      "")
        self._obj: Optional[SmartConnect] = None
        self._lock       = threading.Lock()
        self._last_login = 0.0
        self._api_lock = threading.Lock()
        self._last_api_call = 0
        self._api_gap = 1.1   # 1 sec + buffer

        self._ssm = boto3.client("ssm", region_name=os.getenv("AWS_REGION","ap-south-1"))
        self._s3  = boto3.client("s3",  region_name=os.getenv("AWS_REGION","ap-south-1"))

        self.bucket    = (os.getenv("S3_BUCKET")
                          or self._ssm_param("/momentum-watchlist/S3_BUCKET"))
        self.token_map: dict = {}
        self.reload_token_map()
        self._login()
        self._funds_cache = None
        self._funds_last  = 0
        self._funds_ttl   = 5   # seconds (tune: 5–15 sec)

    # ── SSM ───────────────────────────────────────────────────────────────────
    def _ssm_param(self, name: str) -> str:
        try:
            return self._ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:
            log.error("[SSM] %s → %s", name, e); return ""

    # ── Token map ─────────────────────────────────────────────────────────────
    def reload_token_map(self) -> None:
        _div("TOKEN MAP LOAD")
        log.info("[TokenMap] bucket=%s  key=%s", self.bucket, TOKEN_S3_KEY)
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=TOKEN_S3_KEY)
            df  = pd.read_csv(io.BytesIO(obj["Body"].read()))

            # ✅ Normalize everything
            df.columns = [c.strip().lower() for c in df.columns]

            df["symbol"] = (
                df["symbol"]
                .astype(str)
                .str.upper()
                .str.strip()
                .str.replace(r"\s+", "", regex=True)   # remove hidden spaces
            )

            df["token"] = df["token"].astype(str).str.strip()

            self.token_map = {
                row["symbol"]: {
                "token": row["token"],
                "margin": float(row.get("margin", 1))
                }
            for _, row in df.iterrows()
            }

            log.info("[TokenMap] Loaded %d symbols", len(self.token_map))

            # 🔍 DEBUG sample
            sample = list(self.token_map.keys())[:10]
            log.info("[TokenMap] Sample keys: %s", sample)

        except Exception as e:
            log.error("[TokenMap] Load failed: %s", e)

    def get_token(self, symbol: str) -> Optional[str]:
        clean_symbol = (
            str(symbol)
            .upper()
            .strip()
            .replace(" ", "")
        )

        e = self.token_map.get(clean_symbol)

        if e:
            return e["token"]

        log.warning(
            "[TokenMap] Not found: raw='%s' clean='%s'",
            symbol, clean_symbol
        )

        return None

    def get_margin(self, symbol: str) -> float:
        m = self.token_map.get(symbol.upper(), {}).get("margin", 1.0) 
        log.debug("[TokenMap] margin(%s) = %.1f×", symbol, m)
        return self.token_map.get(symbol.upper(), {}).get("margin", 1.0)

    def get_product_type(self, symbol: str) -> str:
        """DELIVERY if margin==1, else MARGIN."""
        return "DELIVERY" if self.get_margin(symbol) == 1.0 else "MARGIN"

    # ── Session ───────────────────────────────────────────────────────────────
    def _login(self) -> None:
        global _last_login_ts

        with _login_lock:

            if self._obj:
                return

            now = time.time()
            wait = LOGIN_COOLDOWN - (now - _last_login_ts)
            if wait > 0:
                log.warning("[Angel] Rate-limit sleep %.2fs", wait)
                time.sleep(wait)

            _div("SESSION LOGIN")
            log.info("[Session] client_id=%s  api_key=%s****",
                     self.client_id, self.api_key[:4] if self.api_key else "??")
            totp = pyotp.TOTP(self.totp_secret).now()
            obj  = SmartConnect(api_key=self.api_key)

            try:
                d = obj.generateSession(self.client_id, self.password, totp)
            except Exception as e:
                log.error("[Session] generateSession EXCEPTION: %s", e)
                raise RuntimeError(f"[Angel] Login exception: {e}")   # ✅ FIX

            _last_login_ts = time.time()

            if not d.get("status"):
                raise RuntimeError(f"[Angel] Login failed: {d.get('message')}")  # ✅ FIX

            self._obj = obj
            self._last_login = time.time()

            log.info("[Angel] Session established")

    # ─────────────────────────────────────────
    # SAFE API CALL (FIXED)
    # ─────────────────────────────────────────
    def _ensure_session(self) -> None:
        """
        Ensure session exists before API call.
        Thread-safe + prevents multiple logins.
        """
        if self._obj:
            return

        with self._lock:
            if self._obj:   # double-check
                return

            self._login()

            if not self._obj:
                raise RuntimeError("Login failed — no session available")
            

    def _call(self, fn, *args, **kwargs):
        retries = 3
        delay   = 1

        for attempt in range(retries):
            try:
                self._ensure_session()

                # ✅ GLOBAL RATE LIMIT
                with self._api_lock:
                    now = time.time()
                    wait = self._api_gap - (now - self._last_api_call)
                    if wait > 0:
                        log.debug("[RateLimit] sleep %.2fs", wait)
                        time.sleep(wait)

                    result = fn(*args, **kwargs)
                    self._last_api_call = time.time()

                return result

            except Exception as e:
                msg = str(e).lower()

                # 🔁 session expired
                if any(k in msg for k in ("unauthorized","session","jwt")):
                    log.warning("[Session] expired → relogin")
                    self._obj = None
                    continue

                # 🚫 rate limit retry
                if "access denied" in msg or "rate" in msg:
                    log.warning("[RateLimit] retry %d", attempt+1)
                    time.sleep(delay)
                    delay *= 2
                    continue

                log.error("[API] unexpected error: %s", e)
                break

        raise RuntimeError("API failed after retries")
            
    # ── LTP ───────────────────────────────────────────────────────────────────
    def get_ltp(self, exchange: str, trading_symbol: str, token: str) -> float:
        r = self._call(self._obj.ltpData, exchange, trading_symbol, token)
        if r.get("status"): return float(r["data"]["ltp"])
        raise RuntimeError(f"LTP failed: {trading_symbol} — {r.get('message')}")

    def get_bulk_ltp(self, instruments: list) -> dict:
        """
        instruments = [{"symboltoken":"3045","tradingsymbol":"RELIANCE-EQ"}, ...]
        Returns {tradingsymbol: {ltp, open, high, low, close, pct_change}}
        """
        result = {}
        tokens = [str(i["symboltoken"]) for i in instruments]
        for s in range(0, len(tokens), BULK_BATCH):
            batch = tokens[s:s+BULK_BATCH]
            try:
                resp = self._call(self._obj.getMarketData, mode="FULL",
                                  exchangeTokens={"NSE": batch})
                if resp.get("status") and resp.get("data"):
                    for item in resp["data"].get("fetched", []):
                        sym = item.get("tradingSymbol","")
                        ltp = float(item.get("ltp",  0) or 0)
                        cls = float(item.get("close",ltp) or ltp)
                        result[sym] = {
                            "ltp":        ltp,
                            "open":       float(item.get("open",  ltp)),
                            "high":       float(item.get("high",  ltp)),
                            "low":        float(item.get("low",   ltp)),
                            "close":      cls,
                            "pct_change": round((ltp-cls)/cls*100,2) if cls else 0,
                        }
            except Exception as e:
                log.warning("[BulkLTP] batch error: %s", e)
            time.sleep(0.3)
        return result

    # ── LTP WITH RETRY (FIXED + PRODUCTION READY) ─────────────────────────
    def get_ltp_with_retry(
        self,
        exchange: str,
        trading_symbol: str,
        token: str,
        retries: int = 5,
        base_delay: float = 1.5
    ) -> Optional[float]:

        start = time.time()

        for attempt in range(1, retries + 1):
            try:
                ltp = float(self.get_ltp(exchange, trading_symbol, token))

                log.info(
                    "[LTP] SUCCESS symbol=%s ltp=%.2f attempt=%d latency=%.2fs",
                    trading_symbol, ltp, attempt, time.time() - start
                )

                return ltp

            except Exception as e:
                log.warning(
                    "[LTP] RETRY symbol=%s attempt=%d/%d err=%s",
                    trading_symbol, attempt, retries, e
                )

                if attempt < retries:
                    sleep_time = base_delay * attempt   # exponential backoff
                    log.debug(
                        "[LTP] sleeping %.2fs before retry symbol=%s",
                        sleep_time, trading_symbol
                    )
                    time.sleep(sleep_time)

        # 🔴 HARD FAILURE
        log.error(
            "[LTP] FAILED symbol=%s retries=%d total_time=%.2fs",
            trading_symbol, retries, time.time() - start
        )

        return None
    # ── Orders ────────────────────────────────────────────────────────────────
    def place_limit_order(
        self,
        trading_symbol: str,
        token:          str,
        qty:            int,
        price:          float,
        transaction:    str = "BUY",
        exchange:       str = "NSE",
    ) -> dict:
        """
        Place a LIMIT order.  Product type derived from token CSV.
        Returns {"status":"success","order_id":"..."} or {"status":"error","message":"..."}
        """
        product_type = self.get_product_type(trading_symbol)
        params = {
            "variety":         "NORMAL",
            "tradingsymbol":   trading_symbol,
            "symboltoken":     str(token),
            "transactiontype": transaction.upper(),
            "exchange":        exchange,
            "ordertype":       "LIMIT",
            "producttype":     product_type,
            "duration":        "DAY",
            "price":           str(round(price, 2)),
            "squareoff":       "0",
            "stoploss":        "0",
            "quantity":        str(qty),
            "scripconsent":    "yes",  # <--- Added this line
        }
        _div("LIMIT ORDER PAYLOAD")
        for k, v in params.items():
            log.info("[Order]   %-20s = %s", k, v)
        log.info("[Order] %s %d×%s @ %.2f [%s]",
                 transaction.upper(), qty, trading_symbol, price, product_type)
        r = self._call(self._obj.placeOrder, params)
        # SmartAPI returns orderid string directly
        if r:
            oid = str(r)
            log.info("[Order] OK — order_id=%s", oid)
            return {"status":"success","order_id":oid}
        
        log.warning("[Order] FAILED: empty response")
        return {"status": "error", "message": "empty response"}

    def get_order_status(self, order_id: str) -> str:
        """
        Return the current status string for an order_id.
        Returns "COMPLETE","PENDING","REJECTED","CANCELLED","UNKNOWN".
        """
        try:
            log.debug("[OrderStatus] querying order_id=%s", order_id) 
            orders = self._call(self._obj.orderBook)
            log.debug("[OrderStatus] orderBook raw=%s", orders)
            if orders.get("status") and orders.get("data"):
                for o in orders["data"]:
                    if str(o.get("orderid","")) == str(order_id):
                        return o.get("orderstatus","UNKNOWN").upper()
        except Exception as e:
            log.warning("[OrderStatus] %s", e)
        return "UNKNOWN"

    def place_sell_market_order(
        self,
        trading_symbol: str,
        token:          str,
        qty:            int,
        exchange:       str = "NSE",
    ) -> dict:
        """Market SELL for manual / time exit."""
        product_type = self.get_product_type(trading_symbol)
        params = {
            "variety":"NORMAL","tradingsymbol":trading_symbol,
            "symboltoken":str(token),"transactiontype":"SELL",
            "exchange":exchange,"ordertype":"MARKET",
            "producttype":product_type,"duration":"DAY",
            "price":"0","squareoff":"0","stoploss":"0","quantity":str(qty),"scripconsent":   "yes",
        }
        _div("SELL MARKET ORDER PAYLOAD")
        for k, v in params.items():
            log.info("[SellOrder]   %-20s = %s", k, v)
        r = self._call(self._obj.placeOrder, params)
        _div("SELL MARKET ORDER RESPONSE")
        log.info("[SellOrder]   raw_response = %s", r)

        if r:
            oid = str(r)
            log.info("[Order] OK — order_id=%s", oid)
            return {"status":"success","order_id":oid}
            
        log.warning("[Order] FAILED: empty response")
        return {"status": "error", "message": "empty response"}

    # ── GTT ───────────────────────────────────────────────────────────────────
    def place_gtt_order(
        self,
        trading_symbol: str,
        token: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        target_price: float,
        exchange: str = "NSE",
    ) -> dict:

        product_type = self.get_product_type(trading_symbol)

        params = {
            "tradingsymbol": trading_symbol,
            "symboltoken": str(token),
            "exchange": exchange,
            "producttype": product_type,
            "transactiontype": "SELL",
            "qty": str(qty),

            # 🔥 IMPORTANT
            "gttType": "OCO",

            # Target leg
            "price": str(round(target_price, 2)),
            "triggerprice": str(round(target_price, 2)),

            # SL leg (correct fields)
            "stoplossprice": str(round(sl_price, 2)),
            "stoplosstriggerprice": str(round(sl_price, 2)),
        }

        _div("GTT CREATE PAYLOAD")
        for k, v in params.items():
            log.info("[GTT]   %-24s = %s", k, v)
        log.info("[GTT]   risk_reward  = 1 : %.2f",
                 (target_price - entry_price) / max(entry_price - sl_price, 0.01))
        try:
            r = self._call(self._obj.gttCreateRule, params)
            _div("GTT CREATE RESPONSE")
            log.info("[GTT]   raw_response = %s", r)
            if r:
                gid = str(r)
                log.info("[GTT] Created OCO id=%s", gid)
                return {"status": "success", "gtt_id": gid}
            log.warning("[GTT]   status = FAILED (empty response)")
            return {"status": "error", "message": "empty response"}

        except Exception as e:
            log.error("[GTT]   EXCEPTION: %s", e)
            return {"status": "error", "message": str(e)}

    def modify_gtt_sl(
        self,
        gtt_id: str,
        trading_symbol: str,
        token: str,
        qty: int,
        new_sl: float,
        target_price: float,
        exchange: str = "NSE",
    ) -> dict:

        product_type = self.get_product_type(trading_symbol)

        params = {
            "id": str(gtt_id),
            "tradingsymbol": trading_symbol,
            "symboltoken": str(token),
            "exchange": exchange,
            "producttype": product_type,
            "transactiontype": "SELL",
            "qty": str(qty),

            "gttType": "OCO",

            # keep target unchanged
            "price": str(round(target_price, 2)),
            "triggerprice": str(round(target_price, 2)),

            # updated SL
            "stoplossprice": str(round(new_sl, 2)),
            "stoplosstriggerprice": str(round(new_sl, 2)),
        }

        _div("GTT MODIFY PAYLOAD")
        for k, v in params.items():
            log.info("[GTTModify]   %-24s = %s", k, v)
        try:
            r = self._call(self._obj.gttModifyRule, params)
            _div("GTT MODIFY RESPONSE")
            log.info("[GTTModify]   raw_response = %s", r)
            if r:
                log.info("[GTTModify]   status = success  gtt_id = %s", str(r))
                return {"status": "success", "gtt_id": str(r)}

            log.warning("[GTTModify]   status = FAILED (empty response)")
            return {"status": "error", "message": "empty response"}

        except Exception as e:
            log.error("[GTTModify]   EXCEPTION: %s", e)
            return {"status": "error", "message": str(e)}

    def cancel_gtt(self, gtt_id: str, trading_symbol: str, token: str) -> dict:
        """Cancel a GTT rule by ID."""
        payload = {"id": str(gtt_id), "tradingsymbol": trading_symbol,
                   "symboltoken": str(token)}
        log.info("[GTTCancel] payload = %s", payload)
        try:
            r = self._call(self._obj.gttCancelRule,
                           {"id":str(gtt_id),"tradingsymbol":trading_symbol,
                            "symboltoken":str(token)})
            log.info("[GTTCancel] raw_response = %s", r)
            if r.get("status"): return {"status":"success"}
            return {"status":"error","message":r.get("message","")}
        except Exception as e:
            return {"status":"error","message":str(e)}

    # ── Positions / funds ─────────────────────────────────────────────────────
    def get_positions(self) -> list:
        log.debug("[Positions] fetching all positions")
        d = self._call(self._obj.position)
        log.debug("[Positions] raw_response = %s", d)
        return (d.get("data") or []) if d.get("status") else []

    def get_funds(self) -> dict:
        now = time.time()

        # ✅ return cache if valid
        if self._funds_cache and (now - self._funds_last < self._funds_ttl):
            log.debug("[Funds] returning cached  balance=₹%.2f",
                      self._funds_cache.get("available_balance", 0))
            return self._funds_cache

        try:
            d = self._call(self._obj.rmsLimit)
            log.debug("[Funds] raw_response = %s", d)

            if d.get("status") and d.get("data"):
                result = {
                    "status": "success",
                    "available_balance": float(d["data"].get("net", 0) or 0)
                }

                # ✅ update cache
                self._funds_cache = result
                self._funds_last  = now

                return result

        except Exception as e:
            log.warning("[Funds] %s", e)

        return {"status": "error", "available_balance": 0.0}

    def get_today_pnl(self) -> dict:
        realized = unrealized = 0.0
        for pos in (self.get_positions() or []):
            log.debug("[PnL] computing from positions")
            try:
                realized   += float(pos.get("realisedprofitandloss",0) or 0)
                qty = int(pos.get("netqty",0) or 0)
                ltp = float(pos.get("ltp",0) or 0)
                avg = float(pos.get("netprice",0) or 0)
                unrealized += qty*(ltp-avg)
            except: continue
        return {"realized_pnl":round(realized,2),
                "unrealized_pnl":round(unrealized,2),
                "total_pnl":round(realized+unrealized,2)}


# ── Singleton ─────────────────────────────────────────────────────────────────
_broker: Optional[AngelBroker] = None
_lock   = threading.Lock()

def get_broker() -> AngelBroker:
    global _broker
    if _broker is None:
        with _lock:
            if _broker is None:
                _broker = AngelBroker()
    return _broker

def reset_broker() -> None:
    global _broker
    with _lock: _broker = None
