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
each price (paper fills don't remove liquidity from the real book).

Exits (26 Nov 2026, paper mode): market orders (walk_market() estimates their slippage on the live book), except
  - a TRAIL STOP whose market order would slip more than the exit cap (0.1%) on the live book: limit exit. Reference =
    the trail stop price, floor = reference -/+ the cap. Child IOC limits from the best bid (long) / ask (short), 1 tick
    worse each, never beyond the floor (exit_steps); what is left rests at the floor (exit_rest, checked on every
    monitoring pass) until it fills, or until price comes back to entry + EXIT_GUARD_R (0.5R; long, mirrored for a
    short): then the rest goes at market (exit_market).
  - fyers 15:10: every open position starts a limit exit (reference = best bid / ask, guard = its stop); what is
    left at 15:15 goes at market.

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
              retries=1, cap=None):
    """Capped IOC entry for qty (a multiple of unit). venue: book() -> (bids, asks); ioc(side, price, qty, book) ->
    (filled, avg). first_book = the book at signal time (gives the reference). sleep(seconds) between children
    (the bot keeps monitoring in it). cap: a fixed limit price instead of reference +/- cap_pct (exits).
    Returns a dict with ref, cap, intended, filled, avg, children, attempts."""
    min_units = round(max(unit, min_units or unit), 10)
    lv0 = our_side(first_book, side)
    out = {"ref": lv0[0][0] if lv0 else None, "cap": None, "intended": qty, "filled": 0.0, "avg": None,
           "children": 0, "attempts": 0}
    if not lv0:
        return out
    ref = out["ref"]
    cap = out["cap"] = cap if cap is not None else cap_price(side, ref, cap_pct, tick)
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


# ---------------- exits ----------------
def exit_take_side(pos_side):
    """The side an exit trades as: a long's exit sells into the bids (= "SHORT" for our_side / beyond)."""
    return "SHORT" if pos_side == "LONG" else "LONG"


def market_slip_pct(levels, qty, take_side):
    """Slippage % of a market order for qty walked through levels, vs the best level. None if they can't fill it."""
    if not levels:
        return None
    got, avg = walk_market(levels, qty)
    if got < qty - 1e-9 or not avg:
        return None
    return slippage("BUY" if take_side == "LONG" else "SELL", levels[0][0], avg, got)[0]


def exit_needs_limit(book, pos_side, qty, cap_pct):
    """True when a market exit of qty would slip more than cap_pct (or the visible book can't fill it)."""
    take = exit_take_side(pos_side)
    pct = market_slip_pct(our_side(book, take), qty, take)
    return pct is None or pct > cap_pct * 100 + 1e-9


def guard_crossed(pos_side, price, guard):
    """Price is back at (or through) the guard: a long's price at/below it, a short's at/above it."""
    if price is None or guard is None:
        return False
    return price <= guard + 1e-12 if pos_side == "LONG" else price >= guard - 1e-12


def start_exit(pos_side, qty, tick, ref, cap_pct, guard, reason, kind, deadline=None):
    """A pending limit exit (kept in the trade's state, so it survives a restart)."""
    return {"reason": reason, "kind": kind, "side": pos_side, "ref": ref,
            "floor": cap_price(exit_take_side(pos_side), ref, cap_pct, tick), "guard": guard, "deadline": deadline,
            "qty": qty, "left": qty, "lim_qty": 0.0, "lim_cost": 0.0, "mkt_qty": 0.0, "mkt_cost": 0.0,
            "children": 0, "rest_fills": 0, "book_short": False, "why_market": ""}


def _add(pend, kind, got, avg):
    if got > 0 and avg:
        pend[kind + "_qty"] = round(pend[kind + "_qty"] + got, 10)
        pend[kind + "_cost"] += got * avg
        pend["left"] = round(max(pend["left"] - got, 0.0), 10)


def exit_steps(pend, venue, unit, tick, book, sleep=None, max_children=20):
    """Child IOC limits from the best price toward the floor (run_entry with a fixed cap, no retry)."""
    take = exit_take_side(pend["side"])
    r = run_entry(venue, take, pend["left"], unit, tick, 0.0, book, sleep=sleep, max_children=max_children,
                  retries=0, cap=pend["floor"])
    pend["children"] += r["children"]
    _add(pend, "lim", r["filled"], r["avg"])
    return r


