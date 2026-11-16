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
API_BASE = "https://api.coingecko.com/api/v3"
API_KEY = os.environ.get("COINGECKO_API_KEY", "")

STATE_FILE = "state.json"
PORTFOLIO = {
    "elrond-erd-2":            {"symbol": "EGLD",  "target": 7497.09},
    "kava":                    {"symbol": "KAVA",  "target": 93.11},
    "axie-infinity":           {"symbol": "AXS",   "target": 1295.67},
    "polkadot":                {"symbol": "DOT",   "target": 1379.78},
    "theta-fuel":              {"symbol": "TFUEL", "target": 8.95},
    "polygon-ecosystem-token": {"symbol": "POL",   "target": 98.5},
    "the-graph":               {"symbol": "GRT",   "target": 12.11},
    "avalanche-2":             {"symbol": "AVAX",  "target": 6612.02},
    "1inch":                   {"symbol": "1INCH", "target": 64.6},
    "internet-computer":       {"symbol": "ICP",   "target": 1765.6},
    "nano":                    {"symbol": "XNO",   "target": 247.4},
    "ethereum":                {"symbol": "ETH",   "target": 431946},
    "bitcoin":                 {"symbol": "BTC",   "target": 11187013},
}
MACRO_ID = "bitcoin"

# ── Signal-engine constants (1:1 with Portfolio_Tracker.html) ─────
DCA_EXTREME_PCT   = -85
DCA_DEEP_PCT      = -70
DCA_MODERATE_PCT  = -40
DCA_WATCH_PCT     = -20
DCA_DIP_WINDOW_DAYS = 90
DCA_DIP_NEAR_PCT    = 3
DCA_VOL_LOOKBACK_DAYS = 20
DCA_GAP_VOL_LOOKBACK_DAYS = 90
DCA_STEP_CHAIN_MAX_DAYS = 180
DCA_GAP_TIERS = [
    {"floor": 15, "ceil": 20, "mult": 5.5},
    {"floor": 22, "ceil": 27, "mult": 7.5},
    {"floor": 28, "ceil": 35, "mult": 9.5},
]
REL_STRENGTH_WINDOW_DAYS = 14
REL_STRENGTH_SCALE     = 3
REL_STRENGTH_MIN_BONUS = -10
REL_STRENGTH_MAX_BONUS = 8
EMA_TREND_FAST_SHORT = 20
EMA_TREND_FAST_LONG  = 50
EMA_TREND_LONG = 200
RSI_PERIOD    = 14
RSI_OVERSOLD  = 30
RSI_OVERBOUGHT = 85
TARGET_PROXIMITY_PCT = 3
LIQUIDITY_DEAD_RATIO = 0.0005
ABSOLUTE_DEAD_VOLUME = 100000
MICROCAP_FLOOR = 2000000
RANK_CAUTION   = 500
TA_HISTORY_DAYS = 365
TA_ALIGN_BUFFER_MIN = 15

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

YEAR_HIGH_DAYS = 365
DCA_YEAR_EXTREME_PCT  = -60
DCA_YEAR_DEEP_PCT     = -45
DCA_YEAR_MODERATE_PCT = -30
YEAR_TIER_ABS_FLOOR_PCT = -80
YEAR_TIER_ABS_CEIL_PCT  = -10
TA_MAX_DATA_AGE_MS = 3 * 24 * 3600 * 1000
TA_FETCH_DELAY_S = 1.5

DISCOVERY_MARKET_LIMIT = 100
DISCOVERY_TA_LIMIT = 25
DISCOVERY_DELAY_S = 2.1
DISCOVERY_REPORT_SIGNALS = {"ACCUMULATE"}

TA_SCHEMA_VERSION = 7

_LAST_HTTP_ERR = None

# ── Telegram ──────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing; skipping message.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        if not r.ok:
            print(f"Telegram error: {r.text}")
    except Exception as e:
        print(f"Telegram failed: {e}")

# ── State ─────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"signals": {}, "ta_cache": {}, "ta_timestamp": 0, "last_daily": "", "dca_log": {}}

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)

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
        key=lambda h: (str(h.get("dateStr") or ""), float(h.get("timestamp") or 0)),
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
    idx = int((pct / 100) * (len(sorted_asc) - 1))
    return sorted_asc[idx]

def reconstruct_streak_days(closes, cur, rsi_threshold=None, near_low_threshold_pct=None, window_days=None):
    if not closes or cur is None:
        return 1
    threshold = rsi_threshold if rsi_threshold is not None else RSI_OVERSOLD
    near_low_pct = near_low_threshold_pct if near_low_threshold_pct is not None else DCA_DIP_NEAR_PCT
    window_days = window_days or DCA_DIP_WINDOW_DAYS
    series = closes + [cur]
    rsi_series = calc_rsi_series(series, RSI_PERIOD)
    streak = 0
    for i in range(len(series) - 1, -1, -1):
        rsi = rsi_series[i] if i < len(rsi_series) else None
        if rsi is None:
            break
        window_start = max(0, i - window_days + 1)
        low = min(series[window_start:i + 1])
        above_low = (series[i] - low) / low * 100
        if not (above_low <= near_low_pct and rsi <= threshold):
            break
        streak += 1
    return streak or 1

