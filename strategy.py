"""Pure strategy logic: no API calls here, so it can be tested on any candles."""
import math
from datetime import datetime

import pandas as pd

import config
from indicators import ema, rsi


def candle_ends(df: pd.DataFrame, tf_minutes: int) -> pd.Series:
    """End time of each candle, capped at the 15:30 session close."""
    ends = df["ts"] + pd.Timedelta(minutes=tf_minutes)
    session_close = df["ts"].dt.normalize() + pd.Timedelta(hours=15, minutes=30)
    return ends.where(ends <= session_close, session_close)


def closed_candles(df: pd.DataFrame, tf_minutes: int, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    return df[candle_ends(df, tf_minutes) <= now].reset_index(drop=True)


def next_close_time(df: pd.DataFrame, tf_minutes: int, now: datetime):
    """When the candle currently forming will close (None if nothing is forming)."""
    if df.empty:
        return None
    ends = candle_ends(df, tf_minutes)
    future = ends[ends > now]
    return None if future.empty else future.min().to_pydatetime()


def arm_state(df_closed: pd.DataFrame):
    """Returns ('SHORT'|'LONG'|None, rsi_value, candle_ts) from the last CLOSED 2H candle."""
    if len(df_closed) < config.RSI_PERIOD + 5:
        return None, None, None
    r = rsi(df_closed["close"], config.RSI_PERIOD)
    val = float(r.iloc[-1])
    ts = df_closed["ts"].iloc[-1]
    if val > config.RSI_OVERBOUGHT:
        return "SHORT", val, ts
    if val < config.RSI_OVERSOLD:
        return "LONG", val, ts
    return None, val, ts


def short_trigger(df5_closed: pd.DataFrame, today):
    """Green candle A with low above 5 EMA, then next candle B closes red -> short.
    Both candles must be from today. Returns dict or None."""
    if len(df5_closed) < config.EMA_PERIOD + 2:
        return None
    e = ema(df5_closed["close"], config.EMA_PERIOD)
    a, b = df5_closed.iloc[-2], df5_closed.iloc[-1]
    if a["ts"].date() != today or b["ts"].date() != today or a["ts"].time() < config.ENTRY_START:
        return None
    a_green = a["close"] > a["open"]
    a_above_ema = a["low"] > e.iloc[-2]
    b_red = b["close"] < b["open"]
    if a_green and a_above_ema and b_red:
        return {"side": "SHORT", "stop_ref": float(a["high"]), "trigger_ts": b["ts"],
                "detail": f"A {a['ts']:%H:%M} low {a['low']} > EMA5 {e.iloc[-2]:.2f}; B {b['ts']:%H:%M} red close {b['close']}"}
    return None


def long_trigger(df15_closed: pd.DataFrame, today):
    """15m: C1 with its high below the 5 EMA, then C2's low above C1's low -> long at C2's close.
    Stop below C1's low. Both candles from today."""
    if len(df15_closed) < config.EMA_PERIOD + 2:
        return None
    e = ema(df15_closed["close"], config.EMA_PERIOD)
    c1, c2 = df15_closed.iloc[-2], df15_closed.iloc[-1]
    if c1["ts"].date() != today or c2["ts"].date() != today or c1["ts"].time() < config.ENTRY_START:
        return None
    if c1["high"] < e.iloc[-2] and c2["low"] > c1["low"]:
        return {"side": "LONG", "stop_ref": float(c1["low"]), "trigger_ts": c2["ts"],
                "detail": f"C1 {c1['ts']:%H:%M} high {c1['high']} < 5EMA {e.iloc[-2]:.2f}; "
                          f"C2 {c2['ts']:%H:%M} low {c2['low']} > C1 low {c1['low']}"}
    return None


def _div_trigger(df_closed, today, side, window_min):
    """RSI divergence on closed candles of today (see config.RSI_ENTRY). The last closed candle is the divergence
    candidate. First peak (short) = RED candle closing above EMA5 with its low above it; first dip (long) = GREEN
    candle with its high below EMA5. Among those within window_min and at least RSI_DIV_MIN_GAP candles before the
    candidate (that many candles in between), the highest peak / lowest dip is the reference. The RSI must differ by
    at least RSI_DIV_MIN_RSI_DIFF (short: peak - divergence, long: divergence - dip)."""
    if len(df_closed) < max(config.EMA_PERIOD, config.RSI_PERIOD) + 3:
        return None
    df = df_closed
    e = ema(df["close"], config.EMA_PERIOD).values
    r = rsi(df["close"], config.RSI_PERIOD).values
    d = df.iloc[-1]
    n = len(df) - 1
    if d["ts"].date() != today:
        return None
    if side == "SHORT":
        is_ref = (df["close"] < df["open"]) & (df["close"] > e) & (df["low"] > e)
    else:
        is_ref = (df["close"] > df["open"]) & (df["high"] < e)
    ref = None
    for k in range(n - 1 - config.RSI_DIV_MIN_GAP, -1, -1):
        if df["ts"].iloc[k].date() != today or df["ts"].iloc[k].time() < config.ENTRY_START:
            break
        if (d["ts"] - df["ts"].iloc[k]).total_seconds() / 60 > window_min:
            break
        if is_ref.iloc[k] and (ref is None or (df["high"].iloc[k] > df["high"].iloc[ref] if side == "SHORT"
                                               else df["low"].iloc[k] < df["low"].iloc[ref])):
            ref = k
    if ref is None:
        return None
    p = df.iloc[ref]
    min_diff = config.RSI_DIV_MIN_RSI_DIFF
    if side == "SHORT":
        diff = r[ref] - r[n]
        ok = d["close"] < d["open"] and d["high"] > p["high"] and diff >= min_diff
        if not ok:
            return None
        return {"side": "SHORT", "stop_ref": float(d["high"]), "trigger_ts": d["ts"], "entry_below": float(d["open"]),
                "detail": f"red peak {p['ts']:%H:%M} high {p['high']} RSI {r[ref]:.1f} (above EMA5, {n - ref - 1} candles between); "
                          f"red {d['ts']:%H:%M} high {d['high']} RSI {r[n]:.1f}; RSI diff {diff:.1f} (>= {min_diff:g}) "
                          f"-> bearish divergence"}
    diff = r[n] - r[ref]
    ok = d["close"] > d["open"] and d["low"] < p["low"] and diff >= min_diff
    if not ok:
        return None
    return {"side": "LONG", "stop_ref": float(d["low"]), "trigger_ts": d["ts"], "entry_above": float(d["open"]),
            "detail": f"green dip {p['ts']:%H:%M} low {p['low']} RSI {r[ref]:.1f} (high < EMA5, {n - ref - 1} candles between); "
                      f"green {d['ts']:%H:%M} low {d['low']} RSI {r[n]:.1f}; RSI diff {diff:.1f} (>= {min_diff:g}) "
                      f"-> bullish divergence"}


def short_div_trigger(df5_closed, today):
    return _div_trigger(df5_closed, today, "SHORT", config.RSI_DIV_SHORT_WINDOW_MIN)


def long_div_trigger(df15_closed, today):
    return _div_trigger(df15_closed, today, "LONG", config.RSI_DIV_LONG_WINDOW_MIN)


def lots_for(balance, margin_per_lot, max_lots=0):
    """As many whole lots as the balance can margin (optionally capped)."""
    if margin_per_lot <= 0:
        return 0
    n = int(balance // margin_per_lot)
    return min(n, max_lots) if max_lots > 0 else n


def _round_tick(price, tick, mode):
    n = price / tick
    n = math.ceil(n - 1e-9) if mode == "up" else math.floor(n + 1e-9) if mode == "down" else round(n)
    return round(n * tick, 2)


def open_at_extreme(side, day_open, day_high, day_low, tol=None):
    """Short: today's open ~ day high. Long: open ~ day low. Returns (ok, gap as a share of the open)."""
    tol = config.OPEN_EXTREME_TOL if tol is None else tol
    if not day_open:
        return False, None
    gap = (day_high - day_open) / day_open if side == "SHORT" else (day_open - day_low) / day_open
    return gap <= tol, gap


def trail_stop(side, entry, risk, price, stop, tick):
    """Whole-R trail: once price reaches TRAIL_START_R, stop = (R reached - TRAIL_GAP_R) x risk from entry.
    Returns (new_stop, r_reached) when it tightens the current stop, else (None, None)."""
    if not risk or risk <= 0 or price is None:
        return None, None
    sign = 1 if side == "LONG" else -1
    r = math.floor((price - entry) * sign / risk + 1e-9)
    if r < config.TRAIL_START_R:
        return None, None
    new = _round_tick(entry + sign * (r - config.TRAIL_GAP_R) * risk, tick, "down" if side == "LONG" else "up")
    if (side == "LONG" and new > stop) or (side == "SHORT" and new < stop):
        return new, r
    return None, None


def levels(side, entry, stop_ref, max_loss, qty, tick, trail=False, min_stop_pct=0.0):
    """Stop = whichever is closer: one tick beyond the candle, or the price where the loss
    reaches max_loss (10% of the account). Target = RISK_REWARD x the chosen stop distance.
    Breakeven trigger = BREAKEVEN_AT_R x the stop distance in profit. trail=True: no target (trend-day trail).
    min_stop_pct: a candle stop closer than this share of the entry is widened to it (still capped at 10% loss).
    Returns dict or None if invalid."""
    cap_dist = max_loss / qty
    if side == "SHORT":
        candle_stop = stop_ref + tick
        candle_dist = candle_stop - entry
    else:
        candle_stop = stop_ref - tick
        candle_dist = entry - candle_stop
    if candle_dist <= 0:
        return None  # price already beyond the stop level
    widened = candle_dist < entry * min_stop_pct
    if widened:
        candle_dist = entry * min_stop_pct
    dist = min(candle_dist, cap_dist)
    stop_basis = ("10% capital" if candle_dist > cap_dist else f"min {min_stop_pct:.1%}" if widened else "candle")
    if side == "SHORT":
        stop = _round_tick(entry + dist, tick, "down")       # round toward entry so loss stays within the cap
        target = _round_tick(entry - config.RISK_REWARD * dist, tick, "near")
        breakeven_at = _round_tick(entry - config.BREAKEVEN_AT_R * dist, tick, "near")
    else:
        stop = _round_tick(entry - dist, tick, "up")
        target = _round_tick(entry + config.RISK_REWARD * dist, tick, "near")
        breakeven_at = _round_tick(entry + config.BREAKEVEN_AT_R * dist, tick, "near")
    if target <= 0 or stop == _round_tick(entry, tick, "near"):
        return None
    return {"stop": stop, "target": None if trail else target, "breakeven_at": breakeven_at,
            "risk_per_unit": round(dist, 4), "stop_basis": stop_basis, "trail": trail}


# ---------------- 2H candles built from 1H (saves half the API calls) ----------------
def resample_2h(df1h: pd.DataFrame) -> pd.DataFrame:
    """2H candles anchored at the 09:15 open: 09:15, 11:15, 13:15, 15:15 (15:15-15:30 stub)."""
    if df1h.empty:
        return df1h
    d = df1h.copy()
    day = d["ts"].dt.normalize()
    open_min = config.MARKET_OPEN.hour * 60 + config.MARKET_OPEN.minute
    mins = (d["ts"] - day).dt.total_seconds() // 60 - open_min
    slot = (mins // 120).astype(int)
    d["slot"] = day + pd.Timedelta(minutes=open_min) + pd.to_timedelta(slot * 120, unit="m")
    out = d.groupby("slot", sort=True).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                           close=("close", "last"), volume=("volume", "sum"))
    return out.reset_index().rename(columns={"slot": "ts"})


# ---------------- 1H consolidation breakout (long only) ----------------
def consolidation_range(df1h_closed: pd.DataFrame, today=None):
    """Last 3 CLOSED 1H candles (they may include yesterday's, so an afternoon range can break at today's open)
    whose BODIES (open/close) span <= BREAKOUT_RANGE_PCT (1%) of the body low.
    Returns the body range ('high'/'low' = break levels) and the wick range ('wick_high'/'wick_low' = stops)."""
    if df1h_closed is None or len(df1h_closed) < config.BREAKOUT_CANDLES:
        return None
    last = df1h_closed.iloc[-config.BREAKOUT_CANDLES:]
    top = float(last[["open", "close"]].max(axis=1).max())
    bot = float(last[["open", "close"]].min(axis=1).min())
    pct = (top - bot) / bot
    if pct <= config.BREAKOUT_RANGE_PCT:
        return {"high": top, "low": bot, "wick_high": float(last["high"].max()), "wick_low": float(last["low"].min()),
                "pct": pct, "from": str(last["ts"].iloc[0]), "to": str(last["ts"].iloc[-1])}
    return None


def breakout_trigger(rng, candle_open, ltp):
    """At minute 12 of the forming 15m candle.
    Breakout : price above the range's body high and the 15m candle green so far -> LONG, stop below the wicks' low.
    Breakdown: price below the range's body low and the 15m candle red so far   -> SHORT, stop above the wicks' high."""
    if not (rng and ltp and candle_open):
        return None
    span = f"1H range {rng['low']}-{rng['high']} ({rng['pct'] * 100:.2f}%, bodies; wicks {rng['wick_low']}-{rng['wick_high']})"
    if ltp > candle_open and ltp > rng["high"]:
        return {"side": "LONG", "stop_ref": rng["wick_low"], "detail": f"BREAKOUT {span}; 15m open {candle_open}, LTP {ltp}"}
    if ltp < candle_open and ltp < rng["low"]:
        return {"side": "SHORT", "stop_ref": rng["wick_high"], "detail": f"BREAKDOWN {span}; 15m open {candle_open}, LTP {ltp}"}
    return None


def open_breakout_trigger(rng, c5_open, c5_high, c5_low, ltp):
    """09:19:30 opening check (config.BREAKOUT_OPEN_CHECK) on the first 5m candle (09:15-09:20) as it stands.
    Breakout : LTP above the range's body high and the 5m candle green so far -> LONG, stop 1 tick below its low.
    Breakdown: LTP below the range's body low and the 5m candle red so far   -> SHORT, stop 1 tick above its high.
    (levels() adds the tick and the OPEN_BO_MIN_STOP_PCT minimum.)"""
    if not (rng and ltp and c5_open):
        return None
    span = f"1H range {rng['low']}-{rng['high']} ({rng['pct'] * 100:.2f}%, bodies)"
    c5 = f"09:15 5m O {c5_open} H {c5_high} L {c5_low}"
    if ltp > c5_open and ltp > rng["high"]:
        return {"side": "LONG", "stop_ref": min(c5_low, ltp), "open_5m": True,
                "detail": f"OPEN 5m BREAKOUT {span}; {c5}, LTP {ltp}; stop below the 5m low"}
    if ltp < c5_open and ltp < rng["low"]:
        return {"side": "SHORT", "stop_ref": max(c5_high, ltp), "open_5m": True,
                "detail": f"OPEN 5m BREAKDOWN {span}; {c5}, LTP {ltp}; stop above the 5m high"}
    return None


# ---------------- order-book imbalance filter (all strategies) ----------------
def imbalance_ok(side, total_buy, total_sell):
    """Longs need bid qty >= 55% of the book; shorts need ask qty >= 55%. Returns (ok, share)."""
    total = (total_buy or 0) + (total_sell or 0)
    if total <= 0:
        return False, 0.0
    share = (total_buy if side == "LONG" else total_sell) / total
    return share >= config.IMBALANCE_MIN_SHARE, share
