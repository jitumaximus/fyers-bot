"""23 Nov 2026 changes: divergence reference colour + minimum RSI difference; 09:19:30 OPEN 5m breakout check.
Run: venv/bin/python -m unittest discover -s tests -v   (no API calls, no .env needed)"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import config  # noqa: E402

config.DATA_DIR = Path(tempfile.mkdtemp())           # never write into a real data/ folder
import strategy  # noqa: E402
from indicators import ema, rsi  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 23)


def candles(n, tf, seed, start="09:15"):
    """Random-walk OHLC for one day (plus enough of the previous day for RSI warm-up)."""
    rng = np.random.default_rng(seed)
    ts0 = pd.Timestamp(f"{DAY} {start}", tz=IST) - pd.Timedelta(minutes=tf * 40)
    ts = [ts0 + pd.Timedelta(minutes=tf * i) for i in range(n)]
    c = 1000 + np.cumsum(rng.normal(0, 2.0, n))
    o = np.r_[1000, c[:-1]] + rng.normal(0, 0.8, n)
    h = np.maximum(o, c) + rng.random(n) * 2
    lo = np.minimum(o, c) - rng.random(n) * 2
    return pd.DataFrame({"ts": ts, "open": o.round(2), "high": h.round(2), "low": lo.round(2), "close": c.round(2),
                         "volume": 1})


def reference_div(df, side, window, gap, min_diff, ref_colour=True):
    """Independent re-implementation of the rule, written from the spec."""
    d = df.iloc[-1]
    n = len(df) - 1
    if d["ts"].date() != DAY:
        return None
    e = ema(df["close"], 5).values
    r = rsi(df["close"], 14).values
    best = None
    for k in range(n - 1 - gap, -1, -1):
        tk = df["ts"].iloc[k]
        if tk.date() != DAY or tk.time() < config.ENTRY_START or (d["ts"] - tk).total_seconds() / 60 > window:
            break
        row = df.iloc[k]
        if side == "SHORT":
            ok = row["close"] > e[k] and row["low"] > e[k] and (not ref_colour or row["close"] < row["open"])
            if ok and (best is None or row["high"] > df["high"].iloc[best]):
                best = k
        else:
            ok = row["high"] < e[k] and (not ref_colour or row["close"] > row["open"])
            if ok and (best is None or row["low"] < df["low"].iloc[best]):
                best = k
    if best is None:
        return None
    if side == "SHORT":
        hit = d["close"] < d["open"] and d["high"] > df["high"].iloc[best] and r[best] - r[n] >= min_diff
    else:
        hit = d["close"] > d["open"] and d["low"] < df["low"].iloc[best] and r[n] - r[best] >= min_diff
    return (best, round(float(abs(r[best] - r[n])), 6)) if hit else None


class Divergence(unittest.TestCase):
    def scan(self, side, tf, window, seeds=60):
        hits = 0
        old_hits = 0
        for seed in range(seeds):
            df = candles(40 + 75 if tf == 5 else 40 + 25, tf, seed)
            for m in range(30, len(df) + 1):
                part = df.iloc[:m].reset_index(drop=True)
                got = strategy._div_trigger(part, DAY, side, window)
                want = reference_div(part, side, window, config.RSI_DIV_MIN_GAP, config.RSI_DIV_MIN_RSI_DIFF)
                self.assertEqual(got is not None, want is not None, f"seed {seed} m {m}")
                old_hits += reference_div(part, side, window, config.RSI_DIV_MIN_GAP, 0, ref_colour=False) is not None
                if got:
                    hits += 1
                    self.assertIn(f"RSI diff {want[1]:.1f} (>= 3)", got["detail"])
                    peak = part.iloc[want[0]]
                    red = peak["close"] < peak["open"]
                    self.assertTrue(red if side == "SHORT" else not red)
        self.assertGreater(hits, 0, "test data produced no signals")
        self.assertGreater(old_hits, hits, "new filters removed nothing")
        return hits, old_hits

    def test_short_matches_spec(self):
        print("short", self.scan("SHORT", 5, config.RSI_DIV_SHORT_WINDOW_MIN))

    def test_long_matches_spec(self):
        print("long", self.scan("LONG", 15, config.RSI_DIV_LONG_WINDOW_MIN))

    def test_rsi_diff_threshold_is_inclusive_and_configurable(self):
        df = None
        for seed in range(200):
            d = candles(115, 5, seed)
            for m in range(30, len(d) + 1):
                part = d.iloc[:m].reset_index(drop=True)
                w = reference_div(part, "SHORT", 45, 2, 0)
                if w and 0 < w[1] < 50:
                    df, diff = part, w[1]
                    break
            if df is not None:
                break
        self.assertIsNotNone(df)
        with mock.patch.object(config, "RSI_DIV_MIN_RSI_DIFF", diff):
            self.assertIsNotNone(strategy._div_trigger(df, DAY, "SHORT", 45))
        with mock.patch.object(config, "RSI_DIV_MIN_RSI_DIFF", diff + 0.01):
            self.assertIsNone(strategy._div_trigger(df, DAY, "SHORT", 45))


class OpenBreakoutTrigger(unittest.TestCase):
    rng = {"high": 100.0, "low": 99.0, "pct": 0.0101, "wick_high": 101.0, "wick_low": 98.0}

    def test_long_short_none(self):
        s = strategy.open_breakout_trigger(self.rng, 100.5, 101.2, 100.2, 101.0)
        self.assertEqual((s["side"], s["stop_ref"], s["open_5m"]), ("LONG", 100.2, True))   # 5m low, not the wick
        self.assertTrue(s["detail"].startswith("OPEN 5m BREAKOUT"))
        s = strategy.open_breakout_trigger(self.rng, 98.8, 98.9, 98.1, 98.3)
        self.assertEqual((s["side"], s["stop_ref"]), ("SHORT", 98.9))
        self.assertIsNone(strategy.open_breakout_trigger(self.rng, 101.5, 101.6, 100.8, 101.0))  # red 5m above range
        self.assertIsNone(strategy.open_breakout_trigger(self.rng, 98.0, 98.9, 97.9, 98.5))      # green 5m below range
        self.assertIsNone(strategy.open_breakout_trigger(self.rng, 99.2, 99.8, 99.1, 99.5))      # inside the range

    def test_stop_one_tick_and_min_pct(self):
        lv = strategy.levels("LONG", 1000.0, 990.0, 1e9, 1, 0.05, True, config.OPEN_BO_MIN_STOP_PCT)
        self.assertEqual((lv["stop"], lv["stop_basis"], lv["target"]), (989.95, "candle", None))
        lv = strategy.levels("LONG", 1000.0, 999.0, 1e9, 1, 0.05, True, config.OPEN_BO_MIN_STOP_PCT)
        self.assertEqual((lv["stop"], lv["stop_basis"]), (997.0, "min 0.3%"))
        lv = strategy.levels("SHORT", 1000.0, 1001.0, 1e9, 1, 0.05, True, config.OPEN_BO_MIN_STOP_PCT)
        self.assertEqual((lv["stop"], lv["stop_basis"], lv["breakeven_at"]), (1003.0, "min 0.3%", 997.0))


# ---------------- bot-level: the 09:19:30 check with a fake client ----------------
class FakeClient:
    def __init__(self, now, bars, ltps):
        self.now, self.bars, self.ltps, self.history_calls = now, bars, ltps, []

    def ltp(self, syms):
        return {s: self.ltps[s] for s in syms if s in self.ltps}

    def quotes(self, syms):   # no open/high/low in these quotes: the 09:19:30 check reads the 5m candle instead
        return {s: {"lp": self.ltps[s]} for s in syms if s in self.ltps}

    def history(self, sym, tf, days):
        self.history_calls.append((sym, tf))
        return self.bars[sym]

    def depth_totals(self, sym):
        return 60, 40

    def margin(self, fut, qty, side):
        return 100_000.0 * qty / 100

    def token_valid(self):
        return True


def bar5(o, h, lo, c):
    return pd.DataFrame({"ts": [pd.Timestamp(f"{DAY} 09:15", tz=IST)], "open": [o], "high": [h], "low": [lo],
                         "close": [c], "volume": [1]})


class OpenCheckInBot(unittest.TestCase):
    def setUp(self):
        config.DATA_DIR = Path(tempfile.mkdtemp())
        import trader
        trader.STATE_FILE = config.DATA_DIR / "state.json"
        trader.TRADES_CSV = config.DATA_DIR / "trades.csv"
        trader.SIGNALS_CSV = config.DATA_DIR / "signals.csv"
        self.trader_mod = trader
        with mock.patch("telegram_bot.Telegram.__init__", lambda self: None), \
                mock.patch("fyers_client.FyersClient.__init__", lambda self: None):
            import bot
            self.bot_mod = bot
            self.b = bot.Bot()
        self.sent = []
        self.b.tg.send = self.sent.append
        self.b.tg.messages = lambda: []
        self.b.trader.notify = self.sent.append

    def run_at(self, hhmmss, client):
        now = datetime.combine(DAY, datetime.strptime(hhmmss, "%H:%M:%S").time(), IST)
        self.b.client = client
        self.b.trader.client = client
        self.b.universe = {s: {"future": s, "lot": 100, "tick": 0.05, "fut_tick": 0.05, "underlying": s}
                           for s in ("NSE:UP", "NSE:DOWN", "NSE:FLAT", "NSE:REDUP")}
        self.b.universe_day = DAY
        self.b.market_live = DAY
        self.b.next_arm_refresh = now + timedelta(hours=1)
        self.b.trader.state["day"] = str(DAY)
        self.trader_mod.CANDIDATES_CSV = config.DATA_DIR / "candidates.csv"
        rng = {"high": 1000.0, "low": 995.0, "pct": 0.005, "wick_high": 1010.0, "wick_low": 980.0,
               "from": "x", "to": "y"}
        self.b.ranges = {s: dict(rng) for s in self.b.universe}
        with mock.patch.object(self.bot_mod, "now_ist", lambda: now):
            self.b.step()
        return now

    def client(self):
        # first 5m moves must be 1-2% since 25 Nov (OPEN_BO_MIN/MAX_MOVE_PCT)
        bars = {"NSE:UP": bar5(993.0, 1006.0, 992.5, 1005.0),         # green +1.21%, above range: stop 992.45 (1.25%)
                "NSE:DOWN": bar5(1004.0, 1004.5, 990.0, 992.0),        # red -1.20%, below: stop 1004.55 (1.27%)
                "NSE:FLAT": bar5(997.0, 998.0, 996.0, 997.5),
                "NSE:REDUP": bar5(1004.0, 1004.5, 1001.0, 1002.0)}     # above the range but red: no entry
        ltps = {"NSE:UP": 1005.0, "NSE:DOWN": 992.0, "NSE:FLAT": 997.5, "NSE:REDUP": 1002.0}
        return FakeClient(None, bars, ltps)

    def test_entries_at_091930(self):
        c = self.client()
        self.run_at("09:19:29", c)
        self.assertEqual(self.b.trader.positions(), [])            # not yet
        self.run_at("09:19:30", c)
        pos = dict(self.b.trader.positions())
        bo, bd = pos["1H BREAKOUT"], pos["1H BREAKDOWN"]
        self.assertEqual((bo["spot"], bo["side"], bo["stop"], bo["target"], bo["trail"]),
                         ("NSE:UP", "LONG", 992.45, None, True))
        self.assertEqual(bo["stop_basis"], "candle")
        self.assertIn("OPEN 5m BREAKOUT", bo["setup"])
        self.assertEqual((bd["spot"], bd["side"], bd["stop"], bd["stop_basis"], bd["target"]),
                         ("NSE:DOWN", "SHORT", 1004.55, "candle", None))
        self.assertIn("first 5m move 1.20%", bd["setup"])
        sig = self.trader_mod.SIGNALS_CSV.read_text()
        self.assertIn("TRIGGER 1H BREAKOUT OPEN 5m", sig)
        self.assertIn("TRIGGER 1H BREAKDOWN OPEN 5m", sig)
        self.assertEqual(self.b.open_bo_day, DAY)
        n = len(c.history_calls)
        self.run_at("09:19:45", c)                                  # only once a day
        self.assertEqual(c.history_calls[n:], [])

    def test_breakeven_then_trail_no_target(self):
        c = self.client()
        self.run_at("09:19:30", c)
        tr = self.b.trader
        tr.check("1H BREAKOUT", 1017.6, 1017.6)                     # 1R = 12.55 -> 1017.55: breakeven
        p = tr.book("1H BREAKOUT")["position"]
        self.assertEqual((p["stop"], p["at_breakeven"]), (1005.0, True))
        tr.check("1H BREAKOUT", 1042.7, 1042.7)                     # 3R -> stop at 2R = 1030.1
        self.assertEqual((p["stop"], p["trail_r"]), (1030.1, 3))
        tr.check("1H BREAKOUT", 1060.0, 1060.0)                     # well past 2R: no target exit
        self.assertIsNotNone(tr.book("1H BREAKOUT")["position"])

    def test_skipped_when_first_seen_after_0920(self):
        c = self.client()
        self.run_at("09:20:01", c)
        self.assertEqual(self.b.trader.positions(), [])
        self.assertEqual(self.b.open_bo_day, DAY)

    def test_respects_can_enter(self):
        self.b.trader.state["paused"] = True
        c = self.client()
        self.run_at("09:19:30", c)
        self.assertEqual(self.b.trader.positions(), [])


if __name__ == "__main__":
    unittest.main()