def compute_ta_from_closes(closes):
    if not closes or len(closes) < RSI_PERIOD + 1:
        return None
    ema_long_series = calc_ema_series(closes, EMA_TREND_LONG)
    rsi_series = calc_rsi_series(closes, RSI_PERIOD)
    dip_window = closes[-DCA_DIP_WINDOW_DAYS:]
    rolling_low = min(dip_window) if dip_window else None
    rolling_low_index = dip_window.index(rolling_low) if dip_window else -1
    rolling_high = max(dip_window) if dip_window else None
    rolling_high_index = dip_window.index(rolling_high) if dip_window else -1

    valid_rsi = [v for v in rsi_series if v is not None]
    rsi_oversold_threshold = RSI_OVERSOLD
    if len(valid_rsi) >= RSI_OVERSOLD_MIN_SAMPLES:
        p = percentile_of(sorted(valid_rsi), RSI_OVERSOLD_PERCENTILE)
        if p is not None:
            rsi_oversold_threshold = max(RSI_OVERSOLD_FLOOR, min(RSI_OVERSOLD_CEIL, p))
    rsi_overbought_threshold = RSI_OVERBOUGHT
    if len(valid_rsi) >= RSI_OVERBOUGHT_MIN_SAMPLES:
        p_high = percentile_of(sorted(valid_rsi), RSI_OVERBOUGHT_PERCENTILE)
        if p_high is not None:
            rsi_overbought_threshold = max(RSI_OVERBOUGHT_FLOOR, min(RSI_OVERBOUGHT_CEIL, p_high))

    avg_daily_move = calc_avg_daily_move_pct(closes, DCA_VOL_LOOKBACK_DAYS)
    gap_avg_daily_move = calc_avg_daily_move_pct(closes, DCA_GAP_VOL_LOOKBACK_DAYS)
    if gap_avg_daily_move is None:
        gap_avg_daily_move = avg_daily_move
    vol_scale = max(VOL_SCALE_MIN, min(VOL_SCALE_MAX, avg_daily_move / VOL_SCALE_REF_PCT)) if avg_daily_move is not None else 1
    near_low_threshold_pct = max(
        DCA_NEAR_LOW_FLOOR_PCT,
        min(DCA_NEAR_LOW_CEIL_PCT, (avg_daily_move if avg_daily_move is not None else DCA_NEAR_LOW_FLOOR_PCT) * DCA_NEAR_LOW_MULT)
    )

    def clamp_tier(base):
        return max(ATH_TIER_ABS_FLOOR_PCT, min(ATH_TIER_ABS_CEIL_PCT, base * vol_scale))

    ath_tiers = {
        "extreme": clamp_tier(DCA_EXTREME_PCT),
        "deep": clamp_tier(DCA_DEEP_PCT),
        "moderate": clamp_tier(DCA_MODERATE_PCT),
        "watch": clamp_tier(DCA_WATCH_PCT),
    }

    def clamp_year_tier(base):
        return max(YEAR_TIER_ABS_FLOOR_PCT, min(YEAR_TIER_ABS_CEIL_PCT, base * vol_scale))

    year_tiers = {
        "extreme": clamp_year_tier(DCA_YEAR_EXTREME_PCT),
        "deep": clamp_year_tier(DCA_YEAR_DEEP_PCT),
        "moderate": clamp_year_tier(DCA_YEAR_MODERATE_PCT),
    }

    return {
        "rsi": rsi_series[-1] if rsi_series else None,
        "rsiOversoldThreshold": rsi_oversold_threshold,
        "rsiOverboughtThreshold": rsi_overbought_threshold,
        "avgDailyMove": avg_daily_move,
        "gapAvgDailyMove": gap_avg_daily_move,
        "nearLowThresholdPct": near_low_threshold_pct,
        "athTiers": ath_tiers,
        "yearTiers": year_tiers,
        "emaLong": ema_long_series[-1] if ema_long_series else None,
        "rollingLow": rolling_low,
        "rollingLowDaysAgo": (len(dip_window) - rolling_low_index) if rolling_low_index >= 0 else None,
        "rollingHigh": rolling_high,
        "rollingHighDaysAgo": (len(dip_window) - rolling_high_index) if rolling_high_index >= 0 else None,
        "rollingLowWindowDays": len(dip_window),
        "rollingExtremesSource": "closes",
        "closes": closes,
        "schemaVersion": TA_SCHEMA_VERSION,
        "timestamp": time.time() * 1000,
    }

def audit_ta(ta):
    if not ta:
        return True
    ath_tiers = ta.get("athTiers") or {}
    year_tiers = ta.get("yearTiers") or {}
    fields = [
        ta.get("rsi"), ta.get("emaLong"), ta.get("rollingLow"), ta.get("rollingHigh"),
        ta.get("rsiOversoldThreshold"), ta.get("rsiOverboughtThreshold"),
        ta.get("nearLowThresholdPct"), ta.get("gapAvgDailyMove"),
        ath_tiers.get("extreme"), ath_tiers.get("deep"), ath_tiers.get("moderate"), ath_tiers.get("watch"),
        year_tiers.get("extreme"), year_tiers.get("deep"), year_tiers.get("moderate"),
    ]
    for v in fields:
        if v is not None and not math.isfinite(v):
            return False
    return True

def days_since(date_str):
    if not date_str:
        return None
    try:
        then = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        return None
    return max(0, round((datetime.now(timezone.utc) - then).total_seconds() / 86400))

def days_since_ts(ts_ms):
    if ts_ms is None:
        return None
    then = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return max(0, round((datetime.now(timezone.utc) - then).total_seconds() / 86400))

