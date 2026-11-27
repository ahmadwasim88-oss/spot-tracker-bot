import requests
import json
import os
import time
import math
import html
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Market data is Binance spot (USDT pairs), exactly as in Portfolio_Tracker.html. data-api.binance.vision is
# listed first because GitHub-hosted runners are US-based and api.binance.com answers those with HTTP 451.
BINANCE_HOSTS = [
    "https://data-api.binance.vision",
    "https://api-gcp.binance.com",
    "https://api.binance.com",
]
# INR price = Binance USDT price x effective USDT/INR rate. The rate is the exchange USDT/INR published by the
# Crypto-tools JSON (what the HTML auto-syncs to); the bank USD/INR rate is only the anchor/fallback.
USDT_INR_JSON_URL = "https://usdt-inr-proxy.waseem-ah-zaki.workers.dev/usdt-inr.json"
BANK_FX_SOURCES = [
    ("https://api.frankfurter.dev/v2/rate/usd/inr", lambda j: j.get("rate")),
    ("https://open.er-api.com/v6/latest/USD", lambda j: (j.get("rates") or {}).get("INR")),
]
FX_PLAUSIBLE_RATIO = (0.85, 1.5)  # exchange USDT/INR vs bank USD/INR; outside this the reading is ignored

STATE_FILE = "state.json"
STATE_SOURCE = "binance"  # bump when stored data/keys change so old state is reset instead of misread

# Keys are Binance USDT symbols (the HTML tool's coin ids). Edit this dict to add/remove coins or change targets.
PORTFOLIO = {
    "EGLDUSDT":  {"symbol": "EGLD",  "target": 5016.65},
    "KAVAUSDT":  {"symbol": "KAVA",  "target": 60.59},
    "AXSUSDT":   {"symbol": "AXS",   "target": 1295.67},
    "DOTUSDT":   {"symbol": "DOT",   "target": 1379.78},
    "TFUELUSDT": {"symbol": "TFUEL", "target": 8.95},
    "POLUSDT":   {"symbol": "POL",   "target": 98.5},
    "GRTUSDT":   {"symbol": "GRT",   "target": 12.11},
    "AVAXUSDT":  {"symbol": "AVAX",  "target": 6612.02},
    "1INCHUSDT": {"symbol": "1INCH", "target": 64.6},
    "ICPUSDT":   {"symbol": "ICP",   "target": 1765.6},
    "XNOUSDT":   {"symbol": "XNO",   "target": 247.4},
    "ETHUSDT":   {"symbol": "ETH",   "target": 431946},
    "BTCUSDT":   {"symbol": "BTC",   "target": 11187013},
}
MACRO_ID = "BTCUSDT"  # used for relative-strength-vs-BTC; always fetched even if not held

# ── Constants (kept in step with Portfolio_Tracker.html) ──────────
DCA_EXTREME_PCT   = -85
DCA_DEEP_PCT      = -70
DCA_MODERATE_PCT  = -40
DCA_WATCH_PCT     = -20
DCA_DIP_WINDOW_DAYS = 90
DCA_DIP_NEAR_PCT    = 3  # fallback only, used before a coin has enough TA history
DCA_VOL_LOOKBACK_DAYS = 90  # ONE volatility baseline shared by ATH/year tiers, near-low/high band and the DCA gap
DCA_STEP_CHAIN_MAX_DAYS = 180
DCA_GAP_TIERS = [
    {"floor": 15, "ceil": 20, "mult": 5.5},  # Buy #2 (after 1st DCA): 15%–20%
    {"floor": 22, "ceil": 27, "mult": 7.5},  # Buy #3 (after 2nd DCA): 22%–27%
    {"floor": 28, "ceil": 35, "mult": 9.5},  # Buy #4+ (after 3rd+ DCA): 28%–35%
]
REL_STRENGTH_WINDOW_DAYS = 14
REL_STRENGTH_SCALE     = 3
REL_STRENGTH_MIN_BONUS = -10
REL_STRENGTH_MAX_BONUS = 8
EMA_TREND_FAST_SHORT = 20
EMA_TREND_FAST_LONG  = 50
EMA_TREND_LONG = 200
RSI_PERIOD     = 14
RSI_OVERSOLD   = 30  # fallback only
RSI_OVERBOUGHT = 85  # fallback only
TARGET_PROXIMITY_PCT = 3

# Liquidity (all from Binance): 24h volume vs the coin's own 30-day median, absolute thinness, order-book spread
VOLUME_COLLAPSE_RATIO = 0.05   # 24h volume under 5% of its own 30-day median -> EXIT
VOLUME_FADING_RATIO   = 0.15   # under 15% -> AT RISK
VOLUME_CEIL_USDT      = 1_000_000  # at/above this much 24h volume a drop vs the median is a cool-off, never EXIT/AT RISK
THIN_VOLUME_USDT      = 200_000    # under this many USDT per 24h: thin -> AT RISK
VOL_MEDIAN_DAYS       = 30
ABSOLUTE_DEAD_VOLUME  = 100_000    # INR
SPREAD_RISK_PCT       = 1.5
SPREAD_EXIT_PCT       = 5.0
SPREAD_MIN_TICKS      = 2

TA_HISTORY_DAYS = 365
TA_ALIGN_BUFFER_S = 15  # TA is treated stale once per UTC day, 15 s after 00:00 UTC (Binance daily candles close exactly then)
TA_MAX_DATA_AGE_MS = 3 * 24 * 3600 * 1000
HEAVY_CALL_GAP_S = 0.07  # same pacing as the HTML tool (Binance klines weight=2, 6000/min limit)

RSI_OVERSOLD_PERCENTILE = 10
RSI_OVERSOLD_MIN_SAMPLES = 60
RSI_OVERSOLD_FLOOR = 20
RSI_OVERSOLD_CEIL = 35
RSI_OVERBOUGHT_PERCENTILE = 97
RSI_OVERBOUGHT_MIN_SAMPLES = 60
RSI_OVERBOUGHT_FLOOR = 78
RSI_OVERBOUGHT_CEIL = 90
SWING_TRIM_EMA_STRETCH_PCT = 40
SWING_TRIM_SURGE_30D_PCT = 50

VOL_SCALE_REF_PCT = 3
VOL_SCALE_MIN = 0.6
VOL_SCALE_MAX = 1.8
ATH_TIER_ABS_FLOOR_PCT = -95
ATH_TIER_ABS_CEIL_PCT = -12
REF_LOW_RSI_MARGIN = 0
DCA_NEAR_LOW_MULT = 1.0
DCA_NEAR_LOW_FLOOR_PCT = 1.5
DCA_NEAR_LOW_CEIL_PCT = 8
EXTREME_TOL = 5e-4

YEAR_HIGH_DAYS = 365
DCA_YEAR_EXTREME_PCT  = -60
DCA_YEAR_DEEP_PCT     = -45
DCA_YEAR_MODERATE_PCT = -30
YEAR_TIER_ABS_FLOOR_PCT = -80
YEAR_TIER_ABS_CEIL_PCT  = -10

ATH_TTL_S = 7 * 24 * 3600   # Binance weekly-history ATH/ATL is re-read at most weekly...
ATH_REFRESH_TOL = 5e-4      # ...or sooner if a fresh 365-day daily extreme beats the stored one

DISCOVER_TOP_N = 200            # top N Binance USDT pairs by 24h volume
DISCOVER_MAX_TA_FETCHES = 100
DISCOVERY_REPORT_SIGNALS = {"ACCUMULATE"}
STABLECOIN_BASES = {
    "USDC", "FDUSD", "TUSD", "USDE", "USDP", "DAI", "BUSD", "USDS", "USD1", "PYUSD", "RLUSD", "XUSD", "USDD",
    "FRAX", "EURI", "EUR", "AEUR", "GBP", "TRY", "BRL", "JPY", "AUD", "ARS", "MXN", "PLN", "RON", "UAH", "ZAR",
    "CZK", "COP",
}

TA_SCHEMA_VERSION = 8  # 8: Binance data layer (USDT series cached, INR derived each run), one 90d volatility window

_LAST_HTTP_ERR = None

# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_MAX_CHARS = 4000  # Telegram rejects a message over 4096 characters outright

def _telegram_chunks(msg, limit=TELEGRAM_MAX_CHARS):
    """Split on line breaks so no chunk exceeds the limit (an oversize alert would otherwise be dropped whole)."""
    chunks, cur = [], ""
    for line in msg.split("\n"):
        while len(line) > limit:  # a single over-long line: hard split
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]

