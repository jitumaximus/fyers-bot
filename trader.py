"""Position handling for paper and live modes.

Four independent books, one per strategy (2H RSI SHORT, 2H RSI LONG, 1H BREAKOUT, 1H BREAKDOWN). Each book has its own
capital (STARTING_CAPITAL each), its own balance, one position at a time, its own daily loss limit and its own
results. Levels (stop, target, breakeven) are on the price named by config.PRICE_LABEL (the future itself in
FUTURES mode); orders go to the future (entries: capped IOC limits; exits: market, or limit for big trail-stop exits and
the 15:10 square-off, see config / execution.py). P&L is the future's P&L."""
import csv
import json
import logging
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import execution
import strategy

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("trader")
STATE_FILE = config.DATA_DIR / "state.json"
TRADES_CSV = config.DATA_DIR / "trades.csv"
SIGNALS_CSV = config.DATA_DIR / "signals.csv"
CANDIDATES_CSV = config.DATA_DIR / "candidates.csv"   # every setup that passed at a check, taken or not
# Same columns in the binance-bot's candidates.csv. *_pct are percentages; blank = not applicable / not known.
CAND_COLS = ["time", "bot", "book", "symbol", "side", "check", "taken", "reason", "rank",
             "range_low", "range_high", "candle_open", "ltp", "move_pct", "candle_colour", "gap_pct", "beyond_pct",
             "arm_rsi", "ref_time", "ref_price", "ref_rsi", "div_time", "div_price", "div_rsi", "rsi_gap",
             "div_open", "entry_check", "level", "distance_pct", "stop_candle", "stop_used", "stop_pct", "size",
             # execution (25 Nov 2026); qty_* in shares (fyers) / coins (binance), size = lots / coins traded
             "depth5_qty", "liq_cap_qty", "liq_capped", "entry_ref", "entry_cap", "entry_avg", "entry_slip_pct",
             "entry_slip_money", "entry_children", "qty_intended", "qty_filled"]
LIVE_PRICES = config.DATA_DIR / "live_prices.json"   # read-only feed for the website (mintoro-api): open positions only
LIVE_PRICES_EVERY_S = 3
FILLED, CANCELLED, REJECTED = 2, 1, 5
BOOKS = ("2H RSI SHORT", "2H RSI LONG", "1H BREAKOUT", "1H BREAKDOWN")
LABELS = {}     # optional display names for books


def label(name):
    return LABELS.get(name, name)
TRADE_COLS = ["mode", "strategy", "spot", "future", "side", "qty", "entry_time",
              "spot_entry", "spot_stop", "stop_basis", "spot_target", "moved_to_entry",
              "fut_entry", "exit_time", "spot_exit", "fut_exit", "reason",
              "gross_pnl", "costs", "net_pnl", "balance", "setup", "trail", "trail_stop_r"] + [
              # execution (25 Nov 2026): entry = capped IOC children; exit = market (paper: estimated on the depth).
              # slip_pct / slip_inr: positive = worse than the reference price; qty in shares
              "entry_ref", "entry_avg", "entry_slip_pct", "entry_slip_inr", "entry_children", "qty_intended",
              "qty_filled", "liq_capped", "exit_ref", "exit_avg", "exit_slip_pct", "exit_slip_inr", "exit_children",
              "exit_estimated",
              # 26 Nov 2026: market / limit / limit+market (limit exits: trail stops whose market order would slip
              # > EXIT_SLIP_CAP_PCT, and the 15:10 square-off)
              "exit_type"]


def log_signal(ts, symbol, kind, detail, action):
    new = not SIGNALS_CSV.exists()
    with SIGNALS_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "symbol", "type", "detail", "action"])
        w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, kind, detail, action])


def log_candidate(row):
    """One row in candidates.csv. Never raises: a logging problem must not affect trading."""
    try:
        new = not CANDIDATES_CSV.exists()
        with CANDIDATES_CSV.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CAND_COLS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow({k: ("" if v is None else v) for k, v in row.items()})
    except Exception as e:
        log.warning("candidates.csv: %s", e)


def _tick_round(price, tick, mode):
    n = price / tick
    n = math.ceil(n - 1e-9) if mode == "up" else math.floor(n + 1e-9)
    return round(n * tick, 2)


