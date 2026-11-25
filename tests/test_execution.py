"""execution.py (same file in the fyers-bot and the binance-bot): capped IOC children, retry, liquidity cap,
paper fills on the live book, exit walk."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import execution as X  # noqa: E402


def book(asks=(), bids=()):
    return list(bids), list(asks)


class Fixed(X.PaperVenue):
    """Paper venue whose 'live' book is a fixed list of snapshots (the last one repeats)."""
    def __init__(self, *snaps):
        self.snaps, self.calls = list(snaps), 0
        super().__init__(self.next)

    def next(self):
        self.calls += 1
        return self.snaps[min(self.calls - 1, len(self.snaps) - 1)]


class Entry(unittest.TestCase):
    def test_liquid_one_child_no_slippage(self):
        b = book(asks=[(100.0, 1000), (100.05, 1000)])
        r = X.run_entry(Fixed(b), "LONG", 300, 50, 0.05, 0.001, b)
        self.assertEqual((r["ref"], r["cap"], r["filled"], r["avg"], r["children"]), (100.0, 100.1, 300, 100.0, 1))

    def test_walks_up_one_tick_per_child_within_the_cap(self):
        b = book(asks=[(100.0, 100), (100.05, 100), (100.1, 500), (100.15, 900)])
        r = X.run_entry(Fixed(b), "LONG", 250, 50, 0.05, 0.001, b)
        self.assertEqual((r["filled"], r["children"]), (250, 3))
        self.assertAlmostEqual(r["avg"], (100 * 100 + 100.05 * 100 + 100.1 * 50) / 250)

    def test_partial_then_retry_then_keep_what_filled(self):
        b = book(asks=[(100.0, 100), (100.2, 1000)])           # the next level is beyond the 100.10 cap
        v = Fixed(b)
        r = X.run_entry(v, "LONG", 300, 100, 0.05, 0.001, b)
        self.assertEqual((r["filled"], r["avg"], r["children"], r["attempts"]), (100, 100.0, 3, 1))
        # retry with a fresh book: the best ask 100.2 is beyond the cap from the ORIGINAL reference -> nothing more

    def test_retry_fills_the_rest_when_liquidity_returns(self):
        first = book(asks=[(100.0, 100), (100.2, 1000)])
        refill = book(asks=[(100.05, 1000)])
        v = Fixed(first, first, refill)                        # child 2, child 3, then the retry's fresh book
        r = X.run_entry(v, "LONG", 300, 100, 0.05, 0.001, first)
        self.assertEqual((r["filled"], r["attempts"]), (300, 2))
        self.assertAlmostEqual(r["avg"], (100 * 100 + 100.05 * 200) / 300)

    def test_nothing_filled(self):
        class Never(Fixed):
            def ioc(self, *a, **k):
                return 0, None
        b = book(asks=[(100.0, 100)])
        r = X.run_entry(Never(b), "LONG", 100, 100, 0.05, 0.001, b)
        self.assertEqual((r["filled"], r["avg"]), (0, None))
        self.assertEqual(r["children"], 3 + 3)                 # 100.00, .05, .10 then the retry's three

    def test_short_mirror_and_cap_rounding(self):
        b = book(bids=[(2455.0, 50), (2454.95, 50), (2454.0, 999)])
        r = X.run_entry(Fixed(b), "SHORT", 100, 50, 0.05, 0.001, b)
        self.assertEqual(r["cap"], 2452.55)                     # 2455 * 0.999 = 2452.545 -> up to the tick
        self.assertEqual((r["filled"], r["children"]), (100, 2))
        self.assertAlmostEqual(r["avg"], (2455 * 50 + 2454.95 * 50) / 100)

    def test_children_never_beyond_the_cap(self):
        prices = []

        class Spy(Fixed):
            def ioc(self, side, price, qty, book=None):
                prices.append(price)
                return 0, None
        b = book(asks=[(10.0, 5)])
        X.run_entry(Spy(b), "LONG", 5, 1, 0.001, 0.001, b, max_children=50, retries=0)
        self.assertEqual(prices[0], 10.0)
        self.assertEqual(max(prices), 10.01)
        self.assertEqual(len(prices), 11)

    def test_min_notional_children_and_merge(self):
        b = book(asks=[(1.0, 0.4), (1.0001, 5)])
        sent = []

        class Spy(Fixed):
            def ioc(self, side, price, qty, book=None):
                sent.append(round(qty, 6))
                return super().ioc(side, price, qty, book)
        r = X.run_entry(Spy(b), "LONG", 0.5, 0.1, 0.0001, 0.001, b, min_units=0.2)
        self.assertEqual(sent, [0.5])                           # 0.4 + a 0.1 rest (< 0.2 min) merged
        # the IOC at 1.0 only finds 0.4 there; the 0.1 left is below the minimum order, so 0.4 is kept
        self.assertAlmostEqual(r["filled"], 0.4)

    def test_empty_book(self):
        r = X.run_entry(Fixed(book()), "LONG", 1, 1, 0.01, 0.001, book())
        self.assertEqual((r["ref"], r["filled"]), (None, 0))


class Helpers(unittest.TestCase):
    def test_liquidity_cap(self):
        asks = [(100, 300), (100.05, 300), (100.1, 300), (100.15, 50), (100.2, 50), (100.25, 9999)]
        self.assertEqual(X.liquidity_cap(1000, 50, asks, 0.20), (200, 200, 1000))   # 20% of the top 5 = 200
        self.assertEqual(X.liquidity_cap(100, 50, asks, 0.20), (100, 200, 1000))
        self.assertEqual(X.liquidity_cap(100, 250, asks, 0.20)[0], 0)              # below one lot

    def test_walk_and_slippage(self):
        got, avg = X.walk_market([(99.9, 100), (99.8, 100)], 150)
        self.assertEqual(got, 150)
        self.assertAlmostEqual(avg, (99.9 * 100 + 99.8 * 50) / 150)
        pct, money = X.slippage("SELL", 99.9, avg, 150)
        self.assertAlmostEqual(money, (99.9 - avg) * 150)
        self.assertGreater(pct, 0)
        self.assertEqual(X.walk_market([(1.0, 2)], 5), (2, 1.0))                  # book shallower than the order
        pct, _ = X.slippage("BUY", 100, 99.9, 1)
        self.assertLess(pct, 0)                                                      # better than reference


if __name__ == "__main__":
    unittest.main()
