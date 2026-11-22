#!/usr/bin/env python3
"""
One-time download of 5-minute SPOT candles for every NSE stock that has futures (same universe as the bot),
for backtesting. Uses the bot's existing Fyers login (data/token.json) - log in via the bot's Telegram first.

Run it from the LIVE bot folder (where .env and the login token are), with the bot's own Python,
ideally AFTER market hours (so it doesn't compete with the bot for API limits):
    cp ~/trading-lab/fyers_bot/download_history.py ~/fyers_bot/
    cd ~/fyers_bot && venv/bin/python download_history.py --days 730
Output: ~/trading-lab/data_nse/<UNDERLYING>_5m.csv.gz  (+ universe.json with lot sizes / ticks)
        --futures: ~/trading-lab/data_nse_fut/ with the continuous futures chart of each stock
It can be stopped and re-run: stocks already downloaded are skipped.
"""
import argparse
import gzip
import json
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

from fyers_client import FyersClient, now_ist

OUT_SPOT = Path.home() / "trading-lab" / "data_nse"         # inside the shared repo
OUT_FUT = Path.home() / "trading-lab" / "data_nse_fut"     # --futures: continuous futures candles
CHUNK_DAYS = 95            # Fyers allows at most 100 days per intraday request


def fetch(client, symbol, start, end, tries=4):
    data = {"symbol": symbol, "resolution": "5", "date_format": "0",
            "range_from": str(int(start.timestamp())), "range_to": str(int(end.timestamp())), "cont_flag": "1"}
    for k in range(tries):
        client.limiter.wait()
        try:
            resp = client._check(client.fyers.history(data=data))
        except Exception as e:  # network hiccup
            resp = {"s": "error", "message": str(e)}
        if resp.get("s") == "ok":
            return resp.get("candles") or []
        msg = str(resp.get("message", ""))
        if "no_data" in msg.lower() or resp.get("s") == "no_data":
            return []
        time.sleep(3 * (k + 1))   # rate limit or temporary error: back off and retry
    print(f"   ! {symbol} {start:%Y-%m-%d}: {resp.get('message')}")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--only", help="comma list of underlyings to download (default: all F&O stocks)")
    ap.add_argument("--futures", action="store_true",
                    help="download the CONTINUOUS FUTURES chart (current contract + cont_flag) instead of spot")
    a = ap.parse_args()

    client = FyersClient()
    if not client.token_valid():
        raise SystemExit("Fyers token not valid - log in through the bot's Telegram (/login), then re-run.")
    client.limiter.gap = 1 / 2.0          # stay well under Fyers' limits
    OUT = OUT_FUT if a.futures else OUT_SPOT
    OUT.mkdir(exist_ok=True)
    today = now_ist().date()
    uni = client.load_universe(today)
    meta = {v["underlying"]: {"spot": s, "future": v["future"], "lot": v["lot"],
                              "tick": v["fut_tick"] if a.futures else v["tick"], "fut_tick": v["fut_tick"]}
            for s, v in uni.items()}
    (OUT / "universe.json").write_text(json.dumps(meta, indent=1))
    names = sorted(meta)
    if a.only:
        names = [n for n in names if n in {x.strip().upper() for x in a.only.split(",")}]
    print(f"{len(names)} stocks, {a.days} days of 5m candles each -> {OUT}")

    t_start = time.time()
    for n, und in enumerate(names, 1):
        path = OUT / f"{und}_5m.csv.gz"
        if path.exists():
            continue
        sym = meta[und]["future"] if a.futures else meta[und]["spot"]
        end = now_ist()
        stop_at = end - timedelta(days=a.days)
        rows, empty_streak, failed = [], 0, False
        while end > stop_at and empty_streak < 2:
            start = max(stop_at, end - timedelta(days=CHUNK_DAYS))
            c = fetch(client, sym, start, end)
            if c is None:
                failed = True
                break
            empty_streak = empty_streak + 1 if not c else 0
            rows += c
            end = start - timedelta(seconds=1)
        if failed or not rows:
            print(f"[{n}/{len(names)}] {und}: {'FAILED' if failed else 'no data'} (will retry on next run)")
            continue
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"]).drop_duplicates("time")
        df = df.sort_values("time")
        with gzip.open(path, "wt") as f:
            df.to_csv(f, index=False)
        el = time.time() - t_start
        print(f"[{n}/{len(names)}] {und}: {len(df):,} candles "
              f"{pd.to_datetime(df.time.iloc[0], unit='s'):%Y-%m-%d} -> {pd.to_datetime(df.time.iloc[-1], unit='s'):%Y-%m-%d} "
              f"({el / 60:.1f} min elapsed)")
    files = list(OUT.glob("*_5m.csv.gz"))
    print(f"\nDone: {len(files)} files, {sum(f.stat().st_size for f in files) / 1e6:.0f} MB in {OUT}")


if __name__ == "__main__":
    main()
