"""Main loop. SIGNAL_SOURCE=FUTURES (default): scans the futures (stocks + indices) and every signal, stop, target and
order-book check uses the future's own price. SIGNAL_SOURCE=SPOT: scans stock spot prices, trades their futures.
Squares off at 15:15."""
import logging
import time
import traceback
from datetime import datetime, time as dtime, timedelta

import config
import execution
import strategy
from fyers_client import AuthError, FyersClient, now_ist
from telegram_bot import Telegram
from trader import BOOKS, Trader, label, log_candidate, log_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(config.DATA_DIR / "bot.log"), logging.StreamHandler()])
log = logging.getLogger("bot")


class FyersLiveVenue:
    """Live IOC limit children on the future (execution.run_entry). A rejected or failed child counts as unfilled."""

    def __init__(self, client, fut):
        self.client, self.fut = client, fut

    def book(self):
        d = self.client.depth(self.fut)
        return d["bids"], d["asks"]

    def ioc(self, side, price, qty, book=None):
        try:
            oid = self.client.place_order(self.fut, int(qty), 1 if side == "LONG" else -1, 1,
                                          limit_price=round(price, 2), validity="IOC")
        except Exception as e:
            log.warning("IOC child %s %s @ %s: %s", self.fut, qty, price, e)
            return 0, None
        status, px, filled = None, 0.0, 0.0
        for _ in range(10):                 # an IOC is final at once; poll briefly for the order book to show it
            status, px, filled = self.client.order_fill(oid)
            if status in (2, 1, 5):
                break
            time.sleep(0.3)
        return filled, (px or None)


OPEN_CANDLE_END = dtime(9, 20)   # end of the first 5m candle (the OPEN 5m check must run before it)


def floor_to(dt, minutes):
    return dt.replace(minute=dt.minute - dt.minute % minutes, second=0, microsecond=0)