def send_telegram(msg):
    """True when the message was delivered (or Telegram is not configured, so state is never frozen on a dry run);
    False when delivery failed, so the caller can retry on the next run instead of losing the alert."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing; skipping message.")
        return True
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    ok = True
    for chunk in _telegram_chunks(msg):
        try:
            r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"}, timeout=10)
            if not r.ok:
                print(f"Telegram error: {r.text}")
                ok = False
        except Exception as e:
            # requests errors embed the request URL, which contains the bot token: never print it
            print(f"Telegram failed: {type(e).__name__}")
            ok = False
    return ok

# ── State ─────────────────────────────────────────────────────────
class StateCorrupt(Exception):
    pass

def load_state():
    """Missing file -> fresh state. A file that exists but can't be parsed (e.g. a typo from hand-editing dca_log)
    raises StateCorrupt: silently starting fresh would erase the DCA log and overwrite the file on the next save."""
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"signals": {}, "ta_cache": {}, "ath_cache": {}, "last_daily": "", "dca_log": {}}
    except Exception as e:
        raise StateCorrupt(f"{type(e).__name__}: {e}")
    if not isinstance(state, dict):
        raise StateCorrupt("top level is not an object")
    return state

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

def migrate_state(state):
    """State written by the CoinGecko version is keyed by CoinGecko ids and holds INR series; neither is
    reusable. Drop it (dca_log, last_daily kept) and report True so main() doesn't fire a change alert per coin."""
    if state.get("dataSource") == STATE_SOURCE:
        return False
    for k in ("signals", "ta_cache", "ath_cache", "discovery_candidates"):
        state[k] = {}
    state.pop("last_discovery", None)
    state["dataSource"] = STATE_SOURCE
    return True

# ── Formatting ────────────────────────────────────────────────────
SIGNAL_EMOJI = {
    "ACCUMULATE": "\U0001F7E2", "WAIT": "\u26AA", "HOLD": "\U0001F535",
    "TRIM": "\U0001F7E1", "SWING TRIM": "\U0001F7E3", "EXIT": "\U0001F534",
    "AT RISK": "\U0001F7E0", "NA": "\u26AA",
}

def fmt_inr(n):
    if n is None or not math.isfinite(n):
        return "\u2014"
    a = abs(n)
    dec = 2 if a >= 100 else (4 if a >= 1 else (6 if a >= 0.01 else 8))
    return f"\u20b9{n:,.{dec}f}"

def f2(v):
    return "\u2014" if v is None else f"{v:.2f}"

def days_since(date_str):
    """Whole UTC calendar days since a YYYY-MM-DD (or ISO) date, same as the HTML tool's daysSince."""
    if not date_str:
        return None
    try:
        then = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(0, round((today - then).total_seconds() / 86400))

def days_since_ts(ts_ms):
    if ts_ms is None:
        return None
    return days_since(datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))

