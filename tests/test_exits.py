"""26 Nov 2026: limit exits (paper). Trail stops whose market exit would slip > EXIT_SLIP_CAP_PCT exit with limit
orders (floor = trail stop - 0.1%), the rest waits at the floor and goes at market once price is back to entry + 0.5R;
breakeven / first stops stay market; 15:10 starts limit exits on every position, 15:15 sends the rest at market.
Run: venv/bin/python -m unittest discover -s tests -v   (no API calls, no .env needed)"""
import csv
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import test_changes_2026_11_23 as base  # noqa: E402


class Client(base.FakeClient):
    """Deep book at the LTP unless a book is set for the symbol."""
    def __init__(self, *a):
        super().__init__(*a)
        self.books = {}

    def depth(self, sym):
        if sym in self.books:
            bids, asks = self.books[sym]
            return {"bids": list(bids), "asks": list(asks), "totalbuyqty": 1.0, "totalsellqty": 1.0}
        return super().depth(sym)


class Exits(unittest.TestCase):
    setUp = base.OpenCheckInBot.setUp
    run_at = base.OpenCheckInBot.run_at

    def open_long(self):
        c = base.OpenCheckInBot.client(self)
        c = Client(None, c.bars, c.ltps)
        self.run_at("09:19:30", c)                       # NSE:UP long 200 @ 1005, stop 992.45, R 12.55
        self.c, self.tr = c, self.b.trader
        self.tr.sleep = lambda s: None
        self.p = self.tr.book("1H BREAKOUT")["position"]
        self.assertEqual((self.p["qty"], self.p["lot"]), (200, 100))
        return self.p

    def to_trail(self):
        self.tr.check("1H BREAKOUT", 1017.6, 1017.6)    # breakeven
        self.tr.check("1H BREAKOUT", 1042.7, 1042.7)    # 3R: stop 1030.1 (2R locked)
        self.assertEqual(self.p["stop"], 1030.1)

    def rows(self):
        with base.config.DATA_DIR.joinpath("trades.csv").open() as f:
            return [r for r in csv.DictReader(f) if r["spot"] == "NSE:UP"]

    def thin(self):
        # a market sell of 200: 100 @ 1030, 50 @ 1028, 50 @ 1027 -> avg 1028.75, 0.12% below 1030 (> 0.1%)
        self.c.books["NSE:UP"] = ([(1030.0, 100), (1028.0, 50), (1027.0, 50), (1026.0, 50), (1025.0, 50)],
                                  [(1030.05, 999)])

    def test_trail_stop_deep_book_exits_at_market(self):
        self.open_long()
        self.to_trail()
        self.tr.check("1H BREAKOUT", 1030.0, 1030.0)
        self.assertIsNone(self.tr.book("1H BREAKOUT")["position"])
        r = self.rows()[-1]
        self.assertEqual((r["reason"], r["exit_type"]), ("TRAIL STOP (2R)", "market"))
        self.assertTrue(any("Exit: market, slippage +0.000%" in m for m in self.sent))

    def test_trail_stop_thin_book_limit_then_fills_at_floor(self):
        self.open_long()
        self.to_trail()
        self.thin()
        self.tr.check("1H BREAKOUT", 1030.0, 1030.0)
        pend = self.p["pending"]
        self.assertEqual((pend["floor"], pend["guard"]), (1029.1, 1005.0 + 0.5 * 12.55))
        self.assertEqual((pend["lim_qty"], pend["left"]), (100, 100))   # 100 @ 1030; nothing else above the floor
        self.assertTrue(any("-> limit exit" in m and "waiting at 1029.1" in m for m in self.sent))
        # saved: survives a restart
        st = json.loads(base.config.DATA_DIR.joinpath("state.json").read_text())
        self.assertEqual(st["books"]["1H BREAKOUT"]["position"]["pending"]["left"], 100)
        self.c.books["NSE:UP"] = ([(1029.0, 500)], [(1029.05, 999)])
        self.tr.check("1H BREAKOUT", 1029.0, 1029.0)        # bids below the floor: keeps waiting
        self.assertEqual(self.p["pending"]["left"], 100)
        self.c.books["NSE:UP"] = ([(1029.5, 40), (1029.2, 500)], [(1029.55, 999)])
        self.tr.check("1H BREAKOUT", 1029.5, 1029.5)        # the resting limit at 1029.1 fills
        self.assertIsNone(self.tr.book("1H BREAKOUT")["position"])
        r = self.rows()[-1]
        self.assertEqual((r["exit_type"], r["reason"]), ("limit", "TRAIL STOP (2R)"))
        avg = (100 * 1030 + 40 * 1029.5 + 60 * 1029.2) / 200
        self.assertAlmostEqual(float(r["fut_exit"]), round(avg, 4))
        self.assertAlmostEqual(float(r["net_pnl"]), round((avg - 1005) * 200 - 150, 2), places=1)
        self.assertTrue(any("Exit: limit," in m for m in self.sent))

    def test_trail_limit_goes_market_near_breakeven(self):
        self.open_long()
        self.to_trail()
        self.thin()
        self.tr.check("1H BREAKOUT", 1030.0, 1030.0)
        self.c.books["NSE:UP"] = ([(1011.0, 60), (1010.0, 60)], [(1011.05, 999)])
        self.tr.check("1H BREAKOUT", 1015.0, 1015.0)        # above entry + 0.5R (1011.275): rest still waits
        self.assertEqual(self.p["pending"]["left"], 100)
        self.tr.check("1H BREAKOUT", 1011.0, 1011.0)        # at/below the guard: the rest at market
        r = self.rows()[-1]
        self.assertEqual(r["exit_type"], "limit+market")
        self.assertAlmostEqual(float(r["fut_exit"]), (100 * 1030 + 60 * 1011 + 40 * 1010) / 200)
        msg = next(m for m in self.sent if "EXIT" in m and "Net P&L" in m)
        self.assertIn("Exit: limit 100 + market 100 (price back to entry + 0.5R; market part", msg)

    def test_breakeven_stop_is_market_even_when_thin(self):
        self.open_long()
        self.tr.check("1H BREAKOUT", 1017.6, 1017.6)        # breakeven only
        self.thin()
        self.tr.check("1H BREAKOUT", 1004.0, 1004.0)
        r = self.rows()[-1]
        self.assertEqual((r["reason"], r["exit_type"]), ("BREAKEVEN STOP", "market"))
        self.assertGreater(float(r["exit_slip_pct"]), 0.1)                 # logged

    def test_square_off_limit_at_1510_rest_market_at_1515(self):
        self.open_long()
        self.b.trader.state["day"] = str(base.DAY)
        self.c.ltps["NSE:UP"] = 1020.0
        self.c.books["NSE:UP"] = ([(1020.0, 100), (1018.0, 300)], [(1020.05, 999)])
        self.tr.square_off_limits()
        pend = self.p["pending"]
        self.assertEqual((pend["kind"], pend["ref"], pend["floor"], pend["guard"]), ("squareoff", 1020.0, 1019.0, 992.45))
        self.assertEqual(pend["left"], 100)
        self.tr.square_off_limits()                         # 5 s later: nothing new
        self.c.books["NSE:UP"] = ([(1018.0, 300)], [(1018.05, 999)])
        self.tr.check("1H BREAKOUT", 1019.0, 1019.0)        # bids 1018 < floor 1019: waits
        self.assertEqual(pend["left"], 100)
        self.tr.square_off()                                # 15:15: the rest at market
        r = self.rows()[-1]
        self.assertEqual((r["reason"], r["exit_type"]), ("15:10 SQUARE-OFF", "limit+market"))
        self.assertAlmostEqual(float(r["fut_exit"]), (100 * 1020 + 100 * 1018) / 200)

    def test_square_off_stop_hit_goes_market(self):
        self.open_long()
        self.c.books["NSE:UP"] = ([(1000.0, 100), (998.0, 300)], [(1000.05, 999)])
        self.tr.square_off_limits()
        self.c.books["NSE:UP"] = ([(992.0, 500)], [(992.05, 999)])
        self.tr.check("1H BREAKOUT", 992.0, 992.0)          # its stop 992.45: the rest at market
        r = self.rows()[-1]
        self.assertEqual(r["exit_type"], "limit+market")
        msg = next(m for m in self.sent if "Net P&L" in m and "UP" in m)
        self.assertIn("stop hit", msg)

    def test_entry_line_in_telegram(self):
        self.open_long()
        self.assertTrue(any("Entry: limit, 1 order(s), filled 200/200 @ avg 1005 vs best 1005, slippage +0.000%" in m
                            for m in self.sent))


if __name__ == "__main__":
    unittest.main()
