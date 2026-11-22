"""Thin wrapper over the Fyers API v3: login, symbols, candles, quotes, margin, orders."""
import io
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from fyers_apiv3 import fyersModel

import config

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("fyers")
TOKEN_FILE = config.DATA_DIR / "token.json"
SYMBOL_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
CASH_MASTER_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
SPAN_MARGIN_URL = "https://api.fyers.in/api/v2/span_margin"
AUTH_ERROR_CODES = {-8, -15, -16, -17}


class AuthError(Exception):
    pass


class RateLimiter:
    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            delay = self.last + self.gap - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()


def now_ist():
    return datetime.now(IST)


def _token_boundary(now):
    """Fyers tokens expire daily at 6:30 AM IST."""
    b = now.replace(hour=6, minute=30, second=0, microsecond=0)
    return b if now >= b else b - timedelta(days=1)


def target_month(today: date):
    """Before the 22nd: this month's contract. From the 22nd: next month's."""
    if today.day < config.CONTRACT_SWITCH_DAY:
        return today.year, today.month
    return (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)


def select_contracts(df: pd.DataFrame, today: date) -> dict:
    """From the raw NSE_FO symbol master, returns {symbol: {underlying, lot, tick, expiry}}
    with one future per underlying, expiring in the target month."""
    df = df[pd.to_numeric(df[3], errors="coerce").notna()]  # drop any header row
    df = df[(df[16].astype(str) == "XX") & df[9].astype(str).str.endswith("FUT")].copy()
    df["expiry"] = pd.to_datetime(df[8].astype("int64"), unit="s", utc=True).dt.tz_convert(IST).dt.date
    year, month = target_month(today)
    df = df[df["expiry"].apply(lambda d: d.year == year and d.month == month and d >= today)]

    if not (config.INCLUDE_INDICES and config.SIGNAL_SOURCE == "FUTURES"):
        df = df[~df[13].isin(config.INDEX_UNDERLYINGS)]
    if config.UNIVERSE != "ALL":
        wanted = {u.strip() for u in config.UNIVERSE.split(",")}
        df = df[df[13].isin(wanted)]

    df = df.sort_values("expiry").groupby(13, as_index=False).first()
    return {r[9]: {"underlying": r[13], "lot": int(r[3]), "tick": float(r[4]), "expiry": r["expiry"]}
            for _, r in df.iterrows()}


def build_universe(fo: pd.DataFrame, cm: pd.DataFrame, today: date) -> dict:
    """{scan_symbol: {underlying, future, lot, fut_tick, tick, expiry}}.
    FUTURES mode: the scan symbol IS the future (stocks and, if enabled, indices) - everything runs on its price.
    SPOT mode: the scan symbol is the stock's EQ listing and only orders go to the future (indices excluded)."""
    futures = select_contracts(fo, today)
    if config.SIGNAL_SOURCE == "FUTURES":
        return {fut: {"underlying": f["underlying"], "future": fut, "lot": f["lot"], "fut_tick": f["tick"],
                      "tick": f["tick"], "expiry": f["expiry"]} for fut, f in futures.items()}
    cm = cm[pd.to_numeric(cm[4], errors="coerce").notna()]
    cm = cm[cm[9].astype(str).str.endswith("-EQ")]
    spot_by_underlying = {str(r[13]): (str(r[9]), float(r[4])) for _, r in cm.iterrows()}
    out = {}
    for fut, f in futures.items():
        spot = spot_by_underlying.get(f["underlying"])
        if not spot:
            continue  # no cash listing found (e.g. an index) -> skip
        out[spot[0]] = {"underlying": f["underlying"], "future": fut, "lot": f["lot"], "fut_tick": f["tick"],
                        "tick": spot[1], "expiry": f["expiry"]}
    return out