def _num(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None

# ── DCA gap gate (1:1 with Portfolio_Tracker.html) ────────────────
def _dca_entry_time_ms(entry):
    if isinstance(entry, dict) and entry.get("dateStr"):
        try:
            dt = datetime.fromisoformat(str(entry["dateStr"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp() * 1000
        except Exception:
            pass
    ts = entry.get("timestamp") if isinstance(entry, dict) else None
    try:
        val = float(ts)
        return val if math.isfinite(val) else None
    except (TypeError, ValueError):
        return None

def dca_history_of(dca_log, coin_id):
    e = (dca_log or {}).get(coin_id)
    if not isinstance(e, dict):
        return []
    hist = e.get("history")
    if isinstance(hist, list) and hist:
        return list(hist)
    try:
        p = float(e.get("price"))
        if math.isfinite(p) and p > 0:
            return [{"price": p, "dateStr": e.get("dateStr"), "timestamp": e.get("timestamp")}]
    except (TypeError, ValueError):
        pass
    return []

def count_active_dca_steps(history):
    if not isinstance(history, list) or not history:
        return 0
    valid = []
    for h in history:
        if not isinstance(h, dict):
            continue
        try:
            p = float(h.get("price"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(p) and p > 0:
            valid.append(h)
    if not valid:
        return 0
    sorted_hist = sorted(
        valid,
        key=lambda h: (str(h.get("dateStr") or ""), _num(h.get("timestamp")) or 0),
        reverse=True,
    )
    steps = 1
    for i in range(1, len(sorted_hist)):
        if steps >= len(DCA_GAP_TIERS):
            break
        prev, cur = sorted_hist[i - 1], sorted_hist[i]
        t_prev, t_cur = _dca_entry_time_ms(prev), _dca_entry_time_ms(cur)
        days_between = (t_prev - t_cur) / 86400000 if (t_prev is not None and t_cur is not None) else 0
        if days_between <= DCA_STEP_CHAIN_MAX_DAYS and float(cur["price"]) >= float(prev["price"]):
            steps += 1
        else:
            break
    return steps

def get_dca_gap_status(current_price, last_log, gap_avg_daily_move, history=None):
    has_last_dca = last_log is not None
    step_count = max(1, count_active_dca_steps(history)) if has_last_dca else 0
    tier = DCA_GAP_TIERS[min(max(step_count, 1), len(DCA_GAP_TIERS)) - 1]
    try:
        move = float(gap_avg_daily_move)
    except (TypeError, ValueError, OverflowError):
        move = None
    if move is not None and math.isfinite(move) and move > 0:
        required_gap_pct = max(tier["floor"], min(tier["ceil"], move * tier["mult"]))
    else:
        required_gap_pct = tier["floor"]

    try:
        last_dca_price = float(last_log.get("price")) if has_last_dca and isinstance(last_log, dict) else None
    except (TypeError, ValueError, OverflowError):
        last_dca_price = None
    valid_reference = last_dca_price is not None and math.isfinite(last_dca_price) and last_dca_price > 0

    try:
        current = float(current_price)
    except (TypeError, ValueError, OverflowError):
        current = None
    valid_current = current is not None and math.isfinite(current) and current > 0

    gap_pct = (
        (last_dca_price - current) / last_dca_price * 100
        if has_last_dca and valid_reference and valid_current
        else None
    )
    cleared = not has_last_dca or (gap_pct is not None and gap_pct >= required_gap_pct)
    next_dca_price = last_dca_price * (1 - required_gap_pct / 100) if valid_reference else None

    return {
        "requiredGapPct": required_gap_pct,
        "gapPct": gap_pct,
        "cleared": cleared,
        "stepCount": step_count,
        "nextDcaPrice": next_dca_price,
        "lastDcaPrice": last_dca_price if valid_reference else None,
    }

# ── Indicators ────────────────────────────────────────────────────
def calc_ema_series(values, period):
    if not values or len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [None] * len(values)
    ema = sum(values[:period]) / period
    out[period - 1] = ema
    for i in range(period, len(values)):
        ema = values[i] * k + ema * (1 - k)
        out[i] = ema
    return out

def calc_rsi_series(values, period):
    if not values or len(values) < period + 1:
        return []
    out = [None] * len(values)
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-diff, 0)) / period
        out[i] = 100 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out

def calc_return_pct(closes, days):
    if not closes or len(closes) < days + 1:
        return None
    start, end = closes[-(days + 1)], closes[-1]
    if start is None or end is None or start == 0:
        return None
    return (end - start) / start * 100

def calc_avg_daily_move_pct(closes, days):
    if not closes or len(closes) < days + 1:
        return None
    sl = closes[-(days + 1):]
    s, n = 0.0, 0
    for i in range(1, len(sl)):
        prev, cur = sl[i - 1], sl[i]
        if prev:
            s += abs((cur - prev) / prev * 100)
            n += 1
    return s / n if n else None

def percentile_of(sorted_asc, pct):
    if not sorted_asc:
        return None
    return sorted_asc[int((pct / 100) * (len(sorted_asc) - 1))]

def compute_ta_from_closes(closes):
    """Per-coin indicators from daily INR closes. Rolling extremes here are close-based; apply_range_extremes
    replaces them with the exact daily highs/lows."""
    if not closes or len(closes) < RSI_PERIOD + 1:
        return None
    ema_long_series = calc_ema_series(closes, EMA_TREND_LONG)
    rsi_series = calc_rsi_series(closes, RSI_PERIOD)
    dip_window = closes[-DCA_DIP_WINDOW_DAYS:]
    rolling_low, rolling_high = min(dip_window), max(dip_window)

    valid_rsi = sorted(v for v in rsi_series if v is not None)
    rsi_oversold_threshold = RSI_OVERSOLD
    if len(valid_rsi) >= RSI_OVERSOLD_MIN_SAMPLES:
        p = percentile_of(valid_rsi, RSI_OVERSOLD_PERCENTILE)
        if p is not None:
            rsi_oversold_threshold = max(RSI_OVERSOLD_FLOOR, min(RSI_OVERSOLD_CEIL, p))
    rsi_overbought_threshold = RSI_OVERBOUGHT
    if len(valid_rsi) >= RSI_OVERBOUGHT_MIN_SAMPLES:
        p_high = percentile_of(valid_rsi, RSI_OVERBOUGHT_PERCENTILE)
        if p_high is not None:
            rsi_overbought_threshold = max(RSI_OVERBOUGHT_FLOOR, min(RSI_OVERBOUGHT_CEIL, p_high))

    avg_daily_move = calc_avg_daily_move_pct(closes, DCA_VOL_LOOKBACK_DAYS)
    vol_scale = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, avg_daily_move / VOL_SCALE_REF_PCT)) if avg_daily_move is not None else 1
    near_low_threshold_pct = max(
        DCA_NEAR_LOW_FLOOR_PCT,
        min(DCA_NEAR_LOW_CEIL_PCT, (avg_daily_move if avg_daily_move is not None else DCA_NEAR_LOW_FLOOR_PCT) * DCA_NEAR_LOW_MULT),
    )

    def clamp_tier(base):
        return max(ATH_TIER_ABS_FLOOR_PCT, min(ATH_TIER_ABS_CEIL_PCT, base * vol_scale))

    def clamp_year_tier(base):
        return max(YEAR_TIER_ABS_FLOOR_PCT, min(YEAR_TIER_ABS_CEIL_PCT, base * vol_scale))

    return {
        "rsi": rsi_series[-1] if rsi_series else None,
        "rsiOversoldThreshold": rsi_oversold_threshold,
        "rsiOverboughtThreshold": rsi_overbought_threshold,
        "avgDailyMove": avg_daily_move,
        "nearLowThresholdPct": near_low_threshold_pct,
        "athTiers": {
            "extreme": clamp_tier(DCA_EXTREME_PCT), "deep": clamp_tier(DCA_DEEP_PCT),
            "moderate": clamp_tier(DCA_MODERATE_PCT), "watch": clamp_tier(DCA_WATCH_PCT),
        },
        "yearTiers": {
            "extreme": clamp_year_tier(DCA_YEAR_EXTREME_PCT), "deep": clamp_year_tier(DCA_YEAR_DEEP_PCT),
            "moderate": clamp_year_tier(DCA_YEAR_MODERATE_PCT),
        },
        "emaLong": ema_long_series[-1] if ema_long_series else None,
        "rollingLow": rolling_low,
        "rollingLowDaysAgo": len(dip_window) - dip_window.index(rolling_low),
        "rollingHigh": rolling_high,
        "rollingHighDaysAgo": len(dip_window) - dip_window.index(rolling_high),
        "rollingLowWindowDays": len(dip_window),
        "closes": closes,
    }

def apply_range_extremes(ta, closes, highs, lows, vol_med_usd):
    """Exact 90d / 180d / 365d extremes from the daily candle highs and lows (same as the HTML tool)."""
    ta["volMedUsd"] = vol_med_usd

    def ext_at(arr, n, is_high):
        s = arr[-n:]
        v = max(s) if is_high else min(s)
        return v, s[::-1].index(v) + 1  # days ago of the LAST occurrence

    if len(highs) == len(closes) and len(lows) == len(closes):
        def win(n):
            (h, hd), (l, ld) = ext_at(highs, n, True), ext_at(lows, n, False)
            return {"high": h, "highDaysAgo": hd, "low": l, "lowDaysAgo": ld}
        w90 = win(DCA_DIP_WINDOW_DAYS)
        ta.update(rollingHigh=w90["high"], rollingHighDaysAgo=w90["highDaysAgo"],
                  rollingLow=w90["low"], rollingLowDaysAgo=w90["lowDaysAgo"])
        ta["win180Ext"], ta["year"] = win(180), win(YEAR_HIGH_DAYS)
        return ta
    w = closes[-YEAR_HIGH_DAYS:]
    y_hi, y_lo = max(w), min(w)
    ta["year"] = {"high": y_hi, "highDaysAgo": len(w) - w.index(y_hi), "low": y_lo, "lowDaysAgo": len(w) - w.index(y_lo)}
    return ta

def audit_ta(ta):
    if not ta:
        return True
    ath_tiers, year_tiers = ta.get("athTiers") or {}, ta.get("yearTiers") or {}
    fields = [
        ta.get("rsi"), ta.get("emaLong"), ta.get("rollingLow"), ta.get("rollingHigh"),
        ta.get("rsiOversoldThreshold"), ta.get("rsiOverboughtThreshold"),
        ta.get("nearLowThresholdPct"), ta.get("avgDailyMove"),
        ath_tiers.get("extreme"), ath_tiers.get("deep"), ath_tiers.get("moderate"), ath_tiers.get("watch"),
        year_tiers.get("extreme"), year_tiers.get("deep"), year_tiers.get("moderate"),
    ]
    return all(v is None or math.isfinite(v) for v in fields)

def build_ta(series, rate):
    """ONE function turning a cached USDT daily series into the INR indicator set used by every signal. The cache
    holds USDT values; converting at the current effective rate on each run means a changed USDT/INR rate can
    never leave stale-rate INR figures behind (the HTML tool rescales its caches for the same reason)."""
    if not series or not (rate and rate > 0):
        return None
    closes = [c * rate for c in series["closesUsd"]]
    ta = compute_ta_from_closes(closes)
    if not ta or not audit_ta(ta):
        return None
    ta["closesEndTs"] = series.get("closesEndTs")
    apply_range_extremes(ta, closes, [h * rate for h in series["highsUsd"]], [l * rate for l in series["lowsUsd"]],
                         series.get("volMedUsd"))
    return ta

# ── Data fetch: FX ────────────────────────────────────────────────
def _fetch_json(url, timeout=7):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Fetch failed {url}: {e}")
        return None

def resolve_fx(state):
    """(effective USDT/INR rate, source). Exchange reading (Crypto-tools JSON) when reachable and plausible vs the
    bank rate; otherwise bank rate x the last good multiplier; otherwise the last stored rate."""
    fx = state.setdefault("fx", {})
    bank = None
    for url, pick in BANK_FX_SOURCES:
        j = _fetch_json(url, 5)
        v = _num(pick(j)) if isinstance(j, dict) else None
        if v and v > 0:
            bank = v
            break
    j = _fetch_json(f"{USDT_INR_JSON_URL}?_={int(time.time())}")
    reading = _num(j.get("usdtInr")) if isinstance(j, dict) else None
    ref_bank = bank or fx.get("bank")
    if reading and reading > 0 and ref_bank and FX_PLAUSIBLE_RATIO[0] <= reading / ref_bank <= FX_PLAUSIBLE_RATIO[1]:
        rate, source, mult = reading, "exchange", reading / ref_bank
    elif bank:
        rate, source, mult = bank * (fx.get("multiplier") or 1), "bank", fx.get("multiplier")
    elif fx.get("rate"):
        return fx["rate"], "stored"
    else:
        return None, None
    # The stored figures are only a fallback for a run where both FX feeds are down, so they are rewritten on a
    # meaningful move only (otherwise state.json would be committed on every run as the rate ticks).
    if mult and ref_bank and (not fx.get("rate") or abs(rate / fx["rate"] - 1) >= 0.005):
        fx.update(rate=round(rate, 2), bank=round(ref_bank, 2), multiplier=round(mult, 4))
    return rate, source

# ── Data fetch: Binance ───────────────────────────────────────────
_active_host = BINANCE_HOSTS[0]
_last_call_at = 0.0

def binance_get(path, params=None, timeout=10):
    """GET on Binance market data, trying each host in turn. A rate-limit/ban (429/418) never fails over: the IP
    limit is shared. HTTP 400 (bad symbol) is the same on every host, so it stops there too."""
    global _LAST_HTTP_ERR, _active_host, _last_call_at
    _LAST_HTTP_ERR = None
    gap = HEAVY_CALL_GAP_S - (time.time() - _last_call_at)
    if gap > 0:
        time.sleep(gap)
    _last_call_at = time.time()
    for host in [_active_host] + [h for h in BINANCE_HOSTS if h != _active_host]:
        try:
            r = requests.get(host + path, params=params, timeout=timeout)
        except Exception as e:
            _LAST_HTTP_ERR = str(e)
            continue
        if r.status_code in (429, 418, 400):
            _LAST_HTTP_ERR = f"HTTP_ERROR:{r.status_code}"
            return None
        if not r.ok:
            _LAST_HTTP_ERR = f"HTTP_ERROR:{r.status_code}"
            continue
        try:
            data = r.json()
        except ValueError:
            _LAST_HTTP_ERR = "bad JSON"
            continue
        _active_host = host
        return data
    print(f"Binance fetch failed {path}: {_LAST_HTTP_ERR}")
    return None

def fetch_exchange_info():
    """{symbol: {"base", "tick"}} for every TRADING USDT spot pair (tick size feeds the spread-in-ticks check)."""
    data = binance_get("/api/v3/exchangeInfo", {"permissions": "SPOT"}, 20)
    if not isinstance(data, dict):
        return None
    out = {}
    for s in data.get("symbols") or []:
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT" or not s.get("isSpotTradingAllowed"):
            continue
        pf = next((f for f in s.get("filters") or [] if f.get("filterType") == "PRICE_FILTER"), None)
        tick = _num(pf.get("tickSize")) if pf else None
        out[s["symbol"]] = {"base": s.get("baseAsset"), "tick": tick if tick and tick > 0 else None}
    return out

def fetch_tickers(symbols=None):
    """24hr tickers (price, 24h range, quote volume, best bid/ask). symbols=None -> every pair."""
    global _LAST_HTTP_ERR
    if symbols is None:
        data = binance_get("/api/v3/ticker/24hr", None, 20)
        return data if isinstance(data, list) else None
    out, failed = [], False
    for i in range(0, len(symbols), 90):
        chunk = symbols[i:i + 90]
        data = binance_get("/api/v3/ticker/24hr", {"symbols": json.dumps(chunk, separators=(",", ":"))})
        if data is None and _LAST_HTTP_ERR == "HTTP_ERROR:400" and len(chunk) > 1:
            # one invalid/delisted symbol makes Binance reject the whole batch: isolate per symbol
            data = []
            for sym in chunk:
                one = binance_get("/api/v3/ticker/24hr", {"symbol": sym})
                if isinstance(one, dict) and one.get("symbol"):
                    data.append(one)
                elif _LAST_HTTP_ERR in ("HTTP_ERROR:429", "HTTP_ERROR:418"):
                    return None
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            failed = True
            continue
        out.extend(data)
    return None if (failed and not out) else out

def normalize_ticker(t, rate, tick_size=None):
    """One normaliser for portfolio coins AND Discover candidates (as in the HTML tool). INR = USDT x rate."""
    def inr(k):
        v = _num(t.get(k))
        return v * rate if v is not None else None
    md = {
        "symbol": t.get("symbol"),
        "current_price": inr("lastPrice"), "low_24h": inr("lowPrice"), "high_24h": inr("highPrice"),
        "total_volume": inr("quoteVolume"), "change_24h": _num(t.get("priceChangePercent")),
        "spreadPct": None, "spreadTicks": None, "deadBook": False,
        "ath": None, "ath_change_percentage": None, "ath_date": None, "atl": None, "atl_date": None,
        "notFound": False,
    }
    bid, ask = _num(t.get("bidPrice")), _num(t.get("askPrice"))
    if bid is not None and ask is not None:
        if not (bid > 0) or not (ask > 0) or ask < bid:
            md["deadBook"] = True
        else:
            mid = (bid + ask) / 2
            md["spreadPct"] = (ask - bid) / mid * 100 if mid > 0 else None
            md["spreadTicks"] = math.floor((ask - bid) / tick_size + 0.5) if tick_size else None
    return md

def fetch_binance_ath_atl(symbol):
    """All-time high/low in USDT from Binance's weekly history, refined to the exact day (same as the HTML tool)."""
    wk = binance_get("/api/v3/klines", {"symbol": symbol, "interval": "1w", "limit": 1000})
    if not isinstance(wk, list) or not wk:
        return None
    max_k = min_k = wk[0]
    for k in wk:
        if float(k[2]) > float(max_k[2]):
            max_k = k
        if float(k[3]) < float(min_k[3]):
            min_k = k
    exact_high, exact_low = max_k, min_k
    d_high = binance_get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "startTime": max_k[0], "endTime": max_k[6], "limit": 10})
    d_low = binance_get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "startTime": min_k[0], "endTime": min_k[6], "limit": 10})
    for k in d_high if isinstance(d_high, list) else []:
        if float(k[2]) >= float(exact_high[2]):
            exact_high = k
    for k in d_low if isinstance(d_low, list) else []:
        if float(k[3]) <= float(exact_low[3]):
            exact_low = k
    day = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return {
        "binAthUsd": float(exact_high[2]), "binAthDate": day(exact_high[0]),
        "binAtlUsd": float(exact_low[3]), "binAtlDate": day(exact_low[0]),
        "binListDate": day(wk[0][0]), "fetchedAt": time.time() * 1000,
    }

