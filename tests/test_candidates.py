"""candidates.csv rows and the 09:19:30 move band (1-2%, biggest move first)."""
import csv
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402
import config  # noqa: E402
import strategy  # noqa: E402
import trader  # noqa: E402
from fyers_client import IST  # noqa: E402

NOW = datetime(2026, 11, 26, 9, 19, 30, tzinfo=IST)


class FakeClient:
    def __init__(self, quotes):
        self.q, self.calls = quotes, []

    def quotes(self, syms):
        self.calls.append(("quotes", len(syms)))
        return {s: self.q[s] for s in syms if s in self.q}

    def ltp(self, syms):
        self.calls.append(("ltp", len(syms)))
        return {s: float(self.q[s]["lp"]) for s in syms if s in self.q}

    def depth_totals(self, sym):
        return 100.0, 100.0

    def margin(self, fut, qty, side):
        return 50_000.0 * qty / 100

    def history(self, sym, tf, days):
        self.calls.append(("history", sym))
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])


class FakeTrader:
    paper = True

    def __init__(self, busy=()):
        self.busy, self.opened = set(busy), []

    def can_enter(self, b):
        return b not in self.busy

    def balance(self, b):
        return 250_000.0

    def open(self, name, sym, *a):
        self.opened.append((name, sym))
        self.busy.add(name)

    def in_cooldown(self, *a):
        return False

    def positions(self):
        return []


def make(quotes, ranges, busy=()):
    b = bot.Bot.__new__(bot.Bot)
    b.client, b.trader, b.ranges = FakeClient(quotes), FakeTrader(busy), ranges
    b.universe = {s: {"future": s, "lot": 100, "tick": 0.05, "fut_tick": 0.05, "underlying": s} for s in ranges}
    b.tg = type("T", (), {"send": lambda *a: None, "messages": lambda *a: []})()
    b.last_poll, b.last_seen, b.arms = 0.0, {}, {}
    return b


def rng(lo, hi):
    return {"low": lo, "high": hi, "wick_low": lo - 1, "wick_high": hi + 1, "pct": (hi - lo) / lo, "from": "", "to": ""}


def q(o, h, l, lp):
    return {"lp": lp, "open_price": o, "high_price": h, "low_price": l}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._saved = trader.CANDIDATES_CSV, trader.SIGNALS_CSV, bot.log_signal, bot.now_ist
        trader.CANDIDATES_CSV, trader.SIGNALS_CSV = d / "candidates.csv", d / "signals.csv"
        bot.now_ist = lambda: NOW.replace(second=31)

    def tearDown(self):
        trader.CANDIDATES_CSV, trader.SIGNALS_CSV, bot.log_signal, bot.now_ist = self._saved
        self.tmp.cleanup()

    def rows(self):
        with trader.CANDIDATES_CSV.open() as f:
            r = list(csv.DictReader(f))
        self.assertEqual(list(r[0].keys()), trader.CAND_COLS)
        return {x["symbol"]: x for x in r}


class OpenCheck(Base):
    def test_band_and_biggest_move_first(self):
        ranges = {"A": rng(98, 100), "B": rng(98, 100), "C": rng(98, 100), "D": rng(101, 103), "E": rng(98, 100),
                  "F": rng(98, 100), "G": rng(98, 100)}
        quotes = {"A": q(100, 101.6, 99.9, 101.5),     # long, +1.5% -> ranked lower (C moved more)
                  "B": q(100, 102.6, 99.9, 102.5),     # long, +2.5% -> outside move band
                  "C": q(100, 101.9, 99.8, 101.8),     # long, +1.8% -> taken
                  "D": q(100, 100.1, 98.7, 98.8),      # short, -1.2% -> taken (own book)
                  "E": q(99, 99.5, 98.5, 99.2),        # inside the range -> no row
                  "F": q(101, 101.2, 100.2, 100.5),    # above the range but red 5m -> no setup, no row
                  "G": q(100, 100.6, 99.9, 100.5)}     # long, +0.5% -> outside move band
        b = make(quotes, ranges)
        b.check_open_breakouts(NOW)
        r = self.rows()
        self.assertEqual(set(r), {"A", "B", "C", "D", "G"})
        self.assertEqual(b.trader.opened, [("1H BREAKOUT", "C"), ("1H BREAKDOWN", "D")])
        self.assertEqual((r["C"]["taken"], r["C"]["rank"], r["C"]["reason"]), ("yes", "1", ""))
        self.assertEqual((r["A"]["taken"], r["A"]["rank"], r["A"]["reason"]), ("no", "2", "ranked lower"))
        self.assertEqual((r["B"]["reason"], r["G"]["reason"]), ("outside move band", "outside move band"))
        self.assertEqual(r["D"]["side"], "SHORT")
        self.assertEqual(float(r["C"]["move_pct"]), 1.8)
        self.assertEqual(float(r["C"]["gap_pct"]), 0.0)            # opened at the range high
        self.assertEqual(float(r["C"]["beyond_pct"]), 1.8)
        self.assertEqual(r["C"]["candle_colour"], "green")
        self.assertEqual(float(r["C"]["stop_candle"]), 99.75)     # 5m low 99.8 - 1 tick
        self.assertEqual(float(r["C"]["stop_used"]), 99.75)       # 2.0% away, inside the 10% cap
        self.assertEqual(r["C"]["size"], "5")
        self.assertAlmostEqual(float(r["D"]["move_pct"]), 1.2)
        self.assertAlmostEqual(float(r["D"]["gap_pct"]), 0.99, places=3)    # range low 101 -> open 100
        # one quote call for the whole check; candle calls only inside try_enter (trend-day check) for the entries
        self.assertEqual([c for c in b.client.calls if c[0] in ("quotes", "history")],
                         [("quotes", 7), ("history", "C"), ("history", "D")])

    def test_quote_without_ohlc_falls_back_to_the_5m_candle(self):
        b = make({"A": {"lp": 101.5}}, {"A": rng(98, 100)})
        b.client.history = lambda *a, **k: pd.DataFrame([{"ts": pd.Timestamp(NOW.replace(minute=15, second=0)), "open": 100.0,
                                                     "high": 101.6, "low": 99.9, "close": 101.5, "volume": 1}])
        b.check_open_breakouts(NOW)
        self.assertEqual(self.rows()["A"]["taken"], "yes")

    def test_inconsistent_quote_ohlc_falls_back_to_the_5m_candle(self):
        b = make({"A": q(100, 101.0, 99.9, 101.5)}, {"A": rng(98, 100)})     # LTP above the quoted high
        b.client.history = lambda *a, **k: pd.DataFrame([{"ts": pd.Timestamp(NOW.replace(minute=15, second=0)),
                                                          "open": 100.0, "high": 101.6, "low": 99.9, "close": 101.5,
                                                          "volume": 1}])
        b.check_open_breakouts(NOW)
        self.assertEqual(self.rows()["A"]["taken"], "yes")

    def test_band_limits(self):
        self.assertTrue(strategy.in_move_band(0.01) and strategy.in_move_band(0.02))
        self.assertFalse(strategy.in_move_band(0.0099) or strategy.in_move_band(0.0201) or strategy.in_move_band(None))
        self.assertEqual((config.OPEN_BO_MIN_MOVE_PCT, config.OPEN_BO_MAX_MOVE_PCT), (0.01, 0.02))


