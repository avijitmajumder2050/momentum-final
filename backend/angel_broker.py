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
SESSION_TTL  = 6 * 3600
BULK_BATCH   = 50
GTT_DAYS     = 365
TOKEN_S3_KEY = "angel/angel_tokens_dump_margin.csv"


class AngelBroker:

    def __init__(self) -> None:
        self.api_key     = os.getenv("apiKey",  "")
        self.totp_secret = os.getenv("totpKey", "")
        self.client_id   = os.getenv("userid",  "")
        self.password    = os.getenv("pin",      "")
        self._obj: Optional[SmartConnect] = None
        self._lock       = threading.Lock()
        self._last_login = 0.0

        self._ssm = boto3.client("ssm", region_name=os.getenv("AWS_REGION","ap-south-1"))
        self._s3  = boto3.client("s3",  region_name=os.getenv("AWS_REGION","ap-south-1"))

        self.bucket    = (os.getenv("S3_BUCKET")
                          or self._ssm_param("/momentum-watchlist/S3_BUCKET"))
        self.token_map: dict = {}
        self.reload_token_map()
        self._login()

    # ── SSM ───────────────────────────────────────────────────────────────────
    def _ssm_param(self, name: str) -> str:
        try:
            return self._ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:
            log.error("[SSM] %s → %s", name, e); return ""

    # ── Token map ─────────────────────────────────────────────────────────────
    def reload_token_map(self) -> None:
        """Download angel_tokens_dump_margin.csv from S3 and build lookup dict."""
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=TOKEN_S3_KEY)
            df  = pd.read_csv(io.BytesIO(obj["Body"].read()))
            df.columns   = [c.strip().lower() for c in df.columns]
            df["symbol"] = df["symbol"].str.upper().str.strip()
            self.token_map = {
                row["symbol"]: {"token": str(row["token"]),
                                "margin": float(row.get("margin", 1))}
                for _, row in df.iterrows()
            }
            log.info("[TokenMap] Loaded %d symbols", len(self.token_map))
        except Exception as e:
            log.error("[TokenMap] Load failed: %s", e)

    def get_token(self, symbol: str) -> Optional[str]:
        e = self.token_map.get(symbol.upper())
        if e: return e["token"]
        log.warning("[TokenMap] Not found: %s", symbol); return None

    def get_margin(self, symbol: str) -> float:
        return self.token_map.get(symbol.upper(), {}).get("margin", 1.0)

    def get_product_type(self, symbol: str) -> str:
        """DELIVERY if margin==1, else MARGIN."""
        return "DELIVERY" if self.get_margin(symbol) == 1.0 else "MARGIN"

    # ── Session ───────────────────────────────────────────────────────────────
    def _login(self) -> None:
        totp = pyotp.TOTP(self.totp_secret).now()
        obj  = SmartConnect(api_key=self.api_key)
        d    = obj.generateSession(self.client_id, self.password, totp)
        if not d.get("status"):
            raise RuntimeError(f"Login failed: {d.get('message')}")
        self._obj = obj; self._last_login = time.time()
        log.info("[Angel] Session established")

    def _ensure_session(self) -> None:
        with self._lock:
            if time.time() - self._last_login > SESSION_TTL:
                self._login()

    def _call(self, fn, *args, **kwargs):
        self._ensure_session()
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if any(k in str(e).lower() for k in ("unauthorized","session","jwt")):
                self._login(); return fn(*args, **kwargs)
            raise

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
        }
        log.info("[Order] %s %d×%s @ %.2f [%s]",
                 transaction.upper(), qty, trading_symbol, price, product_type)
        r = self._call(self._obj.placeOrder, params)
        if r.get("status"):
            oid = r.get("data",{}).get("orderid","")
            log.info("[Order] OK — order_id=%s", oid)
            return {"status":"success","order_id":oid}
        msg = r.get("message","Unknown error")
        log.warning("[Order] FAILED: %s", msg)
        return {"status":"error","message":msg}

    def get_order_status(self, order_id: str) -> str:
        """
        Return the current status string for an order_id.
        Returns "COMPLETE","PENDING","REJECTED","CANCELLED","UNKNOWN".
        """
        try:
            orders = self._call(self._obj.orderBook)
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
            "price":"0","squareoff":"0","stoploss":"0","quantity":str(qty),
        }
        r = self._call(self._obj.placeOrder, params)
        if r.get("status"):
            return {"status":"success","order_id":r.get("data",{}).get("orderid","")}
        return {"status":"error","message":r.get("message","")}

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

        try:
            r = self._call(self._obj.gttCreateRule, params)
            if r.get("status"):
                gid = (r.get("data") or {}).get("id", "")
                log.info("[GTT] Created OCO id=%s", gid)
                return {"status": "success", "gtt_id": gid}

            return {"status": "error", "message": r.get("message", "")}

        except Exception as e:
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

        try:
            r = self._call(self._obj.gttModifyRule, params)
            if r.get("status"):
                return {"status": "success"}

            return {"status": "error", "message": r.get("message", "")}

        except Exception as e:
            return {"status": "error", "message": str(e)}

    def cancel_gtt(self, gtt_id: str, trading_symbol: str, token: str) -> dict:
        """Cancel a GTT rule by ID."""
        try:
            r = self._call(self._obj.gttCancelRule,
                           {"id":str(gtt_id),"tradingsymbol":trading_symbol,
                            "symboltoken":str(token)})
            if r.get("status"): return {"status":"success"}
            return {"status":"error","message":r.get("message","")}
        except Exception as e:
            return {"status":"error","message":str(e)}

    # ── Positions / funds ─────────────────────────────────────────────────────
    def get_positions(self) -> list:
        d = self._call(self._obj.position)
        return (d.get("data") or []) if d.get("status") else []

    def get_funds(self) -> dict:
        try:
            d = self._call(self._obj.rmsLimit)
            if d.get("status") and d.get("data"):
                return {"status":"success",
                        "available_balance":float(d["data"].get("net",0) or 0)}
        except Exception as e:
            log.warning("[Funds] %s", e)
        return {"status":"error","available_balance":0.0}

    def get_today_pnl(self) -> dict:
        realized = unrealized = 0.0
        for pos in (self.get_positions() or []):
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
