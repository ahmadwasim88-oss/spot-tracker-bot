# Portfolio Tracker Bot

Python port of `Portfolio_Tracker.html`'s signal engine, running on GitHub Actions cron
(reusing the fvg-bot repo's infra) instead of needing the browser tab open.

## What's ported 1:1 from the HTML tool
- `calcEMASeries` / `calcRSISeries` / `calcReturnPct` / `calcAvgDailyMovePct` / `reconstructStreakDays`
- `computeTaFromCloses` (RSI-14, EMA-200, per-coin adaptive RSI/near-low/tier thresholds from
  CoinGecko daily closes). "Oversold" is the 10th percentile of a coin's own RSI history, clamped to
  20–35 (tightened from 15th / 20–45 — keep these two constants in step with `Portfolio_Tracker.html`).
- **True 90-day high/low**: hourly price history for an exact 90-day window, merged with CoinGecko
  OHLC wick candles that sit fully inside it, with wick dates refined to the matching hour.
  (Daily closes alone miss intraday wicks, and the raw 4-day OHLC candles can straddle the window
  edge — both were understating or misdating the 90-day low.) Falls back to daily closes if both
  wick sources fail.
- **365-day range and entry-depth gate**: ATH discount stays the coarse filter, but a coin also
  needs a real pullback from its 365-day high (volatility-scaled tiers, base -60 / -45 / -30%) to
  count as WAIT/ACCUMULATE; otherwise it's HOLD ("no meaningful recent pullback to buy"). The
  all-time high/low are folded into the 365-day range when they fall inside the year.
- `classifyHolding` — the full EXIT / AT RISK / WAIT / HOLD / TRIM / ACCUMULATE decision tree,
  including the DCA gap gate, relative-strength-vs-BTC conviction nudge, and target-proximity
  TRIM override.
- The once-daily TA refresh boundary (aligned to 00:00 UTC + 15 min, same as the HTML tool).
- **Data-freshness guard**: a daily price history that ends more than 3 days ago is discarded
  (logged in the Action output) instead of being used, and a cached entry with such a history is
  retried automatically (at most every 6 hours).

## What's different (automation-specific, not in the HTML tool)
- **Coin list**: hardcoded in `PORTFOLIO` at the top of `tracker_bot.py` (the HTML tool keeps
  this in browser localStorage). Edit that dict directly to add/remove coins or change targets.
- **Alerts**: sends a Telegram message the moment any coin's signal changes, plus one daily
  digest of all signals (sent once, in the 03:00–05:00 UTC / ~08:30–10:30 IST window, chosen to
  land after the daily TA refresh).
- **DCA log**: `state["dca_log"]` exists but nothing writes to it yet — there's no "Log DCA"
  button equivalent in a headless bot. The HTML tool's own DCA log lives in your browser's
  localStorage and is **not** visible to the bot, so a DCA you log in the app does not close the
  bot's gap gate. Until a price is logged here, the gap gate has nothing to compare against, so it
  stays open (fires ACCUMULATE every day conditions hold) — same as a fresh browser session with no
  DCA logged yet. The bot only reads each coin's `price` from its log entry, so the HTML tool's newer
  format (`{price, dateStr, timestamp, history: [...]}`, where `price` is the newest-dated entry)
  is compatible if you ever copy one across. If you want to log a DCA price from Telegram (e.g. a
  bot command) or via a `DCA_LOG` dict like `PORTFOLIO`, that needs to be added.

## Setup
1. Push this to your repo (replacing fvg-bot's contents, or in a new repo — your call).
   Keep `tracker_bot.py` and `state.json` in the repo root, and the workflow at
   `.github/workflows/tracker_bot.yml` (replace the existing file, don't add a second one).
2. In repo Settings → Secrets and variables → Actions, set:
   - `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (reuse from fvg-bot if same channel)
   - `COINGECKO_API_KEY` — **required in practice**: the script defaults it to an empty string
     (there is no built-in fallback key), so without it every CoinGecko call is sent unauthenticated
     and is likely to be rate-limited.
3. The workflow runs every 2 hours automatically, on every push to `main`, or manually via
   Actions → Spot Tracker Bot → Run workflow. Runs are queued one at a time
   (`concurrency`), and the bot's own `Update state [skip ci]` commit doesn't re-trigger it.

## After updating the bot
- The first run after a `TA_SCHEMA_VERSION` bump refetches every coin (3 CoinGecko calls per coin:
  daily history, 365-day OHLC, 90-day hourly), so it takes a minute or two instead of seconds.
  Later runs only refresh once per UTC day.
- Coins whose signal changes under new logic (e.g. WAIT → HOLD from the 365-day gate) send one
  "signal change" Telegram alert on that run. This is expected.
- The `TA_SCHEMA_VERSION` 4 → 5 bump (oversold rule tightened) triggers this full refetch once. Because the
  new rule is stricter, a coin may simply stop qualifying for ACCUMULATE; that shows up as a normal
  "signal change" alert if its signal moves.
- Don't hand-edit `state.json`; it's rewritten each run. Deleting it forces a clean rebuild but
  resends every signal as a change.

## Tuning
- `DCA_YEAR_EXTREME_PCT` / `DCA_YEAR_DEEP_PCT` / `DCA_YEAR_MODERATE_PCT` (and the floor/ceiling
  clamps) near the top of `tracker_bot.py` control the 365-day gate. They're scaled per coin by
  volatility, so calm coins (BTC, ETH) get shallower thresholds (~-18% "moderate").
- `TA_MAX_DATA_AGE_MS` sets how stale a daily history can be before it's rejected (default 3 days).

## Not yet ported (flag if you want these)
- Liquidity-dip-only microcap nuances that only ever showed up in the UI (colors, icons) —
  irrelevant to a text alert.
- The ⚡ "live low" / "live ATH" flags, the 365-day High/Low cells and the "Daily data through"
  line — all UI-only in the HTML tool, no Telegram equivalent needed.