class Minute12(Base):
    def test_busy_book_and_ranked_lower(self):
        ranges = {"A": rng(98, 100), "B": rng(98, 100), "S": rng(100, 102)}
        quotes = {"A": q(0, 0, 0, 101.0), "B": q(0, 0, 0, 100.5), "S": q(0, 0, 0, 99.0)}
        b = make(quotes, ranges, busy={"1H BREAKDOWN"})
        ts = pd.Timestamp(NOW.replace(hour=10, minute=15, second=0))
        b.client.history = lambda sym, tf, days: pd.DataFrame(
            [{"ts": ts, "open": 100.2, "high": 101, "low": 100, "close": 100.9, "volume": 1}])
        b.check_breakouts(NOW.replace(hour=10, minute=27), ts)
        r = self.rows()
        self.assertEqual((r["A"]["taken"], r["A"]["rank"], r["A"]["candle_colour"]), ("yes", "1", "green"))
        self.assertEqual((r["B"]["reason"], r["B"]["candle_colour"]), ("ranked lower", "not checked"))
        self.assertEqual((r["S"]["reason"], r["S"]["book"]), ("book busy", "1H BREAKDOWN"))
        self.assertEqual(float(r["A"]["candle_open"]), 100.2)
        self.assertEqual(float(r["A"]["stop_candle"]), 96.95)     # wick low 97 - 1 tick


class Rsi(Base):
    def test_rsi_row(self):
        sig = {"side": "SHORT", "stop_ref": 105.0, "trigger_ts": 1, "entry_below": 104.0, "detail": "d",
               "cand": {"ref_time": pd.Timestamp("2026-11-26 10:00"), "ref_price": 104.5, "ref_rsi": 78.2,
                        "div_time": pd.Timestamp("2026-11-26 10:20"), "div_price": 105.0, "div_rsi": 72.1,
                        "div_open": 104.0, "rsi_gap": 6.1}}
        saved = strategy.short_div_trigger
        strategy.short_div_trigger = lambda df, today: sig
        try:
            b = make({"X": q(0, 0, 0, 103.5)}, {})
            b.universe = {"X": {"future": "X", "lot": 100, "tick": 0.05, "fut_tick": 0.05, "underlying": "X"}}
            b.arms = {"X": {"side": "SHORT", "rsi": 74.33, "candle": ""}}
            b.client.history = lambda *a, **k: pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
            b.check_triggers(NOW.replace(hour=10, minute=25, second=8), "SHORT")
        finally:
            strategy.short_div_trigger = saved
        r = self.rows()["X"]
        self.assertEqual((r["taken"], r["check"], r["arm_rsi"], r["rsi_gap"]), ("yes", "5m close", "74.33", "6.1"))
        self.assertEqual((r["ref_time"], r["div_time"], r["entry_check"]), ("2026-11-26 10:00", "2026-11-26 10:20",
                                                                            "103.5 < open 104"))
        self.assertEqual(float(r["stop_candle"]), 105.05)
        self.assertEqual(float(r["stop_used"]), 105.05)
        self.assertAlmostEqual(float(r["stop_pct"]), 1.498, places=3)


if __name__ == "__main__":
    unittest.main()