def ensure_ath_atl(ath_cache, symbol, series):
    """Fetch/refresh one coin's ATH/ATL entry in `ath_cache` when missing, older than a week, or beaten by a fresh
    365-day daily extreme in `series`. Used for the portfolio (persisted cache) and Discover (throwaway dict)."""
    e = ath_cache.get(symbol)
    stale = (not e or not e.get("fetchedAt") or time.time() * 1000 - e["fetchedAt"] > ATH_TTL_S * 1000)
    if not stale and series:
        y_hi, y_lo = max(series["highsUsd"][-YEAR_HIGH_DAYS:]), min(series["lowsUsd"][-YEAR_HIGH_DAYS:])
        stale = (y_lo < e["binAtlUsd"] * (1 - ATH_REFRESH_TOL)) or (y_hi > e["binAthUsd"] * (1 + ATH_REFRESH_TOL))
    if stale:
        fresh = fetch_binance_ath_atl(symbol)
        if fresh:
            ath_cache[symbol] = fresh
    return ath_cache.get(symbol)

def resolve_ath_atl(entry, cur, rate):
    """ATH/ATL in INR at the current effective rate, widened by the live price (a new extreme is dated today)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    ath = ath_date = atl = atl_date = None
    if entry and (entry.get("binAthUsd") or 0) > 0:
        ath, ath_date = entry["binAthUsd"] * rate, entry["binAthDate"]
    if ath is not None and cur is not None and cur > ath:
        ath, ath_date = cur, now_iso
    if entry and (entry.get("binAtlUsd") or 0) > 0:
        atl, atl_date = entry["binAtlUsd"] * rate, entry["binAtlDate"]
    if atl is not None and cur is not None and cur > 0 and cur < atl:
        atl, atl_date = cur, now_iso
    return {
        "ath": ath, "ath_date": ath_date, "atl": atl, "atl_date": atl_date,
        "ath_change_percentage": (cur - ath) / ath * 100 if (ath and ath > 0 and cur is not None) else None,
    }

def fetch_ta_history(symbol):
    """Daily USDT series from Binance 1d klines (today's unfinished candle dropped). None on failure."""
    global _LAST_HTTP_ERR
    ks = binance_get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": TA_HISTORY_DAYS + 1})
    if not isinstance(ks, list) or not ks:
        _LAST_HTTP_ERR = _LAST_HTTP_ERR or "empty history"
        return None
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_is_today = datetime.fromtimestamp(ks[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d") == today
    usable = ks[:-1] if last_is_today else ks
    if not usable:
        _LAST_HTTP_ERR = "empty history"
        return None
    vols = sorted(v for v in (_num(k[7]) for k in usable[-VOL_MEDIAN_DAYS:]) if v is not None and v >= 0)
    r10 = lambda v: float(f"{float(v):.10g}")  # 10 significant digits keeps state.json small
    return {
        "closesUsd": [r10(k[4]) for k in usable],
        "highsUsd": [r10(k[2]) for k in usable],
        "lowsUsd": [r10(k[3]) for k in usable],
        "volMedUsd": vols[len(vols) // 2] if len(vols) >= 10 else None,  # coin's own 30-day median daily volume
        "closesEndTs": usable[-1][0],
    }

# ── Signal engine (1:1 with Portfolio_Tracker.html) ───────────────
def year_high_drawdown(closes, cur, high_24h, low_24h, ath_value, ath_date, atl_value, atl_date, stored):
    """Trailing-365d high/low (daily closes widened by the stored candle extremes, the all-time extremes when they
    fall inside the year, and the live price) and the current drawdown from that high."""
    if not closes or len(closes) < YEAR_HIGH_DAYS - 20 or cur is None:
        return None
    w = closes[-YEAR_HIGH_DAYS:]
    high, low = max(w), min(w)
    high_days, low_days = len(w) - w.index(high), len(w) - w.index(low)
    if stored and stored["high"] > high:
        high, high_days = stored["high"], stored["highDaysAgo"]
    if stored and stored["low"] < low:
        low, low_days = stored["low"], stored["lowDaysAgo"]
    live_high = max(cur, high_24h) if high_24h is not None else cur
    live_low = min(cur, low_24h) if low_24h is not None else cur
    ds_atl = days_since(atl_date) if (atl_value is not None and atl_date) else None
    if ds_atl is not None and ds_atl <= YEAR_HIGH_DAYS and atl_value <= low * 1.005 and (atl_value <= low or abs(ds_atl - low_days) <= 5):
        low, low_days = min(low, atl_value), ds_atl
    ds_ath = days_since(ath_date) if (ath_value is not None and ath_date) else None
    if ds_ath is not None and ds_ath <= YEAR_HIGH_DAYS and ath_value >= high * 0.995 and (ath_value >= high or abs(ds_ath - high_days) <= 5):
        high, high_days = max(high, ath_value), ds_ath
    if live_high > high:
        high, high_days = live_high, 0
    if live_low < low:
        low, low_days = live_low, 0
    if not high or high <= 0:
        return None
    return {"high": high, "low": low, "highDaysAgo": high_days, "lowDaysAgo": low_days, "pct": (cur - high) / high * 100}

def rsi_at_extreme(series, days_ago, is_live, stale_gap_days=0):
    """RSI on the day an extreme was set. `series` is aligned with closes + [live], so index len-1-k is k days ago.
    If the daily candles trail yesterday (stale_gap_days > 0) a past RSI is unavailable rather than read off the
    wrong day."""
    if not series or days_ago is None:
        return None
    n = len(series)
    if is_live or days_ago <= 0:
        return series[n - 1]
    if stale_gap_days > 0:
        return None
    idx = n - 1 - days_ago
    if idx < 0:
        return None
    v = series[idx]
    return None if v is None or v != v else v

def pick_ref_window(cands, thr, is_low):
    """Shortest window whose extreme was set at a qualifying RSI; else the window closest to qualifying
    (never loosens the threshold); else the first candidate."""
    lst = [c for c in cands if c]
    valid = [c for c in lst if c.get("rsi") is not None and c["rsi"] == c["rsi"]]
    hit = next((c for c in valid if (c["rsi"] <= thr if is_low else c["rsi"] >= thr)), None)
    if hit:
        return hit
    best = None
    for c in valid:
        if best is None or (c["rsi"] < best["rsi"] if is_low else c["rsi"] > best["rsi"]):
            best = c
    return best or (lst[0] if lst else None)

def classify_holding(coin_id, md, item, ta, macro_ta, dca_log, rate):
    na = {"signal": "NA", "strength": 0, "cmp": None}
    if md is None:
        return {**na, "reason": "No market data yet for this coin."}
    if md.get("notFound"):
        return {**na, "reason": "Not found in the latest Binance market data — it may have been delisted, or the symbol no longer matches. Verify manually."}

    volume, cur = md.get("total_volume"), md.get("current_price")
    low_24h, high_24h = md.get("low_24h"), md.get("high_24h")
    target = item.get("target")

    def fold(rec, is_low):
        """All-time extreme widened by the live price AND the 24h range (a real traded price)."""
        if rec is None:
            return None, False
        c = [x for x in (cur, low_24h if is_low else high_24h) if x is not None and math.isfinite(x)]
        v = min([rec] + c) if is_low else max([rec] + c)
        return v, v != rec

    ath_value, is_live_ath = fold(md.get("ath"), False)
    atl_value, is_live_atl = fold(md.get("atl"), True)
    ath_pct = (cur - ath_value) / ath_value * 100 if (ath_value is not None and ath_value > 0 and cur is not None) else md.get("ath_change_percentage")
    ath_date, atl_date = md.get("ath_date"), md.get("atl_date")

    closes_all = ta.get("closes") if ta else None
    own_ret = calc_return_pct(closes_all, REL_STRENGTH_WINDOW_DAYS)
    btc_ret = calc_return_pct(macro_ta.get("closes") if macro_ta else None, REL_STRENGTH_WINDOW_DAYS)
    same_candle_clock = bool(ta and macro_ta and ta.get("closesEndTs") is not None and ta["closesEndTs"] == macro_ta.get("closesEndTs"))
    rel_strength = (own_ret - btc_ret) if (coin_id != MACRO_ID and own_ret is not None and btc_ret is not None and same_candle_clock) else None

    # RSI / EMAs are recomputed with the live price appended, so they share a clock with the near-low check
    live_closes = (closes_all + [cur]) if (closes_all and cur is not None) else closes_all
    live_rsi_series = calc_rsi_series(live_closes, RSI_PERIOD) if live_closes else []
    live_rsi = live_rsi_series[-1] if live_rsi_series else (ta.get("rsi") if ta else None)
    live_ema_long_series = calc_ema_series(live_closes, EMA_TREND_LONG) if live_closes else []
    live_ema_long = live_ema_long_series[-1] if live_ema_long_series else (ta.get("emaLong") if ta else None)
    ema_fs = calc_ema_series(live_closes, EMA_TREND_FAST_SHORT) if live_closes else []
    ema_fl = calc_ema_series(live_closes, EMA_TREND_FAST_LONG) if live_closes else []
    sentiment = None
    if ema_fs and ema_fl:
        sentiment = "Bullish" if ema_fs[-1] > ema_fl[-1] else ("Bearish" if ema_fs[-1] < ema_fl[-1] else "Mixed")

    # Liquidity, all from Binance: 24h volume vs own 30-day median, absolute thinness, order-book spread
    vol_med_inr = ta["volMedUsd"] * rate if (ta and (ta.get("volMedUsd") or 0) > 0 and rate and rate > 0) else None
    no_trading_data = volume is None
    vol_rel = volume / vol_med_inr if (not no_trading_data and vol_med_inr and vol_med_inr > 0) else None
    thin_volume = (not no_trading_data) and rate > 0 and volume < THIN_VOLUME_USDT * rate
    negligible_volume = (not no_trading_data) and volume < ABSOLUTE_DEAD_VOLUME
    vol_below_ceil = (not no_trading_data) and (not rate > 0 or volume < VOLUME_CEIL_USDT * rate)
    base_dead = no_trading_data or volume == 0 or negligible_volume or (vol_rel is not None and vol_rel < VOLUME_COLLAPSE_RATIO and vol_below_ceil)
    base_at_risk = (not base_dead) and (thin_volume or (vol_rel is not None and vol_rel < VOLUME_FADING_RATIO and vol_below_ceil))
    spread_pct, spread_ticks = md.get("spreadPct"), md.get("spreadTicks")
    spread_tick_ok = spread_ticks is None or spread_ticks > SPREAD_MIN_TICKS
    # One REST observation counts as a confirmed streak in the HTML tool (SPREAD_FLICKER_STREAK step), so no streak state is kept here
    spread_dead = md.get("deadBook") is True or (spread_pct is not None and spread_pct >= SPREAD_EXIT_PCT and spread_tick_ok)
    spread_at_risk = (not spread_dead) and spread_pct is not None and spread_pct >= SPREAD_RISK_PCT and spread_tick_ok
    dead_liquidity = base_dead or spread_dead
    at_risk = (not dead_liquidity) and (base_at_risk or spread_at_risk)
    spread_txt_core = f"{spread_pct:.2f}%{'' if spread_ticks is None else f', {spread_ticks} ticks'}" if spread_pct is not None else ""

    depth = abs(ath_pct) if ath_pct is not None else None
    ath_tiers = (ta or {}).get("athTiers") or {"extreme": DCA_EXTREME_PCT, "deep": DCA_DEEP_PCT, "moderate": DCA_MODERATE_PCT, "watch": DCA_WATCH_PCT}
    year_tiers = (ta or {}).get("yearTiers") or {"extreme": DCA_YEAR_EXTREME_PCT, "deep": DCA_YEAR_DEEP_PCT, "moderate": DCA_YEAR_MODERATE_PCT}
    yr = year_high_drawdown(closes_all, cur, high_24h, low_24h, ath_value, ath_date, atl_value, atl_date, (ta or {}).get("year"))
    yr_note = f", {abs(yr['pct']):.2f}% below {YEAR_HIGH_DAYS}d high" if yr else ""

    if dead_liquidity:
        signal = "EXIT"
        strength = 90 if no_trading_data else 85 if spread_dead else max(60, min(95, math.floor(100 - ((vol_rel or 0) / VOLUME_COLLAPSE_RATIO) * 40 + 0.5)))
        if not base_dead and spread_dead:
            reason = ("Binance orderbook has no active bid or ask — trading may be halted or delisted. Verify manually."
                      if md.get("deadBook") else
                      f"Binance orderbook spread ({spread_txt_core}) is severely blown out — extreme slippage/liquidity exit risk.")
        elif no_trading_data:
            reason = "No volume data available — trading has likely stopped. Verify manually."
        elif negligible_volume:
            reason = f"Volume ({fmt_inr(volume)}) is negligible. No real market to sell into."
        else:
            reason = f"24h volume is only {vol_rel * 100:.2f}% of its own 30-day median — trading has dried up, there may be no real market left here."
    elif at_risk:
        signal, strength = "AT RISK", (55 if (spread_at_risk and not base_at_risk) else 50)
        micro = ((f"Thin Binance volume ({fmt_inr(volume)} per 24h)" if thin_volume else f"Volume fading to {vol_rel * 100:.2f}% of its 30-day median") + " — liquidity risk") if base_at_risk else ""
        spread = f"Wide Binance orderbook spread ({spread_txt_core}) — thin liquidity / high slippage risk" if spread_at_risk else ""
        reason = f"{'. '.join(x for x in (micro, spread) if x)}. Not a place to add."
    elif ath_pct is not None:
        if ath_pct <= ath_tiers["moderate"] and yr is not None and yr["pct"] > year_tiers["moderate"]:
            signal, strength = "HOLD", 35
            reason = (f"{depth:.2f}% below ATH, but only {abs(yr['pct']):.2f}% below its {YEAR_HIGH_DAYS}d high "
                      f"(needs ≥{abs(year_tiers['moderate']):.2f}% for this coin) — no meaningful recent pullback to buy.")
        elif ath_pct <= ath_tiers["extreme"] and (yr is None or yr["pct"] <= year_tiers["extreme"]):
            signal, strength, reason = "WAIT", 30, f"{depth:.2f}% below ATH{yr_note} — extreme, but depth alone isn't a floor. Watching for a better entry."
        elif ath_pct <= ath_tiers["deep"] and (yr is None or yr["pct"] <= year_tiers["deep"]):
            signal, strength, reason = "WAIT", 25, f"{depth:.2f}% below ATH{yr_note} — deep discount, but that alone doesn't confirm the fall has stopped."
        elif ath_pct <= ath_tiers["moderate"]:
            signal, strength, reason = "WAIT", 20, f"{depth:.2f}% below ATH{yr_note} — meaningful discount, but not yet a confirmed add."
        else:
            signal, strength = "HOLD", 35
            reason = (f"Only {depth:.2f}% below ATH — not a deep enough discount yet."
                      if ath_pct <= ath_tiers["watch"]
                      else f"Within {depth:.2f}% of ATH — holding for your target unless an extreme blow-off triggers.")
    else:
        signal, strength, reason = "HOLD", 25, "No ATH data yet to judge discount."

    # ── 90-day extremes, widened by the live price and the 24h range ──
    window90 = (ta or {}).get("rollingLowWindowDays")
    atl_within_window = bool(atl_date and window90 is not None and days_since(atl_date) is not None and days_since(atl_date) <= window90)
    ath_within_window = bool(ath_date and window90 is not None and days_since(ath_date) is not None and days_since(ath_date) <= window90)
    effective_low = effective_high = None
    if ta and ta.get("rollingLow") is not None and cur is not None:
        effective_low = min([ta["rollingLow"], cur] + ([low_24h] if low_24h is not None else []) + ([atl_value] if (atl_within_window and atl_value is not None) else []))
    if ta and ta.get("rollingHigh") is not None and cur is not None:
        effective_high = max([ta["rollingHigh"], cur] + ([high_24h] if high_24h is not None else []) + ([ath_value] if (ath_within_window and ath_value is not None) else []))
    is_live_low = (effective_low is not None and (effective_low == cur or effective_low == low_24h) and (ta.get("rollingLow") is None or effective_low < ta["rollingLow"]))
    is_live_high = (effective_high is not None and (effective_high == cur or effective_high == high_24h) and (ta.get("rollingHigh") is None or effective_high > ta["rollingHigh"]))
    low_day_k = 0 if is_live_low else (days_since(atl_date) if (atl_within_window and atl_value is not None and effective_low == atl_value) else (ta or {}).get("rollingLowDaysAgo"))
    high_day_k = 0 if is_live_high else (days_since(ath_date) if (ath_within_window and ath_value is not None and effective_high == ath_value) else (ta or {}).get("rollingHighDaysAgo"))

    ends_ts = (ta or {}).get("closesEndTs")
    stale_gap = max(0, (days_since_ts(ends_ts) or 0) - 1) if ends_ts else 0
    low_rsi = rsi_at_extreme(live_rsi_series, low_day_k, is_live_low, stale_gap)
    high_rsi = rsi_at_extreme(live_rsi_series, high_day_k, is_live_high, stale_gap)
    live_rsi_now = rsi_at_extreme(live_rsi_series, 0, True)

    def rsi_of(c):
        if c.get("live"):
            return live_rsi_now
        k = c.get("k")
        if k is None or not closes_all or k > len(closes_all):
            return None
        return rsi_at_extreme(live_rsi_series, k, False, stale_gap)

    def pick_extreme(cands, is_low):
        """A longer window only replaces a shorter one if genuinely more extreme (beyond EXTREME_TOL)."""
        best = None
        for c in cands:
            if not c or c.get("v") is None or c["v"] != c["v"]:
                continue
            if best is None or (c["v"] < best["v"] * (1 - EXTREME_TOL) if is_low else c["v"] > best["v"] * (1 + EXTREME_TOL)):
                best = c
        if best is None:
            return None
        out = dict(best)
        if "rsi" not in out:
            out["rsi"] = rsi_of(best)
        return out

    eff_low_c = {"v": effective_low, "k": low_day_k, "live": is_live_low, "rsi": low_rsi} if effective_low is not None else None
    eff_high_c = {"v": effective_high, "k": high_day_k, "live": is_live_high, "rsi": high_rsi} if effective_high is not None else None
    atl_k, ath_k = (days_since(atl_date) if atl_date else None), (days_since(ath_date) if ath_date else None)
    atl_c = {"v": atl_value, "k": 0 if is_live_atl else atl_k, "live": is_live_atl} if (atl_value is not None and atl_k is not None) else None
    ath_c = {"v": ath_value, "k": 0 if is_live_ath else ath_k, "live": is_live_ath} if (ath_value is not None and ath_k is not None) else None

    def build_win(n_days):
        if not closes_all or cur is None:
            return None
        w = closes_all[-n_days:]
        c_lo, c_hi = min(w), max(w)
        yr_low = {"v": yr["low"], "k": yr["lowDaysAgo"], "live": yr["lowDaysAgo"] == 0} if (yr and yr.get("lowDaysAgo") is not None and yr["lowDaysAgo"] <= n_days) else None
        yr_high = {"v": yr["high"], "k": yr["highDaysAgo"], "live": yr["highDaysAgo"] == 0} if (yr and yr.get("highDaysAgo") is not None and yr["highDaysAgo"] <= n_days) else None
        win_ext = (ta or {}).get("win180Ext") if n_days == 180 else ((ta or {}).get("year") if n_days == YEAR_HIGH_DAYS else None)
        ext_low = {"v": win_ext["low"], "k": win_ext["lowDaysAgo"], "live": False} if win_ext else None
        ext_high = {"v": win_ext["high"], "k": win_ext["highDaysAgo"], "live": False} if win_ext else None
        return {
            "n": len(w),
            "short": len(closes_all) < n_days - 5,  # history shorter than this window: only covers the pair's life since listing
            "lo": pick_extreme([eff_low_c, ext_low, yr_low, atl_c if (atl_k is not None and atl_k <= n_days) else None,
                                {"v": c_lo, "k": len(w) - w.index(c_lo), "live": False}], True),
            "hi": pick_extreme([eff_high_c, ext_high, yr_high, ath_c if (ath_k is not None and ath_k <= n_days) else None,
                                {"v": c_hi, "k": len(w) - w.index(c_hi), "live": False}], False),
        }

    win180, win365 = build_win(180), build_win(YEAR_HIGH_DAYS)

    # Gate references: shortest window whose extreme was set at an RSI meeting THIS coin's own threshold
    w90 = window90 if window90 is not None else DCA_DIP_WINDOW_DAYS
    os_thr = (ta or {}).get("rsiOversoldThreshold", RSI_OVERSOLD)
    ob_thr = (ta or {}).get("rsiOverboughtThreshold", RSI_OVERBOUGHT)
    low_cands, high_cands = [], []
    if eff_low_c:
        low_cands.append({"w": w90, "v": eff_low_c["v"], "rsi": eff_low_c["rsi"]})
    if eff_high_c:
        high_cands.append({"w": w90, "v": eff_high_c["v"], "rsi": eff_high_c["rsi"]})
    for win, n_days, use in ((win180, 180, bool(win180 and win180["n"] > w90 + 5)), (win365, YEAR_HIGH_DAYS, bool(win365 and not win365["short"]))):
        if use:
            if win["lo"]:
                low_cands.append({"w": n_days, "v": win["lo"]["v"], "rsi": win["lo"]["rsi"]})
            if win["hi"]:
                high_cands.append({"w": n_days, "v": win["hi"]["v"], "rsi": win["hi"]["rsi"]})
    q_low = pick_ref_window(low_cands, os_thr + REF_LOW_RSI_MARGIN, True)
    q_high = pick_ref_window(high_cands, ob_thr, False)
    gate_window = q_low["w"] if q_low else w90
    gate_low = q_low["v"] if q_low else effective_low
    gate_high_window = q_high["w"] if q_high else w90
    gate_high = q_high["v"] if q_high else effective_high

    # ── Add pipeline (only for coins at WAIT) ──
    add_reason_shown = False
    dca_gap_pct = dca_required_gap_pct = next_dca_price = None
    if signal == "WAIT":
        rel_bonus, rel_note = 0, ""
        if rel_strength is not None:
            rel_bonus = max(REL_STRENGTH_MIN_BONUS, min(REL_STRENGTH_MAX_BONUS, rel_strength / REL_STRENGTH_SCALE))
            rel_note = f" {'Outperforming' if rel_strength > 0 else 'Underperforming'} BTC ({rel_strength:+.2f}pp over {REL_STRENGTH_WINDOW_DAYS}d) — {'capital may already be rotating in.' if rel_strength > 0 else 'weight this accordingly.'}"

        if ta and ta.get("rollingLow") is not None and gate_low and gate_low > 0 and cur is not None:
            add_reason_shown = True
            above_low = (cur - gate_low) / gate_low * 100
            near_low_pct = ta.get("nearLowThresholdPct", DCA_DIP_NEAR_PCT)
            near_low = above_low <= near_low_pct
            rsi_threshold = ta.get("rsiOversoldThreshold", RSI_OVERSOLD)
            # live AND last-closed RSI must both be oversold: an intraday spike down alone isn't enough to add
            rsi_live_oversold = live_rsi is not None and live_rsi <= rsi_threshold
            rsi_closed_oversold = ta.get("rsi") is not None and ta["rsi"] <= rsi_threshold
            rsi_oversold = rsi_live_oversold and rsi_closed_oversold
            if near_low and rsi_oversold:
                last_log = (dca_log or {}).get(coin_id)
                gap = get_dca_gap_status(cur, last_log, ta.get("avgDailyMove"), dca_history_of(dca_log, coin_id))
                dca_gap_pct, dca_required_gap_pct, next_dca_price = gap["gapPct"], gap["requiredGapPct"], gap["nextDcaPrice"]
                if gap["cleared"]:
                    signal, strength = "ACCUMULATE", math.floor(strength + 30 + rel_bonus + 0.5)
                    gap_note = "" if (last_log is None or gap["gapPct"] is None) else f" Also {gap['gapPct']:.2f}% below last DCA ({fmt_inr(gap['lastDcaPrice'])}) — a real step down."
                    reason = f"{depth:.2f}% below ATH{yr_note}, {above_low:.2f}% above its {gate_window}d low (within this coin's own ≤{near_low_pct:.2f}% band), RSI {live_rsi:.2f} (oversold vs this coin's own ≤{rsi_threshold:.2f} bottom zone) — conditions met.{gap_note}{rel_note}"
                else:
                    g = gap["gapPct"]
                    last_txt = fmt_inr(gap["lastDcaPrice"])
                    if g is None:
                        pos = "its previous DCA price is invalid"
                    else:
                        pos = f"only {g:.2f}% below last DCA ({last_txt})" if g >= 0 else f"{abs(g):.2f}% above last DCA ({last_txt}), not below it"
                    step = f" (DCA #{gap['stepCount'] + 1})" if gap["stepCount"] > 1 else ""
                    reason = f"Still meets add conditions, but {pos}. Needs ≥{gap['requiredGapPct']:.2f}% below that{step} — next DCA near {fmt_inr(gap['nextDcaPrice'])}. Held at WAIT.{rel_note}"
            elif near_low and rsi_live_oversold and not rsi_closed_oversold:
                reason = f"{depth:.2f}% below ATH{yr_note}, near its {gate_window}d low, live RSI {live_rsi:.2f} is oversold (≤{rsi_threshold:.2f}), but the last daily close (RSI {f2(ta.get('rsi'))}) hasn't confirmed it yet — waiting for a daily close."
            elif near_low and not rsi_oversold:
                reason = f"{depth:.2f}% below ATH{yr_note}, near its {gate_window}d low, but RSI ({f2(live_rsi)}) hasn't reached this coin's own oversold zone (≤{rsi_threshold:.2f}) yet."
            else:
                reason = f"{depth:.2f}% below ATH{yr_note}, but {abs(above_low):.2f}% {'above' if above_low >= 0 else 'below'} its {gate_window}d low — outside this coin's own ≤{near_low_pct:.2f}% near-low band."
        else:
            reason = "Not enough price history loaded yet to judge proximity to its recent-range low."

    # ── Swing trim: which question applies (add or trim) is decided first from the plain 90d range ──
    d_to_low = (cur - effective_low) / cur if (effective_low is not None and cur) else None
    d_to_high = (effective_high - cur) / cur if (effective_high is not None and cur) else None
    lean = "swing" if (d_to_low is not None and d_to_high is not None and d_to_high < d_to_low) else "add"
    swing_high, swing_window = (gate_high if gate_high is not None else effective_high), gate_high_window
    ema_stretch_pct = (cur - live_ema_long) / live_ema_long * 100 if (live_ema_long is not None and live_ema_long > 0 and cur is not None) else None
    surge_30d_pct = calc_return_pct(live_closes, 30)
    if swing_high is not None and cur is not None and signal not in ("EXIT", "AT RISK", "ACCUMULATE", "TRIM"):
        below_high_pct = (swing_high - cur) / swing_high * 100
        near_high_pct = (ta or {}).get("nearLowThresholdPct", DCA_DIP_NEAR_PCT)
        near_high = below_high_pct <= near_high_pct
        ob = (ta or {}).get("rsiOverboughtThreshold", RSI_OVERBOUGHT)
        closed_rsi = (ta or {}).get("rsi")
        rsi_live_ob = live_rsi is not None and live_rsi >= ob
        rsi_closed_ob = closed_rsi is not None and closed_rsi >= ob
        ema_stretched = ema_stretch_pct is not None and ema_stretch_pct >= SWING_TRIM_EMA_STRETCH_PCT
        surge_stretched = surge_30d_pct is not None and surge_30d_pct >= SWING_TRIM_SURGE_30D_PCT
        parabolic = ema_stretched or surge_stretched
        if near_high and rsi_live_ob and rsi_closed_ob and parabolic:
            signal, strength = "SWING TRIM", max(strength, 65)
            bits = " & ".join(b for b in (f"+{ema_stretch_pct:.2f}% above EMA-200" if ema_stretched else None, f"+{surge_30d_pct:.2f}% in 30d" if surge_stretched else None) if b)
            target_note = f" Still well short of your {fmt_inr(target)} target, so this is a partial-profit/rebuy-lower play, not an exit." if target else ""
            reason = f"{below_high_pct:.2f}% below its {swing_window}d high (≤{near_high_pct:.2f}% band), live & closed RSI extreme ({live_rsi:.2f} / {closed_rsi:.2f} ≥{ob:.2f}), and stretched ({bits}) — extreme spike with room to buy back lower.{target_note}"
        elif near_high and rsi_live_ob and not rsi_closed_ob:
            reason = f"{below_high_pct:.2f}% below its {swing_window}d high and live RSI {live_rsi:.2f} is overbought (≥{ob:.2f}), but the last daily close (RSI {f2(closed_rsi)}) hasn't confirmed it yet — waiting for a daily close."
        elif near_high and rsi_live_ob and rsi_closed_ob and not parabolic:
            reason = f"{below_high_pct:.2f}% below its {swing_window}d high with extreme RSI ({live_rsi:.2f}), but not parabolic enough yet (needs ≥+{SWING_TRIM_EMA_STRETCH_PCT:.2f}% above EMA-200 or ≥+{SWING_TRIM_SURGE_30D_PCT:.2f}% in 30d) — holding to avoid trimming early."
        elif lean == "swing" and ((add_reason_shown and signal == "WAIT") or (ath_pct is not None and ath_pct > ath_tiers["watch"])):
            lead = f"{below_high_pct:.1f}% below its {swing_window}d high"
            reason = (f"{lead} (within this coin's own ≤{near_high_pct:.2f}% band), but RSI ({f2(live_rsi)}) hasn't reached this coin's own overbought zone (≥{ob:.2f}) yet."
                      if near_high else
                      f"{lead} — outside this coin's own ≤{near_high_pct:.2f}% near-high band, and RSI ({f2(live_rsi)}) is below this coin's own overbought zone (≥{ob:.2f}).")
        elif near_high:
            reason = f"{reason} Also {below_high_pct:.2f}% below its {swing_window}d high, but RSI ({f2(live_rsi)}) hasn't reached this coin's own overbought zone (≥{ob:.2f}) yet."

    if target and cur is not None and cur >= target * (1 - TARGET_PROXIMITY_PCT / 100) and signal not in ("EXIT", "AT RISK"):
        signal = "TRIM"
        strength = 80 if cur >= target else 70
        reason = f"Price is at or within {TARGET_PROXIMITY_PCT:.2f}% of your target ({fmt_inr(target)}) — worth considering a trim regardless of what the underlying discount signal says."

    return {
        "signal": signal, "strength": strength, "reason": reason, "cmp": cur,
        "athPct": ath_pct, "rsi": live_rsi, "sentiment": sentiment, "relStrengthVsBtc": rel_strength,
        "dcaGapPct": dca_gap_pct, "dcaRequiredGapPct": dca_required_gap_pct, "nextDcaPrice": next_dca_price,
        "rsiOversoldThreshold": os_thr, "rsiOverboughtThreshold": ob_thr,
        "nearLowThresholdPct": (ta or {}).get("nearLowThresholdPct", DCA_DIP_NEAR_PCT),
        "lean": lean, "gateLow": gate_low, "gateLowWindow": gate_window,
        "gateHigh": gate_high, "gateHighWindow": gate_high_window,
        "high365": yr["high"] if yr else None, "drawdown365": yr["pct"] if yr else None,
        "volRel": vol_rel, "spreadPct": spread_pct,
    }

# ── TA refresh (once/day, boundary-aligned like taBoundaryPassedSince) ──
def ta_boundary_passed_since(ts_ms):
    if not ts_ms:
        return True
    now = datetime.now(timezone.utc)
    boundary = now.replace(hour=0, minute=0, second=TA_ALIGN_BUFFER_S, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return ts_ms / 1000 < boundary.timestamp()

def fetch_series_with_retry(symbol):
    series = fetch_ta_history(symbol)
    if not series and _LAST_HTTP_ERR in ("HTTP_ERROR:429", "HTTP_ERROR:418"):
        print(f"Rate limited on {symbol} — pausing 15s...")
        time.sleep(15)
        series = fetch_ta_history(symbol)
    return series

def refresh_ta(state, symbols, rate):
    """Refetch each coin's daily USDT series when a UTC day boundary has passed since its last fetch."""
    ta_cache = state.setdefault("ta_cache", {})
    now_ms = time.time() * 1000

    def needs_refresh(s):
        c = ta_cache.get(s)
        if c is None or c.get("schemaVersion") != TA_SCHEMA_VERSION or ta_boundary_passed_since(c.get("timestamp")):
            return True
        end = c.get("closesEndTs")
        return bool(end and now_ms - end > TA_MAX_DATA_AGE_MS and now_ms - (c.get("timestamp") or 0) > 6 * 3600 * 1000)

    updated = failed = stale_feeds = audit_failures = 0
    for s in [s for s in symbols if needs_refresh(s)]:
        series = fetch_series_with_retry(s)
        if not series:
            failed += 1
            print(f"TA fetch failed for {s}: {_LAST_HTTP_ERR}")
            continue
        end = series["closesEndTs"]
        if end is None or now_ms - end > TA_MAX_DATA_AGE_MS:
            stale_feeds += 1
            print(f"Daily history for {s} ends {datetime.fromtimestamp(end / 1000, tz=timezone.utc):%Y-%m-%d} — too old, discarded.")
            continue
        if build_ta(series, rate) is None:
            audit_failures += 1
            print(f"TA audit/history check failed for {s} — keeping previous values.")
            continue
        ta_cache[s] = {**series, "timestamp": now_ms, "schemaVersion": TA_SCHEMA_VERSION}
        updated += 1
    if failed or stale_feeds or audit_failures:
        print(f"TA refresh: {updated} ok, {failed} failed, {stale_feeds} stale feeds, {audit_failures} audit failures.")
    return updated

# ── Message builders ──────────────────────────────────────────────
def digest_message(results, header="Portfolio Signal Digest"):
    lines = [f"\U0001F4CA <b>{header}</b>\n"]
    for cid, r in results.items():
        emoji = SIGNAL_EMOJI.get(r["signal"], "\u26AA")
        lines.append(f"{emoji} <b>{PORTFOLIO[cid]['symbol']}</b> {fmt_inr(r['cmp'])} — {r['signal']} ({r['strength']}%)")
    return "\n".join(lines)

def change_message(cid, prev, new):
    emoji = SIGNAL_EMOJI.get(new["signal"], "\u26AA")
    return (
        f"{emoji} <b>{PORTFOLIO[cid]['symbol']} \u2192 {new['signal']}</b> ({new['strength']}%)\n\n"
        f"Was: {prev or 'N/A'}\n"
        f"CMP: {fmt_inr(new['cmp'])}\n\n"
        f"{html.escape(new['reason'])}"
    )

def candidate_discovery_message(candidates):
    lines = [f"\U0001F50E <b>New Add Candidates (Top {DISCOVER_TOP_N} by Binance volume)</b>", "Outside your portfolio · matching ACCUMULATE\n"]
    for coin in candidates:
        result = coin["result"]
        lines.append(
            f"{SIGNAL_EMOJI.get(result['signal'], chr(0x26AA))} <b>{html.escape(coin['symbol'])}</b> · #{coin['rank']}\n"
            f"{fmt_inr(result['cmp'])} — <b>ADD {result['strength']}%</b>\n"
            f"{html.escape(result['reason'])}\n"
        )
    return "\n".join(lines)

# ── Candidate discovery (1:1 with Portfolio_Tracker.html) ─────────
def is_stablecoin_pair(base, t):
    if (base or "").upper() in STABLECOIN_BASES:
        return True
    px, hi, lo = _num(t.get("lastPrice")), _num(t.get("highPrice")), _num(t.get("lowPrice"))
    return bool(px and 0.97 <= px <= 1.03 and lo and lo > 0 and hi is not None and hi >= lo and (hi - lo) / px < 0.01)

def run_candidate_discovery(state, macro_ta, rate, info):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_discovery") == today:
        return False
    print(f"Starting daily top-{DISCOVER_TOP_N} Binance candidate discovery...")
    tickers = fetch_tickers(None)
    if tickers is None or not info:
        print("Candidate discovery skipped; Binance ticker/exchange-info fetch failed.")
        return False

    # Stage 1: top N USDT pairs by 24h volume, stablecoins out, then the cheap prefilter
    matched = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in info or not ((_num(t.get("lastPrice")) or 0) > 0):
            continue
        base = info[sym]["base"]
        if is_stablecoin_pair(base, t):
            continue
        md = normalize_ticker(t, rate, info[sym]["tick"])
        if md["total_volume"] and md["total_volume"] > 0:
            matched.append({"id": sym, "symbol": base, "md": md})
    matched.sort(key=lambda c: -c["md"]["total_volume"])
    for i, c in enumerate(matched):
        c["rank"] = i + 1

    def above_24h_low(md):
        lo, cur = md.get("low_24h"), md.get("current_price")
        return (cur - lo) / lo * 100 if (lo and cur and lo > 0 and cur > 0) else None

    queue = []
    for c in matched[:DISCOVER_TOP_N]:
        md = c["md"]
        if c["id"] in PORTFOLIO:
            continue
        a = above_24h_low(md)
        if a is not None and a > DCA_NEAR_LOW_CEIL_PCT:
            continue
        v = md["total_volume"]
        if v is None or v < ABSOLUTE_DEAD_VOLUME or v < THIN_VOLUME_USDT * rate:
            continue
        queue.append((a if a is not None else float("inf"), md.get("change_24h") or 0, c))
    queue.sort(key=lambda row: (row[0], row[1]))
    queue = [row[2] for row in queue[:DISCOVER_MAX_TA_FETCHES]]
    seen = state.setdefault("discovery_candidates", {})
    newly_qualified = []

    # Stage 2: daily history per shortlisted coin; ATH only for coins whose last closed RSI is already oversold
    ath_scratch = {}  # throwaway: non-held coins aren't persisted in state.json
    now_ms = time.time() * 1000
    for c in queue:
        series = fetch_series_with_retry(c["id"])
        if not series or series["closesEndTs"] is None or now_ms - series["closesEndTs"] > TA_MAX_DATA_AGE_MS:
            continue
        ta = build_ta(series, rate)
        if not ta or ta.get("rollingLow") is None:
            continue
        md, item = c["md"], {"symbol": c["symbol"], "target": None}
        if ta["rsi"] is not None and ta["rsi"] <= ta.get("rsiOversoldThreshold", RSI_OVERSOLD):
            md.update(resolve_ath_atl(ensure_ath_atl(ath_scratch, c["id"], series), md["current_price"], rate))
        sig = classify_holding(c["id"], md, item, ta, macro_ta, None, rate)
        if sig.get("signal") in DISCOVERY_REPORT_SIGNALS:
            if sig["signal"] != (seen.get(c["id"]) or {}).get("signal"):
                newly_qualified.append({"id": c["id"], "symbol": c["symbol"], "rank": c["rank"], "result": sig})
            seen[c["id"]] = {"signal": sig["signal"], "lastSeen": today}

    # Coins that left ACCUMULATE are cleared so they alert fresh if they qualify again later
    for k in [k for k, v in seen.items() if (v or {}).get("lastSeen") != today]:
        del seen[k]
    state["last_discovery"] = today
    if newly_qualified:
        newly_qualified.sort(key=lambda c: (-c["result"]["strength"], c["result"].get("athPct") or 0))
        if send_telegram(candidate_discovery_message(newly_qualified)):
            print(f"Candidate discovery reported {len(newly_qualified)} new ACCUMULATE candidate(s).")
        else:
            # not delivered: forget them so tomorrow's/the next scan alerts again instead of treating them as already reported
            for c in newly_qualified:
                seen.pop(c["id"], None)
            state["last_discovery"] = ""
            print("Candidate alert not delivered; discovery will retry next run.")
    else:
        print("Candidate discovery complete; no new ACCUMULATE candidates.")
    return True

# ── Main ──────────────────────────────────────────────────────────
def main():
    try:
        state = load_state()
    except StateCorrupt as e:
        send_telegram(f"\u26A0\uFE0F Tracker bot: state.json is unreadable ({html.escape(str(e))}). Fix or restore it; "
                      "the bot has NOT overwritten it.")
        raise SystemExit(f"state.json unreadable: {e}")
    migrated = migrate_state(state)
    ids = list(PORTFOLIO)
    all_ids = list(dict.fromkeys(ids + [MACRO_ID]))
    for cache in ("ta_cache", "ath_cache"):  # prune coins no longer tracked
        c = state.setdefault(cache, {})
        for k in [k for k in c if k not in all_ids]:
            del c[k]

    rate, source = resolve_fx(state)
    if not rate:
        send_telegram("\u26A0\uFE0F Tracker bot: USDT/INR rate unavailable (no FX feed reachable, nothing stored).")
        return
    print(f"USDT/INR {rate:.4f} ({source})")

    print("Fetching Binance market data...")
    info = fetch_exchange_info()
    tickers = fetch_tickers(all_ids)
    if tickers is None:
        send_telegram("\u26A0\uFE0F Tracker bot: Binance market data fetch failed.")
        return
    by_sym = {t["symbol"]: t for t in tickers if t.get("symbol")}
    market = {s: normalize_ticker(by_sym[s], rate, ((info or {}).get(s) or {}).get("tick")) if s in by_sym else {"notFound": True}
              for s in all_ids}

    print("Refreshing TA (if stale)...")
    refresh_ta(state, all_ids, rate)
    ta_cache, ath_cache = state["ta_cache"], state["ath_cache"]
    for s in all_ids:
        if not market[s].get("notFound"):
            ensure_ath_atl(ath_cache, s, ta_cache.get(s))
            market[s].update(resolve_ath_atl(ath_cache.get(s), market[s]["current_price"], rate))
    save_state(state)

    tas = {s: build_ta(ta_cache.get(s), rate) for s in all_ids}
    macro_ta = tas.get(MACRO_ID)
    dca_log = state.get("dca_log", {})
    results = {cid: classify_holding(cid, market.get(cid), PORTFOLIO[cid], tas.get(cid), macro_ta, dca_log, rate) for cid in ids}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prev_signals = state.get("signals", {})
    undelivered = set()  # coins whose change alert failed to send: their old stored signal is kept so it re-sends next run
    if migrated:
        # first run on Binance data: signals were just rebuilt from scratch, so report them once instead of as 13 changes
        if send_telegram(digest_message(results, "Tracker bot now runs on Binance data — current signals")):
            state["last_daily"] = today
    else:
        for cid, r in results.items():
            if prev_signals.get(cid) != r["signal"]:
                if not send_telegram(change_message(cid, prev_signals.get(cid), r)):
                    undelivered.add(cid)
                print(f"Signal change: {PORTFOLIO[cid]['symbol']} {prev_signals.get(cid)} -> {r['signal']}")
        hour = datetime.now(timezone.utc).hour
        if state.get("last_daily") != today and 3 <= hour < 5:
            if send_telegram(digest_message(results)):
                state["last_daily"] = today
                print("Daily digest sent.")
    state["signals"] = {cid: (prev_signals.get(cid) if cid in undelivered else r["signal"]) for cid, r in results.items()}

    run_candidate_discovery(state, macro_ta, rate, info)
    save_state(state)
    print("Done.")

if __name__ == "__main__":
    main()
