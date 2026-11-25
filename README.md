# Fyers NSE Futures Bot

Scans the **spot (cash) price** of every NSE stock that has futures and trades three strategies on the stock's **future**, with Telegram alerts. Starts in **paper mode**: live prices, simulated fills, no real orders.

**Spot vs future:** all candles, signals, the order-book filter, stop, target and breakeven use the **spot** price. Only entry and exit orders go to the future, at market. P&L is the future's actual P&L. Index futures are not traded.

## Strategy (as configured)

| | 2H RSI Short | 2H RSI Long | 1H Breakout (long only) |
|---|---|---|---|
| Setup | Last **closed 2H** candle RSI(14) > 70 | Last **closed 2H** candle RSI(14) < 30 | Last **3 closed 1H candles of today** within a 1% range (first possible at 12:15; previous day ignored) |
| Check | Every 5 min candle close | Every 15 min candle close | **Minute 12** of each 15 min candle (9:27, 9:42 …) |
| Trigger | Green candle A with low above 5 EMA, next candle B closes red → short at market | Green C1, then C2 closes above C1's open → long at market | Forming 15m candle is green and price is above the range high → long at market |
| Stop (spot) | 1 tick above A's high | 1 tick below C1's low | 1 tick below the range low |

**All three:**
- Stop is never further than a loss of **10% of the account** (measured on spot × quantity); if the candle/range stop is further, the 10% level is used
- Stop moves to entry at **1:1**; target **1:2** (intraday only)
- **Order-book filter (spot book):** before entering, longs need total bid quantity ≥ 55% of the book, shorts need ask quantity ≥ 55%. Otherwise the signal is skipped and logged
- One position at a time across all three strategies; whichever triggers first gets it
- `trades.csv` has a `strategy` column (`2H RSI SHORT`, `2H RSI LONG`, `1H BREAKOUT`), so one combined P&L can be filtered per strategy

- No leverage: buys as many lots of the future as the balance can margin (Fyers margin calculator); e.g. ₹90k/lot on ₹2.5L → 2 lots. `MAX_LOTS` in `.env` caps it (0 = no cap). Skips a contract if even 1 lot doesn't fit
- No new entries after 3:00 PM; everything squared off at **3:15 PM**
- New entries stop for the day once the day's loss reaches 50% of the opening balance
- Contract month: before the 22nd, the current month's future; from the 22nd, next month's
- Holidays: no list needed. After 9:16 AM the bot checks whether Fyers returns today's candles, and only scans if it does (rechecks every 5 minutes)
- Every arm, trigger, skip and trade is logged to `data/signals.csv` and `data/trades.csv`
- `data/candidates.csv` (since 25 Nov 2026): one row per setup that passed at a check, taken or not: book, check (`OPEN 5m 09:19:30`, `15m min 12`, `5m close`, `15m close`), taken, reason (ranked lower, book busy, outside move band, cooldown, entry check, margin, ...), rank, and the setup numbers (range, 09:15 / 15m open, LTP, first 5m move %, candle colour, gap %, distance beyond the range %, RSI reference / divergence candles and gap, candle stop, stop used, stop %, lots). No extra API calls: a busy 1H book still gets a row per break from the one price call (15m colour "not checked"); 2H RSI books are only checked while free
- 09:19:30 opening check: only breaks whose first 5m move (LTP vs the 09:15 open, in the break direction) is 1-2% (`OPEN_BO_MIN_MOVE_PCT` / `OPEN_BO_MAX_MOVE_PCT`) qualify, biggest move first. The open, high and low come from the same quote call as the LTP (at 09:19:30 they are the first 5m candle's)

## 1. Fyers app setup (one time)

1. Open the Fyers API dashboard → **Algo trading app**.
2. Set the **redirect URL** (e.g. `https://trade.fyers.in/api-login/redirect-uri/index.html`) and copy the App ID and Secret.
3. Add your Lightsail **static IP**. Paper mode only reads data, but registering it now means live mode works later.
4. Give the app Market Data permission (and Order Placement for live).

## 2. Install on Lightsail

```bash
# from your computer: copy the folder up
scp -r fyers_bot ubuntu@<STATIC_IP>:~

# on the server
cd ~/fyers_bot
sudo apt update && sudo apt install -y python3-venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env      # fill in keys and Telegram

venv/bin/python bot.py                  # first run in the foreground to check it works; Ctrl+C to stop
```

Run it permanently:

```bash
sudo cp fyers-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fyers-bot
journalctl -u fyers-bot -f              # live logs
```

## 3. Daily login (from your iPhone, about 30 seconds)

Fyers tokens expire every morning at 6:30 AM. At 8:45 AM the bot sends a login link on Telegram.
1. Tap the link and log in to Fyers.
2. You land on a page whose address contains `auth_code=...`. Copy the **full address**.
3. Paste it into the Telegram chat. The bot replies "Logged in" and starts.

Prompts are sent on weekdays only. For a special weekend session (e.g. muhurat trading), send `/login` yourself. If you skip a day, the bot just waits.

## Telegram commands

`/status` balance, position, armed count · `/armed` armed contracts and 1H ranges being watched · `/pause` / `/resume` new entries · `/exit` close position now · `/login` new login link

## Going live

Only after a few weeks of paper results you're happy with:
1. Make sure the Algo trading app is activated with your static IP and has order permission.
2. Set `PAPER_MODE=false` and `STARTING_CAPITAL` to your real funds in `.env`.
3. `sudo systemctl restart fyers-bot`

In live mode the bot buys/sells the future at market. Because stop and target are on **spot**, the bot itself watches spot (every 3 seconds) and exits the future at market when a level is hit; exchange orders can't watch spot.

**Safety net:** right after entry it also places a **backup SL-M on the future** at a 12% account-loss level, slightly wider than the 10% cap so it never fires before the bot's own stop. It only fills if the bot or server fails, and it's cancelled when the bot exits normally. If an exit order is rejected, you get a 🚨 alert and the bot retries every few seconds; the 3:15 square-off also retries until it succeeds.

## Notes

- Paper fills use the last traded price when a level is crossed, checked every 3 seconds, so real fills (especially stops) can be worse.
- `COST_PER_ROUND_TRIP` is a rough charges estimate for paper P&L. Compare it with a real contract note once you trade live.
- Every hour the bot fetches 1H spot candles for all ~200 F&O stocks (the 2H candles are built from these), which takes about 1.5 minutes because of API rate limits. A short watchlist (`UNIVERSE=RELIANCE,SBIN,...`) is much faster.
- To start the paper account over, stop the bot and delete `data/state.json`.
