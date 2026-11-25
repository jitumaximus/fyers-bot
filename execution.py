"""Entry execution shared by the fyers-bot and the binance-bot (same file in both repos; pure logic, no API calls).

Capped IOC limit entries (25 Nov 2026), instead of market orders / filling at the LTP:
  - reference = best ask (long) / best bid (short) when the signal fires; cap = reference +/- ENTRY_SLIP_CAP_PCT,
    rounded to the tick toward the reference (never beyond the cap).
  - child orders of at most what rests at the best level, as IOC limits: the first at the best ask / bid, each next one
    1 tick worse than the previous (about 1 s apart), never beyond the cap; stop when the full size is filled or the
    cap is reached.
  - cap reached with only part filled: one retry with a fresh book and the same cap (from the original reference),
    then keep whatever filled. Nothing filled = no trade ("no fill within cap").
  - children are whole units (Fyers: lots; Binance: quantity steps) and at least min_units (Binance: min notional);
    a last child that would be too small is merged into the one before it.
  - max_children per attempt is a safety limit (each child costs a book call and ~1 s).
Liquidity cap: the size is at most LIQ_CAP_PCT_OF_DEPTH of the quantity in the top 5 levels on our side of the book
(asks for a long, bids for a short) at signal time.
Paper mode: PaperVenue fills each IOC child against the real live order book, minus what this entry already took at
each price (paper fills don't remove liquidity from the real book). Exits stay market orders; in paper mode
walk_market() estimates their slippage on the live book.

Books are lists of (price, qty) best first: bids descending, asks ascending. Quantities are in exchange units
(Fyers: shares; Binance: coins)."""
import math


def round_tick(price, tick, mode):
    if not tick or tick <= 0:
        return price
    q = price / tick
    q = math.ceil(q - 1e-9) if mode == "up" else math.floor(q + 1e-9) if mode == "down" else round(q)
    return round(q * tick, 10)


def our_side(book, side):
    """Levels we take liquidity from: asks for a long, bids for a short. book = (bids, asks)."""
    bids, asks = book
    return asks if side == "LONG" else bids


def top_qty(levels, n=5):
    return sum(q for _, q in levels[:n])


def cap_price(side, ref, cap_pct, tick):
    return (round_tick(ref * (1 + cap_pct), tick, "down") if side == "LONG"
            else round_tick(ref * (1 - cap_pct), tick, "up"))


def beyond(side, price, cap):
    return price > cap + 1e-12 if side == "LONG" else price < cap - 1e-12


def liquidity_cap(units, unit, levels, pct):
    """(capped units, max units by liquidity, top-5 qty). Units are multiples of `unit`."""
    depth = top_qty(levels, 5)
    max_units = round(int(math.floor(pct * depth / unit + 1e-9)) * unit, 10) if unit > 0 else 0
    return min(units, max_units), max_units, depth


def slippage(side, ref, avg, qty):
    """(% of the reference, money) for a buy (long entry / short exit) or a sell; positive = worse than reference."""
    if not ref or not avg:
        return None, None
    per = (avg - ref) if side == "BUY" else (ref - avg)
    return per / ref * 100, per * qty


def walk_market(levels, qty):
    """A market order for qty walked through the levels: (filled qty, average price or None)."""
    left, cost = qty, 0.0
    for p, q in levels:
        if left <= 1e-12:
            break
        take = min(q, left)
        cost += take * p
        left -= take
    got = qty - max(left, 0.0)
    return got, (cost / got if got > 0 else None)


class PaperVenue:
    """IOC fills simulated on the live book. fetch() -> (bids, asks) from the exchange."""

    def __init__(self, fetch):
        self.fetch, self.taken = fetch, {}

    def book(self):
        bids, asks = self.fetch()

        def left(levels):
            out = []
            for p, q in levels:
                r = q - self.taken.get(p, 0.0)
                if r > 1e-12:
                    out.append((p, r))
            return out
        return left(bids), left(asks)

    def ioc(self, side, price, qty, book=None):
        """(filled qty, average price). Walks the latest book (re-fetched unless given) up to the limit price."""
        levels = our_side(book or self.book(), side)
        left, cost = qty, 0.0
        for p, q in levels:
            if left <= 1e-12 or beyond(side, p, price):
                break
            take = min(q, left)
            cost += take * p
            left -= take
            self.taken[p] = self.taken.get(p, 0.0) + take
        got = qty - left
        return got, (cost / got if got > 0 else None)


def run_entry(venue, side, qty, unit, tick, cap_pct, first_book, min_units=None, sleep=None, max_children=20,
              retries=1):
    """Capped IOC entry for qty (a multiple of unit). venue: book() -> (bids, asks); ioc(side, price, qty, book) ->
    (filled, avg). first_book = the book at signal time (gives the reference). sleep(seconds) between children
    (the bot keeps monitoring in it). Returns a dict with ref, cap, intended, filled, avg, children, attempts."""
    min_units = round(max(unit, min_units or unit), 10)
    lv0 = our_side(first_book, side)
    out = {"ref": lv0[0][0] if lv0 else None, "cap": None, "intended": qty, "filled": 0.0, "avg": None,
           "children": 0, "attempts": 0}
    if not lv0:
        return out
    ref = out["ref"]
    cap = out["cap"] = cap_price(side, ref, cap_pct, tick)
    filled, cost, book = 0.0, 0.0, first_book
    for attempt in range(1 + retries):
        if filled >= qty - 1e-9:
            break
        if attempt:
            if sleep:
                sleep(1.0)
            book = venue.book()
        levels = our_side(book, side)
        if not levels or beyond(side, levels[0][0], cap):
            continue
        out["attempts"] += 1
        price, n = levels[0][0], 0
        while filled < qty - 1e-9 and n < max_children and not beyond(side, price, cap):
            if n:
                if sleep:
                    sleep(1.0)
                book = venue.book()
                levels = our_side(book, side)
            rest = qty - filled
            best = levels[0][1] if levels else 0.0
            child = round(math.floor(min(rest, best) / unit + 1e-9) * unit, 10)
            child = max(child, min_units)
            if rest - child < min_units - 1e-9:      # a too-small last child merges into this one
                child = round(rest, 10)
            if child > rest + 1e-9 or child < min_units - 1e-9:
                break                                 # what is left is below the minimum order
            got, avg = venue.ioc(side, price, child, book)
            n += 1
            out["children"] += 1
            if got > 0:
                filled += got
                cost += got * avg
            price = round_tick(price + tick if side == "LONG" else price - tick, tick, "nearest")
    out["filled"] = round(filled, 10)
    out["avg"] = cost / filled if filled > 0 else None
    return out