def exit_rest(pend, book, unit):
    """The resting limit at the floor, checked against the current book (whole units only)."""
    take = exit_take_side(pend["side"])
    levels = [(p, q) for p, q in our_side(book, take) if not beyond(take, p, pend["floor"])]
    avail = sum(q for _, q in levels)
    want = min(pend["left"], avail)
    want = round(math.floor(want / unit + 1e-9) * unit, 10) if unit and unit > 0 else want
    if want <= 0:
        return 0.0
    got, avg = walk_market(levels, want)
    if got > 0:
        pend["rest_fills"] += 1
        _add(pend, "lim", got, avg)
    return got


def exit_market(pend, book, why):
    """The rest at market, walked through the book; what the visible book can't hold is priced at its last level."""
    take = exit_take_side(pend["side"])
    levels = our_side(book, take)
    left = pend["left"]
    if left <= 0 or not levels:
        return
    got, avg = walk_market(levels, left)
    if got < left - 1e-9:
        pend["book_short"] = True
        cost = (got * avg if got > 0 else 0.0) + (left - got) * levels[-1][0]
        got, avg = left, cost / left
    pend["why_market"] = why
    _add(pend, "mkt", got, avg)


def exit_done(pend):
    return pend["left"] <= 1e-9


def exit_summary(pend):
    """Execution fields of a finished pending exit: type, reference, average, slippage (% and money), children."""
    q = pend["lim_qty"] + pend["mkt_qty"]
    avg = (pend["lim_cost"] + pend["mkt_cost"]) / q if q > 0 else None
    pct, money = slippage("SELL" if pend["side"] == "LONG" else "BUY", pend["ref"], avg, q)
    typ = ("limit" if pend["mkt_qty"] <= 0 else "market" if pend["lim_qty"] <= 0 else "limit+market")
    out = {"exit_type": typ, "exit_ref": pend["ref"], "exit_floor": pend["floor"], "exit_avg": avg,
           "exit_slip_pct": None if pct is None else round(pct, 4), "exit_slip_money": money,
           "exit_children": pend["children"] + pend["rest_fills"] + (1 if pend["mkt_qty"] > 0 else 0),
           "exit_limit_qty": pend["lim_qty"], "exit_market_qty": pend["mkt_qty"], "why_market": pend["why_market"],
           "exit_estimated": "sim" + (" (book too thin, rest at last level)" if pend["book_short"] else "")}
    if pend["mkt_qty"] > 0:
        mp, _ = slippage("SELL" if pend["side"] == "LONG" else "BUY", pend["ref"], pend["mkt_cost"] / pend["mkt_qty"],
                         pend["mkt_qty"])
        out["exit_market_slip_pct"] = round(mp, 4)
    return out


def exit_text(ex, fmt=lambda x: f"{x:g}"):
    """One line for the log / Telegram: how the exit was done and its slippage."""
    if not ex:
        return "Exit: market"
    typ = ex.get("exit_type", "market")
    slip = ex.get("exit_slip_pct")
    s = "" if slip is None else f", slippage {slip:+.3f}% vs {fmt(ex['exit_ref'])}"
    if typ == "market":
        est = " (estimated on the live book)" if str(ex.get("exit_estimated", "")).startswith("yes") else ""
        return f"Exit: market{s}{est}"
    if typ == "limit":
        return f"Exit: limit, {ex['exit_children']} order(s), avg {fmt(ex['exit_avg'])}{s}"
    return (f"Exit: limit {ex['exit_limit_qty']:g} + market {ex['exit_market_qty']:g} ({ex.get('why_market', '')}; "
            f"market part {ex.get('exit_market_slip_pct', 0):+.3f}%), avg {fmt(ex['exit_avg'])}{s}")


def entry_text(ex, fmt=lambda x: f"{x:g}"):
    """One line for the log / Telegram: how the entry was done (capped limit children) and its slippage."""
    if not ex or not ex.get("entry_avg"):
        return ""
    cap = (f", size cut by liquidity to {ex['qty_intended']:g} of {ex.get('qty_risk') or ex.get('qty_wanted') or '?'}"
           if ex.get("liq_capped") == "yes" else "")
    return (f"Entry: limit, {ex.get('entry_children', 1)} order(s), filled {ex.get('qty_filled', 0):g}/"
            f"{ex.get('qty_intended', 0):g} @ avg {fmt(ex['entry_avg'])} vs best {fmt(ex['entry_ref'])}, "
            f"slippage {ex.get('entry_slip_pct', 0):+.3f}%{cap}")