def short_name(sym):
    return sym.replace("NSE:", "").replace("-EQ", "")


def _new_book():
    return {"balance": config.STARTING_CAPITAL, "day_start_balance": config.STARTING_CAPITAL,
            "day_trades": 0, "position": None}


class Trader:
    def __init__(self, client, notify):
        self.client = client
        self.notify = notify
        self.paper = config.PAPER_MODE
        self.sleep = time.sleep        # between exit child orders; the bot sets its own (keeps monitoring)
        self._busy = set()             # books whose exit child orders are running right now
        old = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        if "books" in old:
            self.state = old
        else:
            # first run of the 3-book version: every strategy starts fresh with STARTING_CAPITAL
            self.state = {"day": old.get("day"), "paused": old.get("paused", False),
                          "books_since": datetime.now(IST).isoformat(timespec="seconds"),
                          "shared_balance_before_split": old.get("balance"),
                          "books": {b: _new_book() for b in BOOKS}}
            p = old.get("position")
            if p and p.get("strategy") in BOOKS:   # carry an open position into its own book
                self.state["books"][p["strategy"]]["position"] = p
                self.state["books"][p["strategy"]]["day_trades"] = 1
        for b in BOOKS:
            self.state["books"].setdefault(b, _new_book())
        # breakdowns used to share the 1H BREAKOUT book: an open 1H short moves into its own 1H BREAKDOWN book
        bo, bd = self.state["books"]["1H BREAKOUT"], self.state["books"]["1H BREAKDOWN"]
        p = bo.get("position")
        if p and p.get("side") == "SHORT" and bd["position"] is None:
            p["strategy"] = "1H BREAKDOWN"
            bd["position"], bo["position"] = p, None
            bd["day_trades"], bo["day_trades"] = bd["day_trades"] + 1, max(0, bo["day_trades"] - 1)
        self.save()

    # ---------- state ----------
    def save(self):
        STATE_FILE.write_text(json.dumps(self.state, indent=2, default=str))

    def book(self, name):
        return self.state["books"][name]

    def positions(self):
        """[(book name, position)] for every book with an open position."""
        return [(b, self.book(b)["position"]) for b in BOOKS if self.book(b)["position"]]

    def balance(self, name):
        return self.book(name)["balance"]

    def total_balance(self):
        return sum(self.balance(b) for b in BOOKS)

    def new_day(self, today):
        if self.state["day"] != str(today):
            self.state["day"] = str(today)
            for b in BOOKS:
                bk = self.book(b)
                bk.update(day_start_balance=bk["balance"], day_trades=0)
            self.save()

    def daily_limit_hit(self, name):
        bk = self.book(name)
        return bk["balance"] <= bk["day_start_balance"] * (1 - config.DAILY_LOSS_LIMIT_PCT)

    def can_enter(self, name):
        return self.book(name)["position"] is None and not self.state["paused"] and not self.daily_limit_hit(name)

    def in_cooldown(self, name, sym, minutes, now):
        """True if this book closed a trade on sym less than `minutes` ago."""
        t = self.state.get("last_exit", {}).get(f"{name}|{sym}")
        return bool(t) and (now - datetime.fromisoformat(t)).total_seconds() < minutes * 60

    def future_held_elsewhere(self, future, name):
        """Live mode: Fyers nets positions per contract, so two books must not hold the same future."""
        return any(p["future"] == future for b, p in self.positions() if b != name)

    # ---------- entry ----------
    def open(self, name, spot, future, side, qty, spot_entry, fut_ref, lv, margin, setup, fut_tick, fill=None, lot=None):
        """name: the book (= strategy). spot_entry: price the levels were built from. fut_ref: future LTP.
        fill: the capped IOC entry already done by the bot (execution.run_entry; qty = what filled): no entry order
        is sent here and fut_ref is its average fill. Without it (old path), a market order in live mode."""
        bk = self.book(name)
        fut_entry, entry_id, backup_id, backup_stop = fut_ref, None, None, None
        if not self.paper and fill is None:
            entry_id = self.client.place_order(future, qty, 1 if side == "LONG" else -1, 2)
            fut_entry = self._wait_fill(entry_id) or fut_ref
        if not self.paper:
            # backup stop on the future, a bit wider than the 10% cap, only for when the bot can't act
            dist = config.BACKUP_LOSS_PCT * bk["balance"] / qty
            backup_stop = (_tick_round(fut_entry - dist, fut_tick, "down") if side == "LONG"
                           else _tick_round(fut_entry + dist, fut_tick, "up"))
            try:
                backup_id = self.client.place_order(future, qty, -1 if side == "LONG" else 1, 3, stop_price=backup_stop)
            except Exception as e:
                self.notify(f"⚠️ Entered {future} but the backup stop order failed: {e}. "
                            f"The bot is still watching its stop.")
        bk["position"] = {
            "spot": spot, "future": future, "strategy": name, "side": side, "qty": qty,
            "spot_entry": spot_entry, "stop": lv["stop"], "target": lv["target"], "initial_stop": lv["stop"],
            "breakeven_at": lv["breakeven_at"], "at_breakeven": False, "stop_basis": lv["stop_basis"],
            "trail": lv.get("trail", False), "risk": lv["risk_per_unit"], "trail_r": None, "tick": fut_tick,
            "fut_entry": fut_entry, "margin": margin, "entry_time": datetime.now(IST).isoformat(timespec="seconds"),
            "setup": setup, "entry_id": entry_id, "backup_id": backup_id, "backup_stop": backup_stop,
            "exit_error": None, "exec": fill or {}, "lot": lot or qty, "pending": None}
        bk["day_trades"] += 1
        self.save()
        risk = abs(spot_entry - lv["stop"]) * qty
        L = config.PRICE_LABEL
        self.notify(f"{'📄 PAPER ' if self.paper else ''}{'🔴 SHORT' if side == 'SHORT' else '🟢 LONG'} "
                    f"{short_name(spot)} [{label(name)}]\n"
                    f"Future {future} qty {qty} @ {fut_entry}\n"
                    f"{L} entry {spot_entry} | {L} stop {lv['stop']} ({lv['stop_basis']}) | "
                    + (f"NO target: trailing (trend day)\n" if lv.get("trail") else f"{L} target {lv['target']}\n") +
                    f"Stop moves to entry when the {L.lower()} reaches {lv['breakeven_at']} (1:{config.BREAKEVEN_AT_R:g})"
                    + (f"; from {config.TRAIL_START_R}R it trails {config.TRAIL_GAP_R}R behind in whole-R steps\n"
                       if lv.get("trail") else "\n") +
                    f"Risk ≈ ₹{risk:,.0f} | Margin ₹{margin:,.0f} | {label(name)} balance ₹{bk['balance']:,.0f}"
                    + (f"\nBackup stop on future: {backup_stop}" if backup_stop else "") + f"\n{setup}"
                    + (f"\n{execution.entry_text(fill)}" if fill else ""))
        log.info("ENTRY [%s] %s %s %s @ %s | %s", name, side, qty, future, fut_entry, execution.entry_text(fill) or "market")

    def _wait_fill(self, order_id, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            status, price = self.client.order_status(order_id)
            if status == FILLED:
                return price
            if status in (CANCELLED, REJECTED):
                raise RuntimeError(f"order {order_id} not filled (status {status})")
            time.sleep(1)
        return None

    # ---------- monitoring ----------
    def check_all(self):
        """Poll every open position with one price call."""
        open_ = self.positions()
        if not open_:
            return
        syms = sorted({s for _, p in open_ for s in (p["spot"], p["future"])})
        q = self.client.ltp(syms)
        self.write_live_prices(q)
        for name, p in open_:
            if name in self._busy:
                continue        # its exit child orders are running (we are inside their sleep)
            self.check(name, q.get(p["spot"]), q.get(p["future"]))

    def write_live_prices(self, q):
        """Latest prices of the open positions' symbols for the website, at most every LIVE_PRICES_EVERY_S seconds.
        Written atomically; any failure is logged and ignored, so it can never affect trading."""
        now = time.time()
        if now - getattr(self, "_live_prices_t", 0) < LIVE_PRICES_EVERY_S:
            return
        self._live_prices_t = now
        try:
            prices = {s: float(v) for s, v in (q or {}).items() if isinstance(v, (int, float)) and v > 0}
            if not prices:
                return
            tmp = LIVE_PRICES.with_suffix(".tmp")
            tmp.write_text(json.dumps({"time": datetime.now(IST).isoformat(timespec="seconds"), "prices": prices}))
            os.replace(tmp, LIVE_PRICES)
        except Exception as e:  # noqa: BLE001
            log.warning("live_prices.json not written: %s", e)

    def check(self, name, spot_ltp, fut_ltp):
        p = self.book(name)["position"]
        if not p:
            return
        if not self.paper and p["backup_id"]:
            status, px = self.client.order_status(p["backup_id"])
            if status == FILLED:
                self.notify(f"⚠️ Backup stop on {p['future']} filled at {px} — the bot's own stop didn't act in time.")
                self._close(name, "BACKUP STOP", px, spot_ltp)
                return
        if spot_ltp is None:
            return
        if p.get("pending"):
            self._step_exit(name, spot_ltp)
            return
        long_ = p["side"] == "LONG"
        if (long_ and spot_ltp <= p["stop"]) or (not long_ and spot_ltp >= p["stop"]):
            why = f"TRAIL STOP ({p['trail_r'] - config.TRAIL_GAP_R}R)" if p.get("trail_r") else \
                "BREAKEVEN STOP" if p["at_breakeven"] else "STOP"
            if p.get("trail_r") and self.paper and self._start_limit_exit(name, why, spot_ltp, kind="trail"):
                return
            self._exit(name, why, spot_ltp, fut_ltp)
            return
        if p.get("target") is not None and \
                ((long_ and spot_ltp >= p["target"]) or (not long_ and spot_ltp <= p["target"])):
            self._exit(name, "TARGET", spot_ltp, fut_ltp)
            return
        if p.get("trail"):
            new, r = strategy.trail_stop(p["side"], p["spot_entry"], p["risk"], spot_ltp, p["stop"],
                                         p.get("tick") or 0.05)
            if new is not None:
                p["stop"], p["trail_r"], p["at_breakeven"] = new, r, True
                self.save()
                self.notify(f"📈 {short_name(p['spot'])} [{label(name)}] reached {r}R at {spot_ltp}. "
                            f"Trailing stop moved to {new} ({r - config.TRAIL_GAP_R}R locked).")
                return
        if not p["at_breakeven"] and \
                ((long_ and spot_ltp >= p["breakeven_at"]) or (not long_ and spot_ltp <= p["breakeven_at"])):
            p["stop"], p["at_breakeven"] = p["spot_entry"], True
            self.save()
            self.notify(f"🛡 {short_name(p['spot'])} [{label(name)}] hit 1:{config.BREAKEVEN_AT_R:g} at {spot_ltp}. "
                        f"Stop moved to entry {p['spot_entry']}.")

    def square_off(self, reason="15:15 SQUARE-OFF"):
        """Close every open position (all books) at market; a pending limit exit sends its rest at market."""
        open_ = self.positions()
        if not open_:
            return
        syms = sorted({s for _, p in open_ for s in (p["spot"], p["future"])})
        q = self.client.ltp(syms)
        for name, p in open_:
            if name in self._busy:
                continue
            if p.get("pending"):
                self._step_exit(name, q.get(p["spot"]), force=reason)
                continue
            self._exit(name, reason, q.get(p["spot"]), q.get(p["future"]))

    def square_off_limits(self):
        """15:10 (paper): every open position without a pending exit starts a limit exit (guard = its stop)."""
        if not self.paper:
            return
        open_ = [(n, p) for n, p in self.positions() if not p.get("pending") and n not in self._busy]
        if not open_:
            return
        q = self.client.ltp(sorted({p["spot"] for _, p in open_}))
        for name, p in open_:
            if not self._start_limit_exit(name, "15:10 SQUARE-OFF", q.get(p["spot"]), kind="squareoff"):
                self._exit(name, "15:10 SQUARE-OFF", q.get(p["spot"]), None)

    # ---------- limit exits (paper) ----------
    def _book(self, fut):
        d = self.client.depth(fut)
        return d["bids"], d["asks"]

    def _start_limit_exit(self, name, reason, spot_ltp, kind):
        """kind "trail": only when a market exit would slip more than EXIT_SLIP_CAP_PCT (returns False otherwise:
        the caller exits at market). kind "squareoff": always. Returns True when the limit exit took over."""
        p = self.book(name)["position"]
        try:
            book = self._book(p["future"])
        except Exception as e:  # noqa: BLE001
            log.warning("limit exit %s: no depth (%s), exiting at market", p["future"], e)
            return False
        take = execution.exit_take_side(p["side"])
        levels = execution.our_side(book, take)
        if not levels:
            return False
        if kind == "trail" and not execution.exit_needs_limit(book, p["side"], p["qty"], config.EXIT_SLIP_CAP_PCT):
            return False
        mslip = execution.market_slip_pct(levels, p["qty"], take)
        sign = 1 if p["side"] == "LONG" else -1
        if kind == "trail":
            ref = p["stop"] if p["spot"] == p["future"] else levels[0][0]     # the trail stop (on the future)
            guard = p["spot_entry"] + sign * config.EXIT_GUARD_R * p["risk"]
        else:
            ref, guard = levels[0][0], p["stop"]
        pend = execution.start_exit(p["side"], p["qty"], p.get("tick") or 0.05, ref, config.EXIT_SLIP_CAP_PCT, guard,
                                    reason, kind)
        pend["unit"] = p.get("lot") or p["qty"]
        p["pending"] = pend
        self.save()
        venue = execution.PaperVenue(lambda: self._book(p["future"]))
        self._busy.add(name)
        try:
            execution.exit_steps(pend, venue, pend["unit"], p.get("tick") or 0.05, book, sleep=self.sleep,
                                 max_children=config.EXIT_MAX_CHILDREN)
        finally:
            self._busy.discard(name)
        why = ("market order would slip " + ("more than the 5 levels hold" if mslip is None else f"{mslip:.3f}%")
               if kind == "trail" else "15:10 square-off")
        msg = (f"{reason} {p['future']} [{label(name)}]: {why} -> limit exit. Floor {pend['floor']:g} (ref {ref:g} "
               f"- {config.EXIT_SLIP_CAP_PCT:.1%}); filled {pend['qty'] - pend['left']:g}/{pend['qty']:g} in "
               f"{pend['children']} order(s)")
        if not execution.exit_done(pend):
            msg += (f"; {pend['left']:g} waiting at {pend['floor']:g}, market if the {config.PRICE_LABEL.lower()} "
                    f"reaches {guard:g}" + (" (its stop)" if kind == "squareoff" else
                                             f" (entry {'+' if sign > 0 else '-'} {config.EXIT_GUARD_R:g}R)")
                    + ", or at 15:15")
        log.info("LIMIT EXIT [%s] %s", name, msg)
        self.notify(f"⏳ {msg}")
        if execution.exit_done(pend):
            self._finish_limit_exit(name, spot_ltp)
        else:
            self.save()
        return True

    def _step_exit(self, name, spot_ltp, force=None):
        """A pending limit exit: the rest at market if the guard is crossed (or force: 15:15), else the resting
        limit at the floor is checked against the current depth."""
        p = self.book(name)["position"]
        pend = p["pending"]
        try:
            book = self._book(p["future"])
        except Exception as e:  # noqa: BLE001
            log.warning("pending exit %s: no depth (%s)", p["future"], e)
            if not force:
                return
            q = self.client.ltp([p["future"]]).get(p["future"]) or p["fut_entry"]
            book = ([(q, p["qty"])], [(q, p["qty"])])
        if force:
            execution.exit_market(pend, book, force)
        elif execution.guard_crossed(p["side"], spot_ltp, pend["guard"]):
            execution.exit_market(pend, book, "stop hit" if pend["kind"] == "squareoff" else
                                  f"price back to entry {'+' if p['side'] == 'LONG' else '-'} {config.EXIT_GUARD_R:g}R")
        else:
            execution.exit_rest(pend, book, pend.get("unit") or p["qty"])
        if execution.exit_done(pend):
            self._finish_limit_exit(name, spot_ltp)
        else:
            self.save()

    def _finish_limit_exit(self, name, spot_ltp):
        p = self.book(name)["position"]
        ex = execution.exit_summary(p["pending"])
        ex["exit_slip_inr"] = round(ex.pop("exit_slip_money") or 0.0, 2)
        ex["exit_avg"] = round(ex["exit_avg"], 4)
        self._close(name, p["pending"]["reason"], ex["exit_avg"], spot_ltp if spot_ltp is not None else ex["exit_avg"], ex)

    def exit_estimate(self, p):
        """Paper: slippage of the market exit estimated by walking the live depth (P&L still uses the LTP).
        Never raises."""
        try:
            d = self.client.depth(p["future"])
            levels = d["bids"] if p["side"] == "LONG" else d["asks"]      # a long exit sells into the bids
            if not levels:
                return {}
            got, avg = execution.walk_market(levels, p["qty"])
            pct, money = execution.slippage("SELL" if p["side"] == "LONG" else "BUY", levels[0][0], avg, got)
            return {"exit_ref": levels[0][0], "exit_avg": round(avg, 4), "exit_slip_pct": round(pct, 4),
                    "exit_slip_inr": round(money, 2), "exit_children": 1,
                    "exit_estimated": "yes" if got >= p["qty"] else f"yes (5 levels hold {got:g} of {p['qty']})"}
        except Exception as e:  # noqa: BLE001
            log.warning("exit slippage estimate %s: %s", p["future"], e)
            return {}

    def _exit(self, name, reason, spot_px, fut_ltp):
        """Close the future at market."""
        p = self.book(name)["position"]
        if self.paper:
            fut_px = fut_ltp if fut_ltp else self.client.ltp([p["future"]]).get(p["future"], p["fut_entry"])
            ex = self.exit_estimate(p)
            ex["exit_type"] = "market"
            self._close(name, reason, fut_px, spot_px, ex)
            return
        try:
            if p["backup_id"]:
                try:
                    self.client.cancel_order(p["backup_id"])
                except Exception as e:
                    log.warning("cancel backup %s: %s", p["backup_id"], e)
            exit_id = self.client.place_order(p["future"], p["qty"], -1 if p["side"] == "LONG" else 1, 2)
            fut_px = self._wait_fill(exit_id) or fut_ltp or p["fut_entry"]
        except Exception as e:
            if p.get("exit_error") != str(e):   # alert once per distinct error; retried every few seconds
                p["exit_error"] = str(e)
                self.save()
                self.notify(f"🚨 EXIT FAILED on {p['future']} [{label(name)}] ({reason}): {e}. Retrying — check Fyers now.")
            return
        ex = {}
        if fut_ltp:   # live: the fill vs the future's LTP when the exit was decided
            pct, money = execution.slippage("SELL" if p["side"] == "LONG" else "BUY", fut_ltp, fut_px, p["qty"])
            ex = {"exit_ref": fut_ltp, "exit_avg": fut_px, "exit_slip_pct": round(pct, 4),
                  "exit_slip_inr": round(money, 2), "exit_children": 1, "exit_estimated": "no", "exit_type": "market"}
        self._close(name, reason, fut_px, spot_px, ex)

    def _close(self, name, reason, fut_exit, spot_exit, ex=None):
        bk = self.book(name)
        p = bk["position"]
        sign = 1 if p["side"] == "LONG" else -1
        gross = (fut_exit - p["fut_entry"]) * p["qty"] * sign
        cost = config.COST_PER_ROUND_TRIP if self.paper else 0.0  # live: check Fyers contract note
        pnl = gross - cost
        bk["balance"] = round(bk["balance"] + pnl, 2)
        bk["position"] = None
        self.state.setdefault("last_exit", {})[f"{name}|{p['spot']}"] = datetime.now(IST).isoformat(timespec="seconds")
        self.save()
        if TRADES_CSV.exists() and "spot_entry" not in TRADES_CSV.open().readline():
            TRADES_CSV.rename(TRADES_CSV.with_name("trades_futures_only.csv"))   # old column layout: archive it
        if TRADES_CSV.exists():
            lines = TRADES_CSV.read_text().splitlines(keepends=True)
            if lines and "exit_type" not in lines[0]:
                lines[0] = ",".join(TRADE_COLS) + "\n"     # add the trail / execution columns to the header
                TRADES_CSV.write_text("".join(lines))
        new = not TRADES_CSV.exists()
        with TRADES_CSV.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(TRADE_COLS)
            w.writerow(["PAPER" if self.paper else "LIVE", name, p["spot"], p["future"], p["side"], p["qty"],
                        p["entry_time"], p["spot_entry"], p["initial_stop"], p["stop_basis"], p["target"],
                        p["at_breakeven"], p["fut_entry"], datetime.now(IST).isoformat(timespec="seconds"),
                        spot_exit, fut_exit, reason, round(gross, 2), cost, round(pnl, 2), bk["balance"], p["setup"],
                        p.get("trail", False), (p["trail_r"] - config.TRAIL_GAP_R) if p.get("trail_r") else ""]
                       + [(p.get("exec") or {}).get(k, "") for k in
                          ("entry_ref", "entry_avg", "entry_slip_pct", "entry_slip_inr", "entry_children",
                           "qty_intended", "qty_filled", "liq_capped")]
                       + [(ex or {}).get(k, "") for k in
                          ("exit_ref", "exit_avg", "exit_slip_pct", "exit_slip_inr", "exit_children", "exit_estimated",
                           "exit_type")])
        emoji = "✅" if pnl > 0 else "❌"
        self.notify(f"{emoji} EXIT {short_name(p['spot'])} [{label(name)}] ({reason})\n"
                    + (f"Future {p['future']} @ {fut_exit}\n" if p["spot"] == p["future"] else
                       f"Spot {spot_exit} | Future {p['future']} @ {fut_exit}\n") +
                    f"{execution.exit_text(ex)}\n"
                    f"Net P&L ₹{pnl:,.0f} | {label(name)} balance ₹{bk['balance']:,.0f}")
        log.info("EXIT [%s] %s %s (%s) @ %s | %s | net %.0f", name, p["future"], p["side"], reason, fut_exit,
                 execution.exit_text(ex), pnl)
        if self.daily_limit_hit(name):
            self.notify(f"🛑 [{label(name)}] daily loss limit ({config.DAILY_LOSS_LIMIT_PCT:.0%}) reached. "
                        f"No new entries for this strategy today.")

    # ---------- reports ----------
    def _book_trades(self, day=None):
        """Trades since the 3-book split (optionally one day), grouped by book."""
        out = {b: [] for b in BOOKS}
        if not TRADES_CSV.exists():
            return out
        since = self.state.get("books_since", "")
        with TRADES_CSV.open() as f:
            for r in csv.DictReader(f):
                if r.get("exit_time", "") < since or r.get("strategy") not in out:
                    continue
                if day and not r["exit_time"].startswith(str(day)):
                    continue
                out[r["strategy"]].append(float(r["net_pnl"]))
        return out

    def report(self, day=None):
        """Per-strategy summary text: today's (day given) or since the 3-book split."""
        trades = self._book_trades(day)
        lines = []
        for b in BOOKS:
            bk, pnls = self.book(b), trades[b]
            wins = sum(x > 0 for x in pnls)
            pnl = (bk["balance"] - bk["day_start_balance"]) if day else (bk["balance"] - config.STARTING_CAPITAL)
            base = bk["day_start_balance"] if day else config.STARTING_CAPITAL
            lines.append(f"{label(b)}\n  Trades {len(pnls)} | Wins {wins} | P&L ₹{pnl:,.0f} ({pnl / base * 100:+.1f}%)"
                         f"\n  Balance ₹{bk['balance']:,.0f}"
                         + (" | 🛑 limit hit" if day and self.daily_limit_hit(b) else ""))
        total = self.total_balance()
        start = sum(self.book(b)["day_start_balance"] for b in BOOKS) if day else config.STARTING_CAPITAL * len(BOOKS)
        lines.append(f"ALL {len(BOOKS)}: P&L ₹{total - start:,.0f} | Balance ₹{total:,.0f}")
        return "\n".join(lines)
