# Portfolio Tracker Bot

Python port of `Portfolio_Tracker.html`'s signal engine, running on GitHub Actions cron and alerting via Telegram.
It is kept 1:1 with the HTML tool: same Binance data, same decision tree, same thresholds. Verified by running the
HTML's own JavaScript and this bot on 10,000 synthetic coins: signal, strength and reason text were identical in every case.

## Data (same as the HTML tool)
- **Binance spot USDT pairs.** Daily klines (365d closes, exact daily highs/lows, 30-day median volume), 24h ticker
  (price, 24h range, volume, best bid/ask), weekly klines for ATH/ATL. `data-api.binance.vision` is tried first
  because GitHub's US runners get HTTP 451 from `api.binance.com`.
- **INR price = USDT price x effective USDT/INR rate.** The rate is the exchange reading in the Crypto-tools
  `index.json` (as the HTML auto-syncs), falling back to bank USD/INR x last multiplier, then the last stored rate.
- `state.json` caches the **USDT** series; INR is derived on every run, so a rate change never leaves stale figures.

## Signal logic (matches the HTML)
EXIT / AT RISK from liquidity (24h volume vs own 30-day median, thin volume, order-book spread/dead book), ATH + 365d
tiers (WAIT/HOLD), ACCUMULATE (near RSI-qualified low + live AND last-closed RSI oversold + DCA gap gate), SWING TRIM
(near high + extreme RSI + >=40% above EMA-200 or >=50% in 30d), target TRIM. One 90-day volatility window drives
tiers, near-low band and the DCA gap. Discover scans the top 100 Binance pairs by 24h volume daily.

## Differences (automation-specific)
- Coin list is `PORTFOLIO` at the top of `tracker_bot.py`, keyed by Binance symbol (e.g. `BTCUSDT`). `XNOUSDT` is
  listed; if Binance has no such spot pair it shows NA, same as the HTML tool.
- Spread checks use one observation per run (the HTML counts one REST poll as a confirmed streak too).
- Alerts: Telegram on every signal change plus a daily digest (03:00-05:00 UTC).
- DCA log: the HTML's log lives in your browser and is invisible to the bot. `state["dca_log"]` is keyed by Binance
  symbol (`{"price": INR, "dateStr": ..., "history": [...]}`); until a price is written there the gap gate stays open.
- Not ported: USDT-peg warning, today's-candle display (UI only).

## Setup
1. Replace the repo's `tracker_bot.py`, `state.json`, and `.github/workflows/tracker_bot.yml` with these.
2. Secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`. **`COINGECKO_API_KEY` is no longer used.**
3. Runs every 2 hours, on push to `main`, or manually (Actions -> Spot Tracker Bot).

## First run after this update
State from the CoinGecko version is discarded automatically. The first run sends ONE message ("now runs on Binance
data - current signals") instead of a change alert per coin, and runs discovery once. Later runs refetch TA once per
UTC day only. Don't hand-edit `state.json`.