# ── Data fetch ────────────────────────────────────────────────────
def fetch_ta_history(coin_id):
    global _LAST_HTTP_ERR
    _LAST_HTTP_ERR = None
    try:
        r = requests.get(
            f"{API_BASE}/coins/{coin_id}/market_chart",
            params={"vs_currency": "inr", "days": TA_HISTORY_DAYS, "interval": "daily", "x_cg_demo_api_key": API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        points = [p for p in r.json().get("prices", []) if p and p[1] is not None]
        if not points:
            _LAST_HTTP_ERR = "empty history"
            return None
        last_date = datetime.fromtimestamp(points[-1][0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        usable = points[:-1] if last_date == today else points
        if not usable:
            _LAST_HTTP_ERR = "empty history"
            return None
        return [p[1] for p in usable], usable[-1][0]
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "HTTP"
        _LAST_HTTP_ERR = f"HTTP_ERROR:{code}"
        print(f"TA fetch failed for {coin_id}: {_LAST_HTTP_ERR}")
        return None
    except Exception as e:
        _LAST_HTTP_ERR = str(e)
        print(f"TA fetch failed for {coin_id}: {e}")
        return None

def _get_json(url, params):
    global _LAST_HTTP_ERR
    _LAST_HTTP_ERR = None
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "HTTP"
        _LAST_HTTP_ERR = f"HTTP_ERROR:{code}"
        print(f"Fetch failed {url}: {_LAST_HTTP_ERR}")
        return None
    except Exception as e:
        _LAST_HTTP_ERR = str(e)
        print(f"Fetch failed {url}: {e}")
        return None

def fetch_range_extremes(coin_id):
    global _LAST_HTTP_ERR
    DAY = 86400000
    BUCKET = 4 * DAY
    now = time.time() * 1000
    cutoff90 = now - DCA_DIP_WINDOW_DAYS * DAY
    cutoff_year = now - YEAR_HIGH_DAYS * DAY

    ohlc = _get_json(f"{API_BASE}/coins/{coin_id}/ohlc",
                     {"vs_currency": "inr", "days": YEAR_HIGH_DAYS, "x_cg_demo_api_key": API_KEY})
    ohlc_err = _LAST_HTTP_ERR
    time.sleep(0.5)
    hourly = _get_json(f"{API_BASE}/coins/{coin_id}/market_chart",
                       {"vs_currency": "inr", "days": DCA_DIP_WINDOW_DAYS, "x_cg_demo_api_key": API_KEY})
    if _LAST_HTTP_ERR is None and ohlc_err is not None:
        _LAST_HTTP_ERR = ohlc_err

    slots = {"d90High": None, "d90Low": None, "yHigh": None, "yLow": None}

    def take(key, val, ts, src, is_high):
        if val is None or ts is None or val != val:
            return
        cur = slots[key]
        if cur is None or (val > cur["val"] if is_high else val < cur["val"]):
            slots[key] = {"val": val, "ts": ts, "src": src}

    any_data = False
    if isinstance(ohlc, list):
        for c in ohlc:
            if not isinstance(c, list) or len(c) < 5:
                continue
            ts, high, low = c[0], c[2], c[3]
            if ts is None or ts < cutoff_year:
                continue
            any_data = True
            take("yHigh", high, ts, "ohlc", True)
            take("yLow", low, ts, "ohlc", False)
            if ts >= cutoff90 + BUCKET:
                take("d90High", high, ts, "ohlc", True)
                take("d90Low", low, ts, "ohlc", False)

    pts = hourly.get("prices") if isinstance(hourly, dict) else None
    if isinstance(pts, list):
        for pt in pts:
            if not isinstance(pt, list) or len(pt) < 2 or pt[0] < cutoff90:
                continue
            any_data = True
            take("d90High", pt[1], pt[0], "hourly", True)
            take("d90Low", pt[1], pt[0], "hourly", False)
            take("yHigh", pt[1], pt[0], "hourly", True)
            take("yLow", pt[1], pt[0], "hourly", False)

        REFINE_TOL = 0.015
        for sl in slots.values():
            if not sl or sl["src"] != "ohlc":
                continue
            best, best_diff = None, float("inf")
            for pt in pts:
                if not isinstance(pt, list) or len(pt) < 2 or abs(pt[0] - sl["ts"]) > BUCKET:
                    continue
                diff = abs(pt[1] - sl["val"])
                if diff < best_diff:
                    best_diff, best = diff, pt
            if best is not None and best_diff / sl["val"] <= REFINE_TOL:
                sl["ts"], sl["src"] = best[0], "hourly"

    return slots if any_data else None

def apply_range_extremes(ta, closes, ext):
    ta["rollingHighSrc"] = ta["rollingLowSrc"] = "closes"
    w = closes[-YEAR_HIGH_DAYS:]
    y_hi, y_lo = max(w), min(w)
    ta["year"] = {
        "high": y_hi, "highDaysAgo": len(w) - w.index(y_hi), "highSrc": "closes",
        "low": y_lo, "lowDaysAgo": len(w) - w.index(y_lo), "lowSrc": "closes",
    }
    if ext:
        def d_ago(ts, cap):
            return min(days_since_ts(ts), cap)
        h90, l90, yh, yl = ext["d90High"], ext["d90Low"], ext["yHigh"], ext["yLow"]
        if h90 and h90["val"] > ta["rollingHigh"]:
            ta["rollingHigh"], ta["rollingHighDaysAgo"], ta["rollingHighSrc"] = h90["val"], d_ago(h90["ts"], DCA_DIP_WINDOW_DAYS), h90["src"]
        if l90 and l90["val"] < ta["rollingLow"]:
            ta["rollingLow"], ta["rollingLowDaysAgo"], ta["rollingLowSrc"] = l90["val"], d_ago(l90["ts"], DCA_DIP_WINDOW_DAYS), l90["src"]
        if yh and yh["val"] > ta["year"]["high"]:
            ta["year"].update(high=yh["val"], highDaysAgo=d_ago(yh["ts"], YEAR_HIGH_DAYS), highSrc=yh["src"])
        if yl and yl["val"] < ta["year"]["low"]:
            ta["year"].update(low=yl["val"], lowDaysAgo=d_ago(yl["ts"], YEAR_HIGH_DAYS), lowSrc=yl["src"])
        srcs = [ta["rollingHighSrc"], ta["rollingLowSrc"]]
        ta["rollingExtremesSource"] = "ohlc" if "ohlc" in srcs else ("hourly" if "hourly" in srcs else "closes")
    else:
        ta["rollingExtremesSource"] = "closes"
    ta["schemaVersion"] = TA_SCHEMA_VERSION
    return ta

def normalize_market_row(coin):
    return {
        "total_volume": coin.get("total_volume"),
        "market_cap": coin.get("market_cap"),
        "market_cap_rank": coin.get("market_cap_rank"),
        "ath": coin.get("ath"),
        "ath_change_percentage": coin.get("ath_change_percentage"),
        "ath_date": coin.get("ath_date"),
        "atl": coin.get("atl"),
        "atl_date": coin.get("atl_date"),
        "current_price": coin.get("current_price"),
        "high_24h": coin.get("high_24h"),
        "low_24h": coin.get("low_24h"),
        "change_24h": coin.get("price_change_percentage_24h"),
        "change_7d": coin.get("price_change_percentage_7d_in_currency"),
        "change_30d": coin.get("price_change_percentage_30d_in_currency"),
        "notFound": False,
        "symbol": str(coin.get("symbol") or "").upper(),
        "name": str(coin.get("name") or coin.get("id") or ""),
    }

def fetch_market_data(ids):
    try:
        r = requests.get(
            f"{API_BASE}/coins/markets",
            params={
                "vs_currency": "inr", "ids": ",".join(ids), "order": "market_cap_desc",
                "per_page": 250, "page": 1, "sparkline": "false",
                "price_change_percentage": "7d,30d", "x_cg_demo_api_key": API_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        market, found = {}, set()
        for coin in data:
            found.add(coin["id"])
            market[coin["id"]] = normalize_market_row(coin)
        for cid in ids:
            if cid not in found:
                market[cid] = {"notFound": True}
        return market
    except Exception as e:
        print(f"Market data fetch failed: {e}")
        return None

def fetch_market_universe():
    try:
        r = requests.get(
            f"{API_BASE}/coins/markets",
            params={
                "vs_currency": "inr", "order": "market_cap_desc",
                "per_page": DISCOVERY_MARKET_LIMIT, "page": 1, "sparkline": "false",
                "price_change_percentage": "7d,30d", "x_cg_demo_api_key": API_KEY,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        market = {}
        for coin in data:
            cid = coin.get("id")
            if cid:
                market[cid] = normalize_market_row(coin)
        return market
    except Exception as e:
        print(f"Market-universe discovery fetch failed: {e}")
        return None

# ── Signal engine (1:1 with Portfolio_Tracker.html) ───────────────
EXTREME_TOL = 5e-4

def rsi_at_extreme(series, days_ago, is_live):
    if not series or days_ago is None:
        return None
    n = len(series)
    if is_live or days_ago <= 0:
        return series[n - 1]
    idx = n - 1 - days_ago
    if idx < 0:
        return None
    v = series[idx]
    return None if v is None or v != v else v

def refine_extreme_day(closes, days_ago, known_price):
    if not closes or days_ago is None or known_price is None:
        return days_ago
    best_k, best_diff = days_ago, float("inf")
    lo, hi = max(1, days_ago - 4), min(len(closes), days_ago + 4, DCA_DIP_WINDOW_DAYS)
    for k in range(lo, hi + 1):
        c = closes[len(closes) - k]
        if c is None or c != c:
            continue
        d = abs(c - known_price)
        if d < best_diff:
            best_diff, best_k = d, k
    return best_k

def classify_holding(coin_id, md, item, ta, macro_ta, dca_log):
    if md is None:
        return {"signal": "NA", "strength": 0, "reason": "No market data yet.", "cmp": None}
    if md.get("notFound"):
        return {"signal": "NA", "strength": 0, "reason": "Not found in latest CoinGecko data — may be delisted.", "cmp": None}

    volume, mcap, rank = md.get("total_volume"), md.get("market_cap"), md.get("market_cap_rank")
    cur = md.get("current_price")
    high_24h, low_24h = md.get("high_24h"), md.get("low_24h")
    md_ath, md_atl = md.get("ath"), md.get("atl")
    ath_value = max(md_ath, cur) if (md_ath is not None and cur is not None) else md_ath
    ath_pct = (md.get("ath_change_percentage") if ath_value == md_ath else 0) if (ath_value is not None and cur is not None) else md.get("ath_change_percentage")
    atl_value = min(md_atl, cur) if (md_atl is not None and cur is not None) else md_atl
    atl_date = md.get("atl_date")
    ath_date = md.get("ath_date")
    target = item.get("target")

    own_ret = calc_return_pct(ta.get("closes") if ta else None, REL_STRENGTH_WINDOW_DAYS)
    btc_ret = calc_return_pct(macro_ta.get("closes") if macro_ta else None, REL_STRENGTH_WINDOW_DAYS)
    rel_strength = (own_ret - btc_ret) if (own_ret is not None and btc_ret is not None) else None

    live_closes = (ta["closes"] + [cur]) if (ta and ta.get("closes") and cur is not None) else (ta.get("closes") if ta else None)
    live_rsi_series = calc_rsi_series(live_closes, RSI_PERIOD) if live_closes else []
    live_rsi = live_rsi_series[-1] if live_rsi_series else (ta.get("rsi") if ta else None)
    live_ema_long_series = calc_ema_series(live_closes, EMA_TREND_LONG) if live_closes else []
    live_ema_long = live_ema_long_series[-1] if live_ema_long_series else (ta.get("emaLong") if ta else None)
    ema_fs = calc_ema_series(live_closes, EMA_TREND_FAST_SHORT) if live_closes else []
    ema_fl = calc_ema_series(live_closes, EMA_TREND_FAST_LONG) if live_closes else []
    sentiment = None
    if ema_fs and ema_fl:
        a, b = ema_fs[-1], ema_fl[-1]
        sentiment = "Bullish" if a > b else ("Bearish" if a < b else "Mixed")

    no_trading_data = volume is None or mcap is None
    zero_mcap = (not no_trading_data) and mcap == 0
    vol_mcap_ratio = (volume / mcap) if (not no_trading_data and mcap > 0) else None
    negligible_volume = (not no_trading_data) and mcap < MICROCAP_FLOOR and volume < ABSOLUTE_DEAD_VOLUME
    dead_liquidity = no_trading_data or volume == 0 or zero_mcap or negligible_volume or (vol_mcap_ratio is not None and vol_mcap_ratio < LIQUIDITY_DEAD_RATIO)
    microcap = mcap is not None and mcap < MICROCAP_FLOOR
    poor_rank = rank is None or rank > RANK_CAUTION
    at_risk = (not dead_liquidity) and microcap and poor_rank

    depth = abs(ath_pct) if ath_pct is not None else None
    ath_tiers = (ta or {}).get("athTiers") or {"extreme": DCA_EXTREME_PCT, "deep": DCA_DEEP_PCT, "moderate": DCA_MODERATE_PCT, "watch": DCA_WATCH_PCT}
    year_tiers = (ta or {}).get("yearTiers") or {"extreme": DCA_YEAR_EXTREME_PCT, "deep": DCA_YEAR_DEEP_PCT, "moderate": DCA_YEAR_MODERATE_PCT}
    yr = year_high_drawdown(ta.get("closes") if ta else None, cur, high_24h, low_24h,
                            ath_value, ath_date, atl_value, atl_date, (ta or {}).get("year"))
    yr_note = f", {abs(yr['pct']):.0f}% below {YEAR_HIGH_DAYS}d high" if yr else ""

    if dead_liquidity:
        signal, strength = "EXIT", (90 if (no_trading_data or zero_mcap) else max(60, min(95, round(100 - (vol_mcap_ratio or 0) / LIQUIDITY_DEAD_RATIO * 40))))
        reason = "No volume/market-cap data — trading may have stopped." if no_trading_data else \
                 "Real ₹0 market cap — likely defunct/delisted." if zero_mcap else \
                 f"Volume negligible against a {mcap:,.0f} market cap." if negligible_volume else \
                 f"Volume/market-cap ratio ({vol_mcap_ratio * 100:.2f}%) has dried up."
    elif at_risk:
        signal, strength = "AT RISK", 50
        reason = f"Micro-cap ({mcap:,.0f}, rank {rank}) with real survival risk."
    elif ath_pct is not None:
        if ath_pct <= ath_tiers["moderate"] and yr is not None and yr["pct"] > year_tiers["moderate"]:
            signal, strength = "HOLD", 35
            reason = (f"{depth:.0f}% below ATH, but only {abs(yr['pct']):.0f}% below its {YEAR_HIGH_DAYS}d high "
                      f"(needs ≥{abs(year_tiers['moderate']):.0f}% for this coin) — no meaningful recent pullback to buy.")
        elif ath_pct <= ath_tiers["extreme"] and (yr is None or yr["pct"] <= year_tiers["extreme"]):
            signal, strength, reason = "WAIT", 20, f"{depth:.0f}% below ATH{yr_note} — extreme, but depth alone isn't a floor. Watching for a better entry."
        elif ath_pct <= ath_tiers["deep"] and (yr is None or yr["pct"] <= year_tiers["deep"]):
            signal, strength, reason = "WAIT", 30, f"{depth:.0f}% below ATH{yr_note} — deep discount, but that alone doesn't confirm the fall has stopped."
        elif ath_pct <= ath_tiers["moderate"]:
            signal, strength, reason = "WAIT", 25, f"{depth:.0f}% below ATH{yr_note} — meaningful discount, but not yet a confirmed add."
        else:
            signal, strength = "HOLD", 35
            reason = (
                f"Only {depth:.0f}% below ATH — not a deep enough discount yet."
                if ath_pct <= ath_tiers["watch"]
                else f"Within {depth:.0f}% of ATH — holding for your target unless an extreme blow-off triggers."
            )
    else:
        signal, strength, reason = "HOLD", 25, "No ATH data yet."

    closes_all = (ta or {}).get("closes")
    window90 = (ta or {}).get("rollingLowWindowDays")
    atl_within_window = False
    if atl_date and ta and window90 is not None:
        ds = days_since(atl_date)
        atl_within_window = ds is not None and ds <= window90

    effective_low = None
    if ta and ta.get("rollingLow") is not None and cur is not None:
        candidates = [ta["rollingLow"], cur]
        if low_24h is not None:
            candidates.append(low_24h)
        if atl_within_window and atl_value is not None:
            candidates.append(atl_value)
        effective_low = min(candidates)

    ath_within_window = False
    if ath_date and ta and window90 is not None:
        ds = days_since(ath_date)
        ath_within_window = ds is not None and ds <= window90
    effective_high = None
    if ta and ta.get("rollingHigh") is not None and cur is not None:
        candidates_h = [ta["rollingHigh"], cur]
        if high_24h is not None:
            candidates_h.append(high_24h)
        if ath_within_window and ath_value is not None:
            candidates_h.append(ath_value)
        effective_high = max(candidates_h)

    is_live_low = (effective_low is not None and cur is not None
                   and (effective_low == cur or effective_low == low_24h)
                   and (ta.get("rollingLow") is None or effective_low < ta["rollingLow"]))
    atl_sets_low = atl_within_window and atl_value is not None and effective_low == atl_value
    low_days = 0 if is_live_low else (days_since(atl_date) if atl_sets_low else (ta or {}).get("rollingLowDaysAgo"))
    low_approx = (not is_live_low) and (not atl_sets_low) and ((ta or {}).get("rollingLowSrc") or (ta or {}).get("rollingExtremesSource")) == "ohlc"
    low_day_k = refine_extreme_day(closes_all, low_days, effective_low) if low_approx else low_days
    low_rsi = rsi_at_extreme(live_rsi_series, low_day_k, is_live_low)

    is_live_high = (effective_high is not None and cur is not None
                    and (effective_high == cur or effective_high == high_24h)
                    and (ta.get("rollingHigh") is None or effective_high > ta["rollingHigh"]))
    ath_sets_high = ath_within_window and ath_value is not None and effective_high == ath_value
    high_days = 0 if is_live_high else (days_since(ath_date) if ath_sets_high else (ta or {}).get("rollingHighDaysAgo"))
    high_approx = (not is_live_high) and (not ath_sets_high) and ((ta or {}).get("rollingHighSrc") or (ta or {}).get("rollingExtremesSource")) == "ohlc"
    high_day_k = refine_extreme_day(closes_all, high_days, effective_high) if high_approx else high_days
    high_rsi = rsi_at_extreme(live_rsi_series, high_day_k, is_live_high)

    live_rsi_now = rsi_at_extreme(live_rsi_series, 0, True)

    def rsi_of(c):
        if c.get("live"):
            return live_rsi_now
        k = c.get("k")
        if k is None or not closes_all or k > len(closes_all):
            return None
        if not c.get("approx"):
            return rsi_at_extreme(live_rsi_series, k, False)
        best_k, best_d = None, float("inf")
        for kk in range(max(1, k - 4), min(len(closes_all), k + 4) + 1):
            if rsi_at_extreme(live_rsi_series, kk, False) is None:
                continue
            d = abs(closes_all[len(closes_all) - kk] - c["v"])
            if d < best_d:
                best_d, best_k = d, kk
        return None if best_k is None else rsi_at_extreme(live_rsi_series, best_k, False)

    def pick_extreme(cands, is_low):
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

    eff_low_c = {"v": effective_low, "k": low_day_k, "live": is_live_low, "approx": False, "rsi": low_rsi} if effective_low is not None else None
    eff_high_c = {"v": effective_high, "k": high_day_k, "live": is_live_high, "approx": False, "rsi": high_rsi} if effective_high is not None else None
    atl_k = days_since(atl_date) if atl_date else None
    ath_k = days_since(ath_date) if ath_date else None
    is_live_atl = atl_value is not None and cur is not None and atl_value == cur and (md_atl is None or cur < md_atl)
    is_live_ath = ath_value is not None and cur is not None and ath_value == cur and (md_ath is None or cur > md_ath)
    atl_c = {"v": atl_value, "k": 0 if is_live_atl else atl_k, "live": is_live_atl, "approx": False} if (atl_value is not None and atl_k is not None) else None
    ath_c = {"v": ath_value, "k": 0 if is_live_ath else ath_k, "live": is_live_ath, "approx": False} if (ath_value is not None and ath_k is not None) else None

    def build_win(n_days):
        if not closes_all or cur is None:
            return None
        w = closes_all[-n_days:]
        c_lo, c_hi = min(w), max(w)
        yr_low = {"v": yr["low"], "k": yr["lowDaysAgo"], "live": yr["lowDaysAgo"] == 0, "approx": bool(yr.get("lowApprox"))} \
            if (yr and yr.get("lowDaysAgo") is not None and yr["lowDaysAgo"] <= n_days) else None
        yr_high = {"v": yr["high"], "k": yr["highDaysAgo"], "live": yr["highDaysAgo"] == 0, "approx": bool(yr.get("highApprox"))} \
            if (yr and yr.get("highDaysAgo") is not None and yr["highDaysAgo"] <= n_days) else None
        return {
            "n": len(w),
            "lo": pick_extreme([eff_low_c, yr_low, atl_c if (atl_k is not None and atl_k <= n_days) else None,
                                {"v": c_lo, "k": len(w) - w.index(c_lo), "live": False, "approx": False}], True),
            "hi": pick_extreme([eff_high_c, yr_high, ath_c if (ath_k is not None and ath_k <= n_days) else None,
                                {"v": c_hi, "k": len(w) - w.index(c_hi), "live": False, "approx": False}], False),
        }

    win180 = build_win(180)
    win365 = build_win(YEAR_HIGH_DAYS)

    w90 = window90 if window90 is not None else DCA_DIP_WINDOW_DAYS
    os_thr = (ta or {}).get("rsiOversoldThreshold", RSI_OVERSOLD)
    ob_thr = (ta or {}).get("rsiOverboughtThreshold", RSI_OVERBOUGHT)
    low_cands, high_cands = [], []
    if eff_low_c:
        low_cands.append({"w": w90, "v": eff_low_c["v"], "rsi": eff_low_c["rsi"]})
    if eff_high_c:
        high_cands.append({"w": w90, "v": eff_high_c["v"], "rsi": eff_high_c["rsi"]})
    if win180 and win180["n"] > w90 + 5:
        if win180["lo"]:
            low_cands.append({"w": 180, "v": win180["lo"]["v"], "rsi": win180["lo"]["rsi"]})
        if win180["hi"]:
            high_cands.append({"w": 180, "v": win180["hi"]["v"], "rsi": win180["hi"]["rsi"]})
    if win365:
        if win365["lo"]:
            low_cands.append({"w": YEAR_HIGH_DAYS, "v": win365["lo"]["v"], "rsi": win365["lo"]["rsi"]})
        if win365["hi"]:
            high_cands.append({"w": YEAR_HIGH_DAYS, "v": win365["hi"]["v"], "rsi": win365["hi"]["rsi"]})

    def pick_ref_window(cands, thr, is_low):
        valid = [c for c in cands if c["rsi"] is not None]
        hit = next((c for c in valid if (c["rsi"] <= thr if is_low else c["rsi"] >= thr)), None)
        if hit:
            return hit
        best = None
        for c in valid:
            if best is None or (c["rsi"] < best["rsi"] if is_low else c["rsi"] > best["rsi"]):
                best = c
        return best or (cands[0] if cands else None)

    q_low = pick_ref_window(low_cands, os_thr + REF_LOW_RSI_MARGIN, True)
    q_high = pick_ref_window(high_cands, ob_thr, False)
    gate_window = q_low["w"] if q_low else w90
    gate_low = q_low["v"] if q_low else effective_low
    gate_high_window = q_high["w"] if q_high else w90
    gate_high = q_high["v"] if q_high else effective_high

    dca_streak_day = None
    add_reason_shown = False
    dca_gap_pct = None
    dca_required_gap_pct = None
    next_dca_price = None
    if signal == "WAIT":
        rel_bonus, rel_note = 0, ""
        if rel_strength is not None:
            rel_bonus = max(REL_STRENGTH_MIN_BONUS, min(REL_STRENGTH_MAX_BONUS, rel_strength / REL_STRENGTH_SCALE))
            rel_note = f" {'Outperforming' if rel_strength > 0 else 'Underperforming'} BTC ({rel_strength:+.1f}pp/{REL_STRENGTH_WINDOW_DAYS}d)."

        if ta and ta.get("rollingLow") is not None and cur is not None:
            add_reason_shown = True
            above_low = (cur - gate_low) / gate_low * 100
            near_low_threshold_pct = ta.get("nearLowThresholdPct", DCA_DIP_NEAR_PCT)
            near_low = above_low <= near_low_threshold_pct
            rsi_threshold = ta.get("rsiOversoldThreshold", RSI_OVERSOLD)
            rsi_live_oversold = live_rsi is not None and live_rsi <= rsi_threshold
            rsi_closed_oversold = ta.get("rsi") is not None and ta["rsi"] <= rsi_threshold
            rsi_oversold = rsi_live_oversold and rsi_closed_oversold
            gap_avg_daily_move = ta.get("gapAvgDailyMove") if ta.get("gapAvgDailyMove") is not None else ta.get("avgDailyMove")
            closed_rsi_txt = "—" if ta.get("rsi") is None else f"{ta['rsi']:.2f}"
            if near_low and rsi_oversold:
                dca_streak_day = reconstruct_streak_days(ta["closes"], cur, rsi_threshold, near_low_threshold_pct, gate_window)
                last_log = (dca_log or {}).get(coin_id)
                gap_status = get_dca_gap_status(cur, last_log, gap_avg_daily_move, dca_history_of(dca_log, coin_id))
                dyn_gap_pct = gap_status["requiredGapPct"]
                gap_pct = gap_status["gapPct"]
                dca_gap_pct = gap_pct
                dca_required_gap_pct = dyn_gap_pct
                next_dca_price = gap_status["nextDcaPrice"]
                if gap_status["cleared"]:
                    signal, strength = "ACCUMULATE", round(min(60, strength + 30) + rel_bonus)
                    gap_note = ""
                    if last_log is not None and gap_pct is not None:
                        gap_note = f" Also {gap_pct:.1f}% below last DCA ({fmt_inr(gap_status['lastDcaPrice'])}) — a real step down."
                    reason = f"{depth:.0f}% below ATH{yr_note}, {above_low:.1f}% above its {gate_window}d low (band ≤{near_low_threshold_pct:.1f}%), RSI {live_rsi:.2f} (oversold vs own ≤{rsi_threshold:.0f}) — day {dca_streak_day}.{gap_note}{rel_note}"
                elif gap_pct is None:
                    reason = f"Still meets add conditions (day {dca_streak_day}), but previous DCA price is invalid. Held at WAIT.{rel_note}"
                else:
                    last_price = gap_status["lastDcaPrice"]
                    next_price = gap_status["nextDcaPrice"]
                    position_note = (
                        f"only {gap_pct:.1f}% below last DCA ({fmt_inr(last_price)})"
                        if gap_pct >= 0
                        else f"{abs(gap_pct):.1f}% above last DCA ({fmt_inr(last_price)}), not below it"
                    )
                    step_note = f" (DCA #{gap_status['stepCount'] + 1})" if gap_status["stepCount"] > 1 else ""
                    reason = f"Still meets add conditions (day {dca_streak_day}), but {position_note}. Needs ≥{dyn_gap_pct:.1f}% below that{step_note} — next DCA near {fmt_inr(next_price)}. Held at WAIT.{rel_note}"
            else:
                if near_low and rsi_live_oversold and not rsi_closed_oversold:
                    reason = f"{depth:.0f}% below ATH{yr_note}, near its {gate_window}d low, live RSI {live_rsi:.2f} is oversold (≤{rsi_threshold:.0f}), but the last daily close (RSI {closed_rsi_txt}) hasn't confirmed it yet — waiting for a daily close."
                elif near_low and not rsi_oversold:
                    reason = f"{depth:.0f}% below ATH{yr_note}, near its {gate_window}d low, but RSI ({'—' if live_rsi is None else f'{live_rsi:.2f}'}) hasn't reached its own oversold zone (≤{rsi_threshold:.0f}) yet."
                else:
                    reason = f"{depth:.0f}% below ATH{yr_note}, but {abs(above_low):.1f}% {'above' if above_low >= 0 else 'below'} its {gate_window}d low — outside its own ≤{near_low_threshold_pct:.1f}% near-low band."
        else:
            reason = "Not enough price history loaded yet."

    d_to_low = (cur - effective_low) / cur if (effective_low is not None and cur) else None
    d_to_high = (effective_high - cur) / cur if (effective_high is not None and cur) else None
    lean = "swing" if (d_to_low is not None and d_to_high is not None and d_to_high < d_to_low) else "add"

    swing_high = gate_high if gate_high is not None else effective_high
    swing_window = gate_high_window
    ema_stretch_pct = (cur - live_ema_long) / live_ema_long * 100 if (live_ema_long is not None and live_ema_long > 0 and cur is not None) else None
    surge_30d_pct = md.get("change_30d")
    if surge_30d_pct is None:
        surge_30d_pct = calc_return_pct(live_closes, 30)

    if swing_high is not None and cur is not None and signal not in ("EXIT", "AT RISK", "ACCUMULATE", "TRIM"):
        below_high_pct = (swing_high - cur) / swing_high * 100
        near_high_threshold_pct = (ta or {}).get("nearLowThresholdPct", DCA_DIP_NEAR_PCT)
        near_high = below_high_pct <= near_high_threshold_pct
        rsi_overbought_threshold = (ta or {}).get("rsiOverboughtThreshold", RSI_OVERBOUGHT)
        rsi_live_overbought = live_rsi is not None and live_rsi >= rsi_overbought_threshold
        closed_rsi = (ta or {}).get("rsi")
        rsi_closed_overbought = closed_rsi is not None and closed_rsi >= rsi_overbought_threshold
        rsi_overbought = rsi_live_overbought and rsi_closed_overbought
        ema_stretched = ema_stretch_pct is not None and ema_stretch_pct >= SWING_TRIM_EMA_STRETCH_PCT
        surge_stretched = surge_30d_pct is not None and surge_30d_pct >= SWING_TRIM_SURGE_30D_PCT
        parabolic_stretched = ema_stretched or surge_stretched
        rsi_txt = "—" if live_rsi is None else f"{live_rsi:.2f}"

        if near_high and rsi_overbought and parabolic_stretched:
            signal, strength = "SWING TRIM", max(strength, 65)
            bits = []
            if ema_stretched:
                bits.append(f"+{ema_stretch_pct:.0f}% above EMA-200")
            if surge_stretched:
                bits.append(f"+{surge_30d_pct:.0f}% in 30d")
            stretch_bits = " & ".join(bits)
            target_note = f" Still well short of target (₹{target:,.2f}), so this is a partial-profit/rebuy-lower play, not an exit." if target else ""
            reason = f"{below_high_pct:.1f}% below its {swing_window}d high (≤{near_high_threshold_pct:.1f}% band), live & closed RSI extreme ({live_rsi:.1f} / {closed_rsi:.1f} ≥{rsi_overbought_threshold:.0f}), and stretched ({stretch_bits}) — extreme spike with room to buy back lower.{target_note}"
        elif near_high and rsi_live_overbought and not rsi_closed_overbought:
            closed_txt = "—" if closed_rsi is None else f"{closed_rsi:.2f}"
            reason = f"{below_high_pct:.1f}% below its {swing_window}d high and live RSI {live_rsi:.2f} is overbought (≥{rsi_overbought_threshold:.0f}), but the last daily close (RSI {closed_txt}) hasn't confirmed it yet — waiting for a daily close."
        elif near_high and rsi_overbought and not parabolic_stretched:
            reason = f"{below_high_pct:.1f}% below its {swing_window}d high with extreme RSI ({live_rsi:.1f}), but not parabolic enough yet (needs ≥+{SWING_TRIM_EMA_STRETCH_PCT}% above EMA-200 or ≥+{SWING_TRIM_SURGE_30D_PCT}% in 30d) — holding to avoid trimming early."
        elif lean == "swing" and ((add_reason_shown and signal == "WAIT") or (ath_pct is not None and ath_pct > ath_tiers["watch"])):
            lead = f"{below_high_pct:.1f}% below its {swing_window}d high"
            if near_high:
                reason = f"{lead} (within its own ≤{near_high_threshold_pct:.1f}% band), but RSI ({rsi_txt}) hasn't reached its own overbought zone (≥{rsi_overbought_threshold:.0f}) yet."
            else:
                reason = f"{lead} — outside its own ≤{near_high_threshold_pct:.1f}% near-high band, and RSI ({rsi_txt}) is below its own overbought zone (≥{rsi_overbought_threshold:.0f})."
        elif near_high:
            reason = f"{reason} Also {below_high_pct:.1f}% below its {swing_window}d high, but RSI ({rsi_txt}) hasn't reached its own overbought zone (≥{rsi_overbought_threshold:.0f}) yet."

    if target and cur is not None and cur >= target * (1 - TARGET_PROXIMITY_PCT / 100) and signal not in ("EXIT", "AT RISK"):
        signal = "TRIM"
        strength = 80 if cur >= target else 70
        reason = f"Price is at or within {TARGET_PROXIMITY_PCT}% of target (₹{target:,.2f})."

    return {
        "signal": signal, "strength": strength, "reason": reason, "cmp": cur,
        "athPct": ath_pct, "rsi": live_rsi, "sentiment": sentiment,
        "relStrengthVsBtc": rel_strength, "dcaStreakDay": dca_streak_day,
        "dcaGapPct": dca_gap_pct,
        "dcaRequiredGapPct": dca_required_gap_pct,
        "nextDcaPrice": next_dca_price,
        "rsiOversoldThreshold": (ta or {}).get("rsiOversoldThreshold", RSI_OVERSOLD),
        "rsiOverboughtThreshold": (ta or {}).get("rsiOverboughtThreshold", RSI_OVERBOUGHT),
        "nearLowThresholdPct": (ta or {}).get("nearLowThresholdPct", DCA_DIP_NEAR_PCT),
        "lean": lean, "gateLow": gate_low, "gateLowWindow": gate_window,
        "gateHigh": gate_high, "gateHighWindow": gate_high_window,
        "high365": yr["high"] if yr else None, "low365": yr["low"] if yr else None,
        "drawdown365": yr["pct"] if yr else None, "range365Pos": yr["rangePos"] if yr else None,
    }

# ── TA refresh (once/day, boundary-aligned like taBoundaryPassedSince) ──
def ta_boundary_passed_since(ts_ms):
    if not ts_ms:
        return True
    now = datetime.now(timezone.utc)
    boundary = now.replace(hour=0, minute=TA_ALIGN_BUFFER_MIN, second=0, microsecond=0)
    if now < boundary:
        boundary -= timedelta(days=1)
    return ts_ms / 1000 < boundary.timestamp()

def refresh_ta(state, ids):
    ta_cache = state.setdefault("ta_cache", {})
    now_ms = time.time() * 1000

    recomputed = 0
    for cid in ids:
        cached = ta_cache.get(cid)
        closes = cached.get("closes") if cached else None
        if cached and cached.get("schemaVersion") != TA_SCHEMA_VERSION and closes and len(closes) >= TA_HISTORY_DAYS - 20:
            rec = compute_ta_from_closes(closes)
            if rec and audit_ta(rec):
                if cached.get("year"):
                    rec["year"] = cached["year"]
                rec["closesEndTs"] = cached.get("closesEndTs")
                rec["timestamp"] = cached.get("timestamp")
                if cached.get("rollingExtremesSource") in ("ohlc", "hourly"):
                    for k in ("rollingHighSrc", "rollingLowSrc", "rollingHigh", "rollingLow",
                              "rollingHighDaysAgo", "rollingLowDaysAgo", "rollingExtremesSource"):
                        rec[k] = cached.get(k)
                ta_cache[cid] = rec
                recomputed += 1
    if recomputed:
        print(f"Re-applied updated signal logic to {recomputed} cached coin(s).")

    def needs_refresh(cid):
        c = ta_cache.get(cid)
        if c is None or ta_boundary_passed_since(c.get("timestamp")) or c.get("schemaVersion") != TA_SCHEMA_VERSION:
            return True
        end = c.get("closesEndTs")
        return bool(end and now_ms - end > TA_MAX_DATA_AGE_MS and now_ms - (c.get("timestamp") or 0) > 6 * 3600 * 1000)

    stale = [cid for cid in ids if needs_refresh(cid)]
    updated = stale_feeds = audit_failures = 0
    for cid in stale:
        res = fetch_ta_history(cid)
        if not res and _LAST_HTTP_ERR == "HTTP_ERROR:429":
            print(f"Rate limited fetching {cid} — pausing 15s...")
            time.sleep(15)
            res = fetch_ta_history(cid)
        if res:
            closes, last_ts = res
            if last_ts is None or now_ms - last_ts > TA_MAX_DATA_AGE_MS:
                stale_feeds += 1
                end = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if last_ts else "unknown"
                print(f"Daily history for {cid} ends {end} — too old, discarded.")
                time.sleep(TA_FETCH_DELAY_S)
                continue
            ta = compute_ta_from_closes(closes)
            if ta and audit_ta(ta):
                ta["closesEndTs"] = last_ts
                time.sleep(0.5)
                ext = fetch_range_extremes(cid)
                apply_range_extremes(ta, closes, ext)
                ta_cache[cid] = ta
                updated += 1
            else:
                audit_failures += 1
                print(f"TA audit/history check failed for {cid} — keeping previous values.")
        time.sleep(TA_FETCH_DELAY_S)
    if stale_feeds:
        print(f"{stale_feeds} daily histories discarded as stale (ended >3d ago).")
    if audit_failures:
        print(f"{audit_failures} TA updates discarded on audit failure.")
    return updated

# ── Formatting ────────────────────────────────────────────────────
SIGNAL_EMOJI = {
    "ACCUMULATE": "\U0001F7E2", "WAIT": "\u26AA", "HOLD": "\U0001F535",
    "TRIM": "\U0001F7E1", "SWING TRIM": "\U0001F7E3", "EXIT": "\U0001F534",
    "AT RISK": "\U0001F7E0", "NA": "\u26AA",
}

def fmt_inr(n):
    if n is None:
        return "—"
    return f"₹{n:,.2f}" if abs(n) < 1000 else f"₹{n:,.0f}"

def digest_message(results):
    lines = ["📊 <b>Portfolio Signal Digest</b>\n"]
    for cid, r in results.items():
        sym = PORTFOLIO[cid]["symbol"]
        emoji = SIGNAL_EMOJI.get(r["signal"], "⚪")
        lines.append(f"{emoji} <b>{sym}</b> {fmt_inr(r['cmp'])} — {r['signal']} ({r['strength']}%)")
    return "\n".join(lines)

def change_message(cid, prev, new):
    sym = PORTFOLIO[cid]["symbol"]
    emoji = SIGNAL_EMOJI.get(new["signal"], "⚪")
    return (
        f"{emoji} <b>{sym} → {new['signal']}</b> ({new['strength']}%)\n\n"
        f"Was: {prev or 'N/A'}\n"
        f"CMP: {fmt_inr(new['cmp'])}\n\n"
        f"{html.escape(new['reason'])}"
    )

def candidate_discovery_message(candidates):
    lines = [f"🔎 <b>New Add Candidates (Top {DISCOVERY_MARKET_LIMIT})</b>", "Outside your portfolio · matching ACCUMULATE\n"]
    for coin in candidates:
        signal = coin["result"]["signal"]
        emoji = SIGNAL_EMOJI.get(signal, "⚪")
        symbol = html.escape(coin["symbol"] or coin["id"].upper())
        name = html.escape(coin["name"])
        rank = coin["rank"] if coin["rank"] is not None else "—"
        result = coin["result"]
        streak = f" · D{result['dcaStreakDay']}" if result.get("dcaStreakDay") else ""
        lines.append(
            f"{emoji} <b>{symbol}</b> · {name} · #{rank}\n"
            f"{fmt_inr(result['cmp'])} — <b>ADD {result['strength']}%{streak}</b>\n"
            f"{html.escape(result['reason'])}\n"
        )
    return "\n".join(lines)

# ── Candidate discovery (1:1 with Portfolio_Tracker.html) ─────────
def run_candidate_discovery(state, macro_ta):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("last_discovery") == today:
        return False

    print(f"Starting daily top-{DISCOVERY_MARKET_LIMIT} market candidate discovery...")
    universe = fetch_market_universe()
    if universe is None:
        print("Candidate discovery skipped; market-universe fetch failed.")
        return False

    portfolio_ids = set(PORTFOLIO)
    now_ms = time.time() * 1000

    def pct_above_24h_low(md):
        lo, cur = md.get("low_24h"), md.get("current_price")
        if lo is not None and cur is not None and lo > 0 and cur > 0:
            return (cur - lo) / lo * 100
        return None

    shortlist = []
    for cid, md in universe.items():
        if cid in portfolio_ids:
            continue
        ath_change = md.get("ath_change_percentage")
        if ath_change is None or ath_change > DCA_MODERATE_PCT * VOL_SCALE_MIN:
            continue
        above_24h_low = pct_above_24h_low(md)
        if above_24h_low is not None and above_24h_low > DCA_NEAR_LOW_CEIL_PCT:
            continue
        volume, mcap, rank = md.get("total_volume"), md.get("market_cap"), md.get("market_cap_rank")
        if volume is None or mcap is None or mcap == 0 or volume == 0:
            continue
        if mcap < MICROCAP_FLOOR and volume < ABSOLUTE_DEAD_VOLUME:
            continue
        if (volume / mcap) < LIQUIDITY_DEAD_RATIO:
            continue
        if mcap < MICROCAP_FLOOR and (rank is None or rank > RANK_CAUTION):
            continue
        sort_key = (
            above_24h_low if above_24h_low is not None else float("inf"),
            md.get("change_30d") if md.get("change_30d") is not None else 0,
        )
        shortlist.append((sort_key, rank, cid, md))

    shortlist.sort(key=lambda row: row[0])
    shortlist = shortlist[:DISCOVERY_TA_LIMIT]
    if not shortlist:
        state["last_discovery"] = today
        state["discovery_candidates"] = {}
        print("Candidate discovery complete; no coins cleared Stage 1 prefilter.")
        return True

    seen = state.setdefault("discovery_candidates", {})
    newly_qualified = []

    for _, rank, cid, md in shortlist:
        res = fetch_ta_history(cid)
        if not res and _LAST_HTTP_ERR == "HTTP_ERROR:429":
            print(f"Rate limited checking {cid} — pausing 15s...")
            time.sleep(15)
            res = fetch_ta_history(cid)
        if res:
            closes, last_ts = res
            if last_ts is not None and (now_ms - last_ts) <= TA_MAX_DATA_AGE_MS:
                ta = compute_ta_from_closes(closes)
                if ta and audit_ta(ta) and ta.get("rollingLow") is not None:
                    ta["closesEndTs"] = last_ts
                    item = {"symbol": md.get("symbol") or cid.upper(), "target": None}
                    sig = classify_holding(cid, md, item, ta, macro_ta, None)

                    if sig.get("signal") == "ACCUMULATE":
                        time.sleep(1.5)
                        ext = fetch_range_extremes(cid)
                        if not ext and _LAST_HTTP_ERR == "HTTP_ERROR:429":
                            print(f"Rate limited during wick check for {cid} — pausing 15s...")
                            time.sleep(15)
                            ext = fetch_range_extremes(cid)
                        apply_range_extremes(ta, closes, ext)
                        sig = classify_holding(cid, md, item, ta, macro_ta, None)
                        time.sleep(2.5)

                    if sig.get("signal") in DISCOVERY_REPORT_SIGNALS:
                        prev_sig = (seen.get(cid) or {}).get("signal")
                        if sig["signal"] != prev_sig:
                            newly_qualified.append({
                                "id": cid, "symbol": item["symbol"],
                                "name": md.get("name") or cid, "rank": rank, "result": sig,
                            })
                        seen[cid] = {"signal": sig["signal"], "lastSeen": today}
        time.sleep(DISCOVERY_DELAY_S)

    for cid in [k for k, v in seen.items() if (v or {}).get("lastSeen") != today]:
        del seen[cid]

    state["last_discovery"] = today
    if newly_qualified:
        newly_qualified.sort(key=lambda c: (-(c["result"]["strength"]), c["result"].get("athPct") or 0))
        send_telegram(candidate_discovery_message(newly_qualified))
        print(f"Candidate discovery reported {len(newly_qualified)} new ACCUMULATE candidate(s).")
    else:
        print("Candidate discovery complete; no new ACCUMULATE candidates.")
    return True

# ── Main ──────────────────────────────────────────────────────────
def main():
    state = load_state()
    ids = list(PORTFOLIO.keys())
    all_ids = list(dict.fromkeys(ids + [MACRO_ID]))
    keep_ids = set(all_ids)

    ta_cache = state.setdefault("ta_cache", {})
    for cid in [k for k in list(ta_cache.keys()) if k not in keep_ids]:
        del ta_cache[cid]

    print("Fetching market data...")
    market = fetch_market_data(all_ids)
    if market is None:
        send_telegram("⚠️ Tracker bot: market data fetch failed.")
        return

    print("Refreshing TA (if stale)...")
    refresh_ta(state, all_ids)
    save_state(state)

    ta_cache = state.get("ta_cache", {})
    macro_ta = ta_cache.get(MACRO_ID)
    dca_log = state.get("dca_log", {})

    results = {}
    for cid in ids:
        results[cid] = classify_holding(cid, market.get(cid), PORTFOLIO[cid], ta_cache.get(cid), macro_ta, dca_log)

    prev_signals = state.get("signals", {})
    changes = []
    for cid, r in results.items():
        prev = prev_signals.get(cid)
        if prev != r["signal"]:
            changes.append((cid, prev, r))

    for cid, prev, r in changes:
        send_telegram(change_message(cid, prev, r))
        print(f"Signal change: {PORTFOLIO[cid]['symbol']} {prev} -> {r['signal']}")

    state["signals"] = {cid: r["signal"] for cid, r in results.items()}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hour = datetime.now(timezone.utc).hour
    if state.get("last_daily") != today and 3 <= hour < 5:
        send_telegram(digest_message(results))
        state["last_daily"] = today
        print("Daily digest sent.")

    run_candidate_discovery(state, macro_ta)

    save_state(state)
    print("Done.")

if __name__ == "__main__":
    main()
