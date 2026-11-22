"""Position handling for paper and live modes.

Four independent books, one per strategy (2H RSI SHORT, 2H RSI LONG, 1H BREAKOUT, 1H BREAKDOWN). Each book has its own
capital (STARTING_CAPITAL each), its own balance, one position at a time, its own daily loss limit and its own
results. Levels (stop, target, breakeven) are on the price named by config.PRICE_LABEL (the future itself in
FUTURES mode); entry and exit orders go to the future at market. P&L is the future's P&L."""
import csv
import json
import logging
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config
import strategy

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("trader")
STATE_FILE = config.DATA_DIR / "state.json"
TRADES_CSV = config.DATA_DIR / "trades.csv"
SIGNALS_CSV = config.DATA_DIR / "signals.csv"
FILLED, CANCELLED, REJECTED = 2, 1, 5
BOOKS = ("2H RSI SHORT", "2H RSI LONG", "1H BREAKOUT", "1H BREAKDOWN")
LABELS = {}     # optional display names for books


def label(name):
    return LABELS.get(name, name)
TRADE_COLS = ["mode", "strategy", "spot", "future", "side", "qty", "entry_time",
              "spot_entry", "spot_stop", "stop_basis", "spot_target", "moved_to_entry",
              "fut_entry", "exit_time", "spot_exit", "fut_exit", "reason",
              "gross_pnl", "costs", "net_pnl", "balance", "setup", "trail", "trail_stop_r"]


def log_signal(ts, symbol, kind, detail, action):
    new = not SIGNALS_CSV.exists()
    with SIGNALS_CSV.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "symbol", "type", "detail", "action"])
        w.writerow([ts.strftime("%Y-%m-%d %H:%M:%S"), symbol, kind, detail, action])


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
    def open(self, name, spot, future, side, qty, spot_entry, fut_ref, lv, margin, setup, fut_tick):
        """name: the book (= strategy). spot_entry: price the levels were built from. fut_ref: future LTP."""
        bk = self.book(name)
        fut_entry, entry_id, backup_id, backup_stop = fut_ref, None, None, None
        if not self.paper:
            entry_id = self.client.place_order(future, qty, 1 if side == "LONG" else -1, 2)
            fut_entry = self._wait_fill(entry_id) or fut_ref
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
            "exit_error": None}
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
                    + (f"\nBackup stop on future: {backup_stop}" if backup_stop else "") + f"\n{setup}")

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
        for name, p in open_:
            self.check(name, q.get(p["spot"]), q.get(p["future"]))

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
        long_ = p["side"] == "LONG"
        if (long_ and spot_ltp <= p["stop"]) or (not long_ and spot_ltp >= p["stop"]):
            why = f"TRAIL STOP ({p['trail_r'] - config.TRAIL_GAP_R}R)" if p.get("trail_r") else \
                "BREAKEVEN STOP" if p["at_breakeven"] else "STOP"
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
        """Close every open position (all books)."""
        open_ = self.positions()
        if not open_:
            return
        syms = sorted({s for _, p in open_ for s in (p["spot"], p["future"])})
        q = self.client.ltp(syms)
        for name, p in open_:
            self._exit(name, reason, q.get(p["spot"]), q.get(p["future"]))

    def _exit(self, name, reason, spot_px, fut_ltp):
        """Close the future at market."""
        p = self.book(name)["position"]
        if self.paper:
            fut_px = fut_ltp if fut_ltp else self.client.ltp([p["future"]]).get(p["future"], p["fut_entry"])
            self._close(name, reason, fut_px, spot_px)
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
        self._close(name, reason, fut_px, spot_px)

    def _close(self, name, reason, fut_exit, spot_exit):
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
            if lines and "trail_stop_r" not in lines[0]:
                lines[0] = ",".join(TRADE_COLS) + "\n"     # add the trail columns to the header
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
                        p.get("trail", False), (p["trail_r"] - config.TRAIL_GAP_R) if p.get("trail_r") else ""])
        emoji = "✅" if pnl > 0 else "❌"
        self.notify(f"{emoji} EXIT {short_name(p['spot'])} [{label(name)}] ({reason})\n"
                    + (f"Future {p['future']} @ {fut_exit}\n" if p["spot"] == p["future"] else
                       f"Spot {spot_exit} | Future {p['future']} @ {fut_exit}\n") +
                    f"Net P&L ₹{pnl:,.0f} | {label(name)} balance ₹{bk['balance']:,.0f}")
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