class Bot:
    def __init__(self):
        self.tg = Telegram()
        self.client = FyersClient()
        self.trader = Trader(self.client, self.tg.send)
        self.trader.sleep = self.exec_sleep   # exit child orders: keep monitoring between them
        self.universe = {}
        self.universe_day = None
        self.arms = {}                 # symbol -> {"side", "rsi", "candle"}   (2H RSI)
        self.ranges = {}               # symbol -> {"high", "low", "pct", ...}  (1H consolidation)
        self.last_bo = None            # 15m candle already checked for breakouts
        self.open_bo_day = None        # day the 09:19:30 OPEN 5m breakout check ran (or was missed)
        self.next_arm_refresh = None   # None = refresh as soon as possible
        self.last_seen = {}            # (symbol, side) -> trigger candle already evaluated
        self.last5 = self.last15 = None
        self.last_poll = 0.0
        self.login_prompted = None
        self.last_square_try = 0.0
        self.summary_sent = None
        self.market_live = None        # date on which live candles were seen
        self.last_live_check = 0.0
        self.no_data_noted = None

    # ---------------- helpers ----------------
    def is_market_live(self, now):
        """No holiday list: the market counts as open once Fyers returns a candle dated today."""
        today = now.date()
        if self.market_live == today:
            return True
        open_check = now.replace(hour=config.MARKET_OPEN.hour, minute=config.MARKET_OPEN.minute + 1,
                                 second=0, microsecond=0)
        retry = 20 if now.time() < OPEN_CANDLE_END else 300   # don't miss the 09:19:30 OPEN 5m check
        if now < open_check or not self.universe or time.time() - self.last_live_check < retry:
            return False
        self.last_live_check = time.time()
        ref = next((s for s, v in self.universe.items() if v["underlying"] == "RELIANCE"), next(iter(self.universe)))
        df = self.client.history(ref, config.SHORT_TF, days=1)
        if not df.empty and (df["ts"].dt.date == today).any():
            self.market_live = today
            self.tg.send("📈 Market data is live — scanning for setups.")
            return True
        if now.time() >= config.MARKET_OPEN.replace(minute=30) and self.no_data_noted != today:
            self.no_data_noted = today
            self.tg.send("😴 No live data from Fyers today — looks like a holiday. I'll keep checking every 5 minutes.")
        return False

    def tick(self):
        """Light work that must keep running during long scans: commands + position monitoring."""
        for m in self.tg.messages():
            self.handle_command(m)
        if self.trader.positions() and time.time() - self.last_poll >= config.POSITION_POLL_SECONDS:
            self.last_poll = time.time()
            self.trader.check_all()

    def prompt_login(self):
        self.tg.send("🔑 Fyers login needed.\n1) Open this link and log in:\n" + self.client.login_url() +
                     "\n2) You'll land on a page — copy its full address and paste it here.")
        self.login_prompted = now_ist().date()

    def handle_command(self, text):
        t = text.lower()
        try:
            if "auth_code" in text:
                self.client.complete_login(text)
                self.tg.send(f"✅ Logged in. Mode: {'PAPER' if self.trader.paper else '⚠️ LIVE'}. "
                             f"{len(BOOKS)} strategies × ₹{config.STARTING_CAPITAL:,.0f}, total balance ₹{self.trader.total_balance():,.0f}")
                self.next_arm_refresh = None
            elif t == "/login":
                self.prompt_login()
            elif t == "/status":
                st = self.trader.state
                lines = [f"Mode: {'PAPER' if self.trader.paper else 'LIVE'} | {'⏸ PAUSED' if st['paused'] else '▶️ running'}"]
                for b in BOOKS:
                    bk = st["books"][b]
                    p = bk["position"]
                    if p:
                        locked = f" (trailing, {p['trail_r'] - config.TRAIL_GAP_R}R locked)" if p.get("trail_r") else \
                            " (at entry)" if p["at_breakeven"] else ""
                        tp = p["target"] if p.get("target") is not None else "none (trailing)"
                    pos = (f"{p['side']} {p['future']} qty {p['qty']} @ {p['fut_entry']} | SL {p['stop']}{locked} | TP {tp}"
                           if p else "no position")
                    lines.append(f"\n{label(b)}: ₹{bk['balance']:,.0f} (day start ₹{bk['day_start_balance']:,.0f}), "
                                 f"{bk['day_trades']} trade(s) today\n  {pos}")
                lines.append(f"\nArmed: {sum(a['side'] == 'SHORT' for a in self.arms.values())} short, "
                             f"{sum(a['side'] == 'LONG' for a in self.arms.values())} long | 1H ranges: {len(self.ranges)}")
                self.tg.send("\n".join(lines))
            elif t == "/summary":
                self.tg.send(f"📊 Results since {self.trader.state['books_since'][:10]} "
                             f"(each strategy started with ₹{config.STARTING_CAPITAL:,.0f})\n" + self.trader.report())
            elif t == "/armed":
                lines = [f"{a['side']:5} {s.replace('NSE:', '')}  RSI {a['rsi']:.1f}"
                         for s, a in sorted(self.arms.items(), key=lambda kv: kv[1]["side"])]
                lines += [f"RANGE {s.replace('NSE:', '')}  {r['low']}-{r['high']} ({r['pct'] * 100:.2f}%)"
                          for s, r in self.ranges.items()]
                text = "\n".join(lines) or "Nothing armed right now."
                for i in range(0, len(text), 3500):   # Telegram message size limit
                    self.tg.send(text[i:i + 3500])
            elif t == "/pause":
                self.trader.state["paused"] = True
                self.trader.save()
                self.tg.send("⏸ Paused: no new entries. Open positions are still managed.")
            elif t == "/resume":
                self.trader.state["paused"] = False
                self.trader.save()
                self.tg.send("▶️ Resumed.")
            elif t == "/exit":
                self.trader.square_off("MANUAL EXIT")
            else:
                self.tg.send("Commands: /status /summary /armed /pause /resume /exit /login")
        except Exception as e:
            self.tg.send(f"⚠️ {e}")

    # ---------------- 2H arming ----------------
    def refresh_arms(self, now):
        """Hourly: one 1H history call per symbol feeds the 2H RSI arms and the 1H consolidation ranges."""
        new_arms, new_ranges, next_close = {}, {}, None
        for i, sym in enumerate(self.universe):
            if i % 10 == 0:
                self.tick()
            df1h = self.client.history(sym, config.BREAKOUT_TF, days=60)
            nc = strategy.next_close_time(df1h, config.BREAKOUT_TF, now)
            if nc and (next_close is None or nc < next_close):
                next_close = nc
            # 2H RSI arming
            closed2h = strategy.closed_candles(strategy.resample_2h(df1h), config.ARM_TF, now)
            side, rsi_val, candle = strategy.arm_state(closed2h)
            if side:
                new_arms[sym] = {"side": side, "rsi": rsi_val, "candle": str(candle)}
                old = self.arms.get(sym)
                if not old or old["candle"] != str(candle):
                    log_signal(now, sym, f"ARM_{side}", f"2H {candle:%d-%b %H:%M} RSI {rsi_val:.1f}", "armed")
            # 1H consolidation
            rng = strategy.consolidation_range(strategy.closed_candles(df1h, config.BREAKOUT_TF, now))
            if rng:
                new_ranges[sym] = rng
                old = self.ranges.get(sym)
                if not old or old["to"] != rng["to"]:
                    log_signal(now, sym, "RANGE_1H", f"{rng['low']}-{rng['high']} ({rng['pct'] * 100:.2f}%)", "watching")
        self.ranges = new_ranges
        self.arms = new_arms
        # refresh again right after the forming 1H candle closes
        open_dt = now.replace(hour=config.MARKET_OPEN.hour, minute=config.MARKET_OPEN.minute, second=0, microsecond=0)
        if next_close:
            self.next_arm_refresh = next_close + timedelta(seconds=20)
        elif now < open_dt:
            self.next_arm_refresh = open_dt + timedelta(minutes=1)
        elif now.time() >= config.MARKET_CLOSE:
            self.next_arm_refresh = open_dt + timedelta(days=1)   # tomorrow (the day rollover also resets it)
        else:
            self.next_arm_refresh = now + timedelta(minutes=15)
        shorts = sum(a["side"] == "SHORT" for a in new_arms.values())
        log.info("refreshed: %d short armed, %d long armed, %d 1H ranges; next %s", shorts,
                 len(new_arms) - shorts, len(new_ranges), self.next_arm_refresh)

    # ---------------- candidates.csv ----------------
    def candidate(self, now, book, sym, side, check, rank, res=None, reason="", **nums):
        """One row per setup that passed at a check, taken or not (trader.CAND_COLS). res = try_enter's result."""
        res = res or {}
        row = {"time": now.strftime("%Y-%m-%d %H:%M:%S"), "bot": "fyers-bot", "book": book, "symbol": sym,
               "side": side, "check": check, "rank": rank}
        row.update(nums)
        row.update({k: v for k, v in res.items() if k not in ("taken", "reason")})
        row["taken"] = "yes" if res.get("taken") else "no"
        row["reason"] = "" if res.get("taken") else (res.get("reason") or reason)
        for k, v in row.items():
            if isinstance(v, float):
                row[k] = round(v, 3) if k.endswith("_pct") else round(v, 4)
            elif hasattr(v, "strftime"):
                row[k] = v.strftime("%Y-%m-%d %H:%M")
        log_candidate(row)

    @staticmethod
    def pct(x):
        return None if x is None else x * 100

    def bo_nums(self, side, rng, ltp, sig=None, tick=0.0, **more):
        """Range numbers shared by the opening and minute-12 rows."""
        out = {"range_low": rng["low"], "range_high": rng["high"], "ltp": ltp,
               "beyond_pct": self.pct(strategy.beyond_range(side, rng, ltp)) if ltp else None}
        if sig:
            out["stop_candle"] = sig["stop_ref"] + (tick if side == "SHORT" else -tick)
        out.update(more)
        return out

    # ---------------- triggers ----------------
    def check_triggers(self, now, side):
        tf = config.SHORT_TF if side == "SHORT" else config.LONG_TF
        book = f"2H RSI {side}"
        check = f"{tf}m close"
        armed = [s for s, a in self.arms.items() if a["side"] == side]
        armed.sort(key=lambda s: self.arms[s]["rsi"], reverse=(side == "SHORT"))  # most extreme first
        rank = 0
        for sym in armed:
            self.tick()
            if not self.trader.can_enter(book):
                return
            df = strategy.closed_candles(self.client.history(sym, tf, days=5), tf, now)
            if config.RSI_ENTRY == "divergence":
                sig = (strategy.short_div_trigger if side == "SHORT" else strategy.long_div_trigger)(df, now.date())
            else:
                sig = (strategy.short_trigger if side == "SHORT" else strategy.long_trigger)(df, now.date())
            if not sig or self.last_seen.get((sym, side)) == sig["trigger_ts"]:
                continue
            self.last_seen[(sym, side)] = sig["trigger_ts"]
            rank += 1
            tick = self.universe[sym]["tick"]
            nums = dict(sig.get("cand") or {}, arm_rsi=round(self.arms[sym]["rsi"], 2),
                        stop_candle=sig["stop_ref"] + (tick if side == "SHORT" else -tick))
            if side == "SHORT" and self.trader.in_cooldown(book, sym, config.SHORT_COOLDOWN_MIN, now):
                log_signal(now, sym, f"TRIGGER {book}", sig["detail"],
                           f"skipped: cooldown ({config.SHORT_COOLDOWN_MIN} min after the last {sym} short)")
                self.candidate(now, book, sym, side, check, rank, reason="cooldown", **nums)
                continue
            res = self.try_enter(now, sym, sig, book, f"2H RSI {self.arms[sym]['rsi']:.1f} | {sig['detail']}")
            self.candidate(now, book, sym, side, check, rank, res, **nums)

    def check_breakouts(self, now, candle_ts):
        """Minute 12 of each 15m candle: consolidating futures trading above (breakout) or below (breakdown) their
        1H range; the 15m candle must be green (long) / red (short) so far. Strongest break first. A book that is
        busy (or filled earlier in this check) still gets a candidates.csv row for each break, from the one price
        call (15m colour "not checked": that would need a candle call per symbol)."""
        if not self.ranges:
            return
        prices = self.client.ltp(list(self.ranges))

        def strength(s):
            p, r = prices.get(s) or 0, self.ranges[s]
            return p / r["high"] if p > r["high"] else (r["low"] / p if 0 < p < r["low"] else 0)
        outside = [s for s in self.ranges if strength(s) > 1]
        outside.sort(key=strength, reverse=True)  # strongest break first
        check, taken, rank = "15m min 12", {}, {"1H BREAKOUT": 0, "1H BREAKDOWN": 0}
        for sym in outside:
            self.tick()
            rng, p = self.ranges[sym], prices.get(sym) or 0
            book = "1H BREAKOUT" if p > rng["high"] else "1H BREAKDOWN"
            side = "LONG" if book == "1H BREAKOUT" else "SHORT"
            if taken.get(book) or not self.trader.can_enter(book):
                rank[book] += 1
                self.candidate(now, book, sym, side, check, rank[book],
                               reason="ranked lower" if taken.get(book) else "book busy",
                               **self.bo_nums(side, rng, p, candle_colour="not checked"))
                continue
            df15 = self.client.history(sym, config.LONG_TF, days=1)
            row = df15[df15["ts"] == candle_ts] if not df15.empty else df15
            if row.empty:
                continue
            ltp = self.client.ltp([sym]).get(sym)
            c_open = float(row["open"].iloc[0])
            sig = strategy.breakout_trigger(rng, c_open, ltp)
            book = "1H BREAKOUT" if sig and sig["side"] == "LONG" else "1H BREAKDOWN"
            if sig and self.trader.can_enter(book):
                rank[book] += 1
                res = self.try_enter(now, sym, sig, book, sig["detail"])
                self.candidate(now, book, sym, sig["side"], check, rank[book], res,
                               **self.bo_nums(sig["side"], rng, ltp, sig, self.universe[sym]["tick"], candle_open=c_open,
                                              candle_colour="green" if sig["side"] == "LONG" else "red"))
                if res.get("taken"):
                    taken[book] = sym

    def check_open_breakouts(self, now):
        """Once a day at 09:19:30 (config.BREAKOUT_OPEN_CHECK): 1H ranges (yesterday's last 3 closed 1H candles) broken
        at the open, with the first 5m candle green (breakout) / red (breakdown) so far. Ignores ENTRY_START.
        One quote call gives every symbol's LTP and today's open / high / low, which at 09:19:30 are the first 5m
        candle's (a symbol whose quote lacks them falls back to its 5m candle). Only breaks whose first 5m move
        (LTP vs the 09:15 open) is within OPEN_BO_MIN/MAX_MOVE_PCT qualify; biggest move first."""
        if not self.ranges:
            log.info("OPEN 5m check: no 1H ranges")
            return
        quotes = self.client.quotes(list(self.ranges))
        first = now.replace(hour=config.MARKET_OPEN.hour, minute=config.MARKET_OPEN.minute, second=0, microsecond=0)
        check, cands, broken = "OPEN 5m 09:19:30", [], 0
        for sym, rng in self.ranges.items():
            v = quotes.get(sym) or {}
            ltp = float(v.get("lp") or 0)
            if not ltp or rng["low"] <= ltp <= rng["high"]:
                continue
            broken += 1
            o, h, l = (float(v.get(k) or 0) for k in ("open_price", "high_price", "low_price"))
            if not (o and h and l and l <= min(o, ltp) and max(o, ltp) <= h):   # missing or inconsistent quote OHLC
                self.tick()
                df5 = self.client.history(sym, config.SHORT_TF, days=1)
                row = df5[df5["ts"] == first] if not df5.empty else df5
                if row.empty:
                    continue
                c = row.iloc[0]
                o, h, l = float(c["open"]), float(c["high"]), float(c["low"])
            sig = strategy.open_breakout_trigger(rng, o, h, l, ltp)
            if not sig:
                continue                     # 5m candle not green / red in the break direction: no setup
            side = sig["side"]
            move = strategy.open_move(side, o, ltp)
            cands.append({"sym": sym, "sig": sig, "side": side, "move": move,
                          "book": "1H BREAKOUT" if side == "LONG" else "1H BREAKDOWN",
                          "nums": self.bo_nums(side, rng, ltp, sig, self.universe[sym]["tick"], candle_open=o,
                                               candle_colour="green" if side == "LONG" else "red",
                                               move_pct=self.pct(move), gap_pct=self.pct(strategy.range_gap(side, rng, o)))})
        band = sorted((c for c in cands if strategy.in_move_band(c["move"])), key=lambda c: c["move"], reverse=True)
        log.info("OPEN 5m check: %d of %d 1H ranges broken at the open, %d with the 5m candle in the break direction, "
                 "%d with a first 5m move of %.1f-%.1f%%", broken, len(self.ranges), len(cands), len(band),
                 config.OPEN_BO_MIN_MOVE_PCT * 100, config.OPEN_BO_MAX_MOVE_PCT * 100)
        for c in cands:
            if c not in band:
                log_signal(now, c["sym"], "OPEN 5m", c["sig"]["detail"],
                           f"skipped: first 5m move {c['move'] * 100:.2f}% outside "
                           f"{config.OPEN_BO_MIN_MOVE_PCT * 100:g}-{config.OPEN_BO_MAX_MOVE_PCT * 100:g}%")
                self.candidate(now, c["book"], c["sym"], c["side"], check, None, reason="outside move band", **c["nums"])
        taken, rank, late = {}, {"1H BREAKOUT": 0, "1H BREAKDOWN": 0}, False
        for c in band:
            self.tick()
            book, sym = c["book"], c["sym"]
            rank[book] += 1
            if taken.get(book) or not self.trader.can_enter(book):
                self.candidate(now, book, sym, c["side"], check, rank[book],
                               reason="ranked lower" if taken.get(book) else "book busy", **c["nums"])
                continue
            if late or now_ist().time() >= OPEN_CANDLE_END:
                if not late:
                    log_signal(now_ist(), sym, "OPEN 5m", "check ran past 09:20", "stopped: first 5m candle closed")
                late = True
                self.candidate(now, book, sym, c["side"], check, rank[book], reason="check ran past 09:20", **c["nums"])
                continue
            res = self.try_enter(now_ist(), sym, c["sig"], book, c["sig"]["detail"] + f"; first 5m move {c['move'] * 100:.2f}%")
            self.candidate(now, book, sym, c["side"], check, rank[book], res, **c["nums"])
            if res.get("taken"):
                taken[book] = sym

    def exec_sleep(self, seconds):
        """Between entry child orders: keep handling commands and open positions."""
        end = time.time() + seconds
        self.tick()
        left = end - time.time()
        if left > 0:
            time.sleep(left)

    def try_enter(self, now, sym, sig, strat, setup):
        """Returns {"taken", "reason" (short, for candidates.csv), "size", "stop_used", "stop_pct", "entry_check",
        + the execution fields}. Size: margin lots, then the liquidity cap (LIQ_CAP_PCT_OF_DEPTH of the top-5
        depth on our side); entry: capped IOC children on the future (execution.run_entry; paper = simulated on the
        live depth); the levels are then rebuilt on the actual average fill and filled quantity."""
        info, side = self.universe[sym], sig["side"]
        opening = sig.get("open_5m", False)          # 09:19:30 OPEN 5m breakout / breakdown
        kind = f"TRIGGER {strat}" + (" OPEN 5m" if opening else "")
        res = {"taken": False, "reason": ""}

        def skip(reason, action):
            log_signal(now, sym, kind, setup, action)
            res["reason"] = reason
            return res
        fut = info["future"]
        try:
            fbook = self.client.depth(fut)             # the future's 5-level depth at signal time
            dbook = fbook if sym == fut else self.client.depth(sym)
        except Exception as e:
            return skip("order book (lookup failed)", f"skipped: depth lookup failed ({e})")
        ok, share = strategy.imbalance_ok(side, dbook["totalbuyqty"], dbook["totalsellqty"])
        book = f"{'bids' if side == 'LONG' else 'asks'} {share * 100:.0f}% of book"
        if config.IMBALANCE_FILTER and not ok:
            return skip("order book", f"skipped: order book against it ({book})")
        setup = f"{setup} | {book}"
        if not self.trader.paper and self.trader.future_held_elsewhere(fut, strat):
            return skip("held by another book", f"skipped: {fut} already held by another strategy (live mode)")
        balance = self.trader.balance(strat)   # this strategy's own capital
        try:
            per_lot = self.client.margin(fut, info["lot"], side)
            lots = strategy.lots_for(balance, per_lot, config.MAX_LOTS)
            margin = 0.0
            while lots > 0:  # confirm the full order's margin fits; step down a lot if it doesn't
                margin = self.client.margin(fut, info["lot"] * lots, side) if lots > 1 else per_lot
                if margin <= balance:
                    break
                lots -= 1
        except Exception as e:
            return skip("margin (lookup failed)", f"skipped: margin lookup failed ({e})")
        if lots < 1:
            return skip("margin", f"skipped: 1 lot needs ₹{per_lot:,.0f} > balance")
        lot = info["lot"]
        wanted = lots * lot                            # margin size, before the liquidity cap
        levels_ours = fbook["asks"] if side == "LONG" else fbook["bids"]
        capped, max_qty, depth5 = execution.liquidity_cap(lots * lot, lot, levels_ours, config.LIQ_CAP_PCT_OF_DEPTH)
        res.update(size=lots, depth5_qty=depth5, liq_cap_qty=max_qty, liq_capped="yes" if capped < lots * lot else "no")
        if capped < lot:
            return skip("too little liquidity", f"skipped: too little liquidity: {config.LIQ_CAP_PCT_OF_DEPTH:.0%} of the "
                                                f"top-5 {'asks' if side == 'LONG' else 'bids'} ({depth5:g}) < 1 lot ({lot})")
        if capped < lots * lot:
            setup = f"{setup} | liquidity cap {lots} -> {capped // lot} lot(s) (top-5 depth {depth5:g})"
            lots = int(capped // lot)
        res["size"] = lots
        qty = lot * lots
        setup = f"{lots} lot(s) | {setup}"
        q = self.client.ltp([sym, fut])
        ltp, fut_ltp = q.get(sym), q.get(fut)   # spot builds the levels; future is what we buy/sell
        if not fut_ltp:
            return skip("no price", f"skipped: no price for {fut}")
        if sig.get("entry_below") and ltp:
            ok = ltp < sig["entry_below"]
            res["entry_check"] = f"{ltp:g} {'<' if ok else '>='} open {sig['entry_below']:g}"
            if not ok:
                return skip("entry check", f"skipped: price {ltp} not below the red candle's body high {sig['entry_below']}")
        if sig.get("entry_above") and ltp:
            ok = ltp > sig["entry_above"]
            res["entry_check"] = f"{ltp:g} {'>' if ok else '<='} open {sig['entry_above']:g}"
            if not ok:
                return skip("entry check", f"skipped: price {ltp} not above the green candle's body low {sig['entry_above']}")
        trail, gap = False, None
        try:   # trend-day check: today's open vs the day's high (short) / low (long) so far
            d5 = self.client.history(sym, config.SHORT_TF, days=1)
            d5 = d5[d5["ts"].dt.date == now.date()] if not d5.empty else d5
            if not d5.empty and ltp:
                trail, gap = strategy.open_at_extreme(side, float(d5["open"].iloc[0]),
                                                      max(float(d5["high"].max()), ltp), min(float(d5["low"].min()), ltp))
        except Exception as e:
            log.warning("open/extreme check %s: %s", sym, e)
        if (config.RSI_ALWAYS_TRAIL and strat.startswith("2H RSI")) or opening:
            trail, gap = True, None
            setup = f"{setup} | trailing, no target"
        if gap is not None:
            setup = (f"{setup} | open {'≈ high' if side == 'SHORT' else '≈ low'} ({gap * 100:.2f}%) → trailing, no target"
                     if trail else f"{setup} | open vs day {'high' if side == 'SHORT' else 'low'} {gap * 100:.2f}% → 2R target")
        max_loss = config.MAX_LOSS_PCT * balance
        min_stop = config.OPEN_BO_MIN_STOP_PCT if opening else config.SHORT_MIN_STOP_PCT if strat == "2H RSI SHORT" else \
            config.LONG_MIN_STOP_PCT if strat == "2H RSI LONG" else 0.0
        lv = strategy.levels(side, ltp, sig["stop_ref"], max_loss, qty, info["tick"], trail, min_stop) if ltp else None
        if not lv:
            return skip("price past stop", f"skipped: price {ltp} already past stop level")
        # ---- capped IOC entry on the future ----
        venue = (execution.PaperVenue(lambda: (lambda d: (d["bids"], d["asks"]))(self.client.depth(fut)))
                 if self.trader.paper else FyersLiveVenue(self.client, fut))
        ex = execution.run_entry(venue, side, qty, lot, info["fut_tick"], config.ENTRY_SLIP_CAP_PCT,
                                 (fbook["bids"], fbook["asks"]), sleep=self.exec_sleep,
                                 max_children=config.ENTRY_MAX_CHILDREN)
        filled, avg = int(round(ex["filled"])), ex["avg"]
        res.update(entry_ref=ex["ref"], entry_cap=ex["cap"], entry_children=ex["children"], qty_intended=qty,
                   qty_filled=filled)
        if filled < lot or not avg:
            return skip("no fill within cap", f"skipped: no fill within cap (ref {ex['ref']}, cap {ex['cap']}, "
                                              f"{ex['children']} IOC child order(s))")
        pct, money = execution.slippage("BUY" if side == "LONG" else "SELL", ex["ref"], avg, filled)
        res.update(entry_avg=round(avg, 4), entry_slip_pct=round(pct, 4), entry_slip_money=round(money, 2),
                   size=filled // lot)
        fill = {"entry_ref": ex["ref"], "entry_avg": round(avg, 4), "entry_slip_pct": round(pct, 4),
                "entry_slip_inr": round(money, 2), "entry_children": ex["children"], "qty_intended": qty,
                "qty_filled": filled, "liq_capped": res["liq_capped"], "qty_wanted": wanted}
        exec_txt = (f"fill {filled}/{qty} @ {avg:.2f} (ref {ex['ref']}, cap {ex['cap']}, {ex['children']} IOC, "
                    f"slippage {pct:+.3f}% = ₹{money:,.0f})")
        setup = f"{setup} | {exec_txt}"
        qty = filled
        if sym == fut:   # FUTURES mode: the levels are rebuilt on the actual average fill and filled quantity
            ltp = avg
            lv = strategy.levels(side, avg, sig["stop_ref"], max_loss, qty, info["tick"], trail, min_stop)
            if not lv:
                if not self.trader.paper:
                    self.trader.client.place_order(fut, qty, -1 if side == "LONG" else 1, 2)
                    self.tg.send(f"⚠️ {fut}: filled {qty} @ {avg:.2f}, already past the stop: closed at market.")
                return skip("fill past stop", f"skipped: average fill {avg:.2f} already past the stop level")
        res.update(stop_used=lv["stop"], stop_pct=abs(lv["stop"] - ltp) / ltp * 100)
        if opening and lv["stop_basis"] != "candle":
            candle_stop = sig["stop_ref"] + (info["tick"] if side == "SHORT" else -info["tick"])
            setup = (f"{setup} | 5m stop {candle_stop:g} ({abs(candle_stop - ltp) / ltp * 100:.2f}%) "
                     + (f"widened to {config.OPEN_BO_MIN_STOP_PCT:.1%}" if lv["stop_basis"].startswith("min")
                        else "capped at 10% capital") + f" -> SL {lv['stop']}")
            log.info("OPEN 5m %s: %s", sym, setup.rsplit(" | ", 1)[-1])
        log_signal(now, sym, kind, setup, f"ENTER {fut} @ {avg:.2f} | {config.PRICE_LABEL.lower()} {ltp:g} SL {lv['stop']} "
                                          f"TP {lv['target'] if lv['target'] is not None else 'trail'}")
        try:
            self.trader.open(strat, sym, fut, side, qty, ltp, avg, lv, margin, setup, info["fut_tick"], fill=fill,
                             lot=lot)
            res["taken"] = True
        except Exception as e:
            self.tg.send(f"⚠️ Entry failed on {sym}: {e}")
            res["reason"] = f"entry failed: {str(e)[:80]}"
        return res

    # ---------------- main loop ----------------
    def run(self):
        self.tg.send(f"🤖 Fyers bot started — {'PAPER' if self.trader.paper else '⚠️ LIVE'} mode, "
                     f"{len(BOOKS)} independent strategies × ₹{config.STARTING_CAPITAL:,.0f}, "
                     + (f"scanning and trading the futures of {config.UNIVERSE} F&O "
                        f"{'stocks + indices' if config.INCLUDE_INDICES else 'stocks'} (signals on the future's price)."
                        if config.SIGNAL_SOURCE == "FUTURES" else
                        f"scanning spot of {config.UNIVERSE} F&O stocks, trading their futures."))
        while True:
            try:
                self.step()
            except AuthError as e:
                self.tg.send(f"🔑 Fyers session expired ({e}).")
                self.prompt_login()
            except Exception:
                log.error(traceback.format_exc())
                self.tg.send("⚠️ Error in bot loop (see bot.log). Retrying in 30s.")
                time.sleep(30)
            time.sleep(1)

    def step(self):
        now = now_ist()
        today, t = now.date(), now.time()
        for m in self.tg.messages():
            self.handle_command(m)

        if not self.client.token_valid():
            # weekdays only; for a special weekend session, send /login yourself
            if today.weekday() < 5 and self.login_prompted != today and \
                    (t >= config.LOGIN_PROMPT_TIME or self.login_prompted is None):
                self.prompt_login()
            return

        if self.universe_day != today:
            self.universe = self.client.load_universe(today)
            self.universe_day = today
            self.trader.new_day(today)
            self.next_arm_refresh = None
            log.info("universe loaded: %d symbols (signals on %s)", len(self.universe), config.PRICE_LABEL.lower())

        self.tick()

        # paper: limit exits on every open position from 15:10 (what is left goes at market at 15:15)
        if config.SQUARE_OFF_LIMIT <= t < config.SQUARE_OFF and self.trader.positions() and \
                time.time() - self.last_square_try >= 5:
            self.last_square_try = time.time()
            self.trader.square_off_limits()

        # square off at 15:15; keeps retrying every 5s if a live exit order fails
        if t >= config.SQUARE_OFF and self.trader.positions() and time.time() - self.last_square_try >= 5:
            self.last_square_try = time.time()
            self.trader.square_off()

        if not self.is_market_live(now):
            return

        if self.next_arm_refresh is None or now >= self.next_arm_refresh:
            self.refresh_arms(now)

        in_window = config.ENTRY_START <= t < config.ENTRY_CUTOFF
        b5, b15 = floor_to(now, 5), floor_to(now, 15)
        if b5 != self.last5 and now >= b5 + timedelta(seconds=8):
            self.last5 = b5
            if in_window and self.trader.can_enter("2H RSI SHORT"):
                self.check_triggers(now, "SHORT")
        if b15 != self.last15 and now >= b15 + timedelta(seconds=8):
            self.last15 = b15
            if in_window and self.trader.can_enter("2H RSI LONG"):
                self.check_triggers(now, "LONG")

        if self.open_bo_day != today and t >= config.BREAKOUT_OPEN_CHECK:
            self.open_bo_day = today                  # once a day, even if it fails
            if t < OPEN_CANDLE_END:
                if self.trader.can_enter("1H BREAKOUT") or self.trader.can_enter("1H BREAKDOWN"):
                    self.check_open_breakouts(now)
            else:
                log.info("OPEN 5m check skipped: first reached at %s, after the first 5m candle", t.strftime("%H:%M:%S"))

        bo_time = b15 + timedelta(minutes=config.BREAKOUT_CHECK_MINUTE)
        if b15 != self.last_bo and bo_time <= now < b15 + timedelta(minutes=15):
            self.last_bo = b15
            if in_window and (self.trader.can_enter("1H BREAKOUT") or self.trader.can_enter("1H BREAKDOWN")):
                self.check_breakouts(now, b15)

        if t >= config.MARKET_CLOSE and self.summary_sent != today:
            self.summary_sent = today
            self.tg.send(f"📊 Day summary {today}\n" + self.trader.report(day=today) +
                         f"\n\n📈 Since {self.trader.state['books_since'][:10]}\n" + self.trader.report())


if __name__ == "__main__":
    Bot().run()