class FyersClient:
    def __init__(self):
        self.token = None
        self.issued = None
        self.fyers = None
        self.limiter = RateLimiter(config.API_CALLS_PER_SEC)
        self._load_token()

    # ---------------- login ----------------
    def _session(self):
        return fyersModel.SessionModel(
            client_id=config.FYERS_APP_ID, secret_key=config.FYERS_SECRET,
            redirect_uri=config.FYERS_REDIRECT_URI, response_type="code",
            grant_type="authorization_code", state="bot")

    def login_url(self):
        return self._session().generate_authcode()

    def complete_login(self, text):
        """Accepts the full redirect URL (or just the auth_code) pasted from the phone."""
        text = text.strip()
        code = parse_qs(urlparse(text).query).get("auth_code", [None])[0] if "auth_code" in text else text
        if not code:
            raise ValueError("No auth_code found in what you pasted.")
        s = self._session()
        s.set_token(code)
        resp = s.generate_token()
        token = resp.get("access_token") if isinstance(resp, dict) else None
        if not token:
            raise ValueError(f"Login failed: {resp}")
        self.token, self.issued = token, now_ist()
        TOKEN_FILE.write_text(json.dumps({"token": token, "issued": self.issued.isoformat()}))
        self._init_model()

    def _load_token(self):
        if TOKEN_FILE.exists():
            d = json.loads(TOKEN_FILE.read_text())
            self.token, self.issued = d["token"], datetime.fromisoformat(d["issued"])
            if self.token_valid():
                self._init_model()

    def _init_model(self):
        self.fyers = fyersModel.FyersModel(client_id=config.FYERS_APP_ID, token=self.token,
                                           is_async=False, log_path=str(config.DATA_DIR))

    def token_valid(self):
        return bool(self.token and self.issued and self.issued >= _token_boundary(now_ist()))

    def _check(self, resp):
        if isinstance(resp, dict) and resp.get("code") in AUTH_ERROR_CODES:
            self.token = None
            raise AuthError(resp.get("message", "token expired"))
        return resp

    # ---------------- symbols ----------------
    def load_universe(self, today: date) -> dict:
        fo = pd.read_csv(io.StringIO(requests.get(SYMBOL_MASTER_URL, timeout=60).text), header=None, low_memory=False)
        cm = pd.read_csv(io.StringIO(requests.get(CASH_MASTER_URL, timeout=60).text), header=None, low_memory=False)
        return build_universe(fo, cm, today)

    # ---------------- market data ----------------
    def history(self, symbol, tf_minutes, days):
        self.limiter.wait()
        now = now_ist()
        data = {"symbol": symbol, "resolution": str(tf_minutes), "date_format": "0",
                "range_from": str(int((now - timedelta(days=min(days, 99))).timestamp())),
                "range_to": str(int(now.timestamp())), "cont_flag": "1"}
        resp = self._check(self.fyers.history(data=data))
        if resp.get("s") != "ok" or not resp.get("candles"):
            if resp.get("s") != "ok":
                log.warning("history %s %s: %s", symbol, tf_minutes, resp.get("message"))
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(resp["candles"], columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(IST)
        return df

    def ltp(self, symbols):
        symbols = list(dict.fromkeys(symbols))      # in FUTURES mode scan symbol and future are the same
        out = {}
        for i in range(0, len(symbols), 50):
            self.limiter.wait()
            resp = self._check(self.fyers.quotes(data={"symbols": ",".join(symbols[i:i + 50])}))
            for q in resp.get("d", []) or []:
                lp = (q.get("v") or {}).get("lp")
                if lp:
                    out[q["n"]] = float(lp)
        return out

    def depth_totals(self, symbol):
        """(total buy qty, total sell qty) from market depth."""
        self.limiter.wait()
        resp = self._check(self.fyers.depth(data={"symbol": symbol, "ohlcv_flag": "1"}))
        d = (resp.get("d") or {}).get(symbol) or {}
        return float(d.get("totalbuyqty") or 0), float(d.get("totalsellqty") or 0)

    def margin(self, symbol, qty, side):
        """SPAN + exposure margin for one order (INR)."""
        self.limiter.wait()
        body = {"data": [{"symbol": symbol, "qty": qty, "side": 1 if side == "LONG" else -1, "type": 2,
                          "productType": "INTRADAY", "limitPrice": 0.0, "stopLoss": 0.0}]}
        r = requests.post(SPAN_MARGIN_URL, json=body, timeout=15,
                          headers={"Authorization": f"{config.FYERS_APP_ID}:{self.token}"})
        resp = self._check(r.json())
        if resp.get("s") != "ok":
            raise RuntimeError(f"margin error: {resp.get('message')}")
        return float(resp["data"]["total"])

    # ---------------- orders (live mode only) ----------------
    def place_order(self, symbol, qty, side, order_type, limit_price=0.0, stop_price=0.0):
        """side: 1 buy / -1 sell. order_type: 1 limit, 2 market, 3 SL-M."""
        self.limiter.wait()
        data = {"symbol": symbol, "qty": qty, "type": order_type, "side": side, "productType": "INTRADAY",
                "limitPrice": limit_price, "stopPrice": stop_price, "validity": "DAY", "disclosedQty": 0,
                "offlineOrder": False, "orderTag": config.ORDER_TAG, "isSliceOrder": False}
        resp = self._check(self.fyers.place_order(data=data))
        if resp.get("s") != "ok":
            raise RuntimeError(f"order rejected: {resp.get('message')}")
        return resp["id"]

    def modify_stop(self, order_id, stop_price):
        """Change the trigger price of a resting SL-M order."""
        self.limiter.wait()
        resp = self._check(self.fyers.modify_order(data={"id": order_id, "type": 3, "stopPrice": stop_price}))
        if resp.get("s") != "ok":
            raise RuntimeError(resp.get("message"))
        return resp

    def cancel_order(self, order_id):
        self.limiter.wait()
        return self._check(self.fyers.cancel_order(data={"id": order_id}))

    def order_status(self, order_id):
        """Returns (status, traded_price). Fyers status 2 = filled, 1 = cancelled, 5 = rejected, 6 = pending."""
        self.limiter.wait()
        resp = self._check(self.fyers.orderbook())
        for o in resp.get("orderBook", []) or []:
            if str(o.get("id")) == str(order_id):
                return int(o.get("status", 0)), float(o.get("tradedPrice") or 0)
        return None, 0.0
