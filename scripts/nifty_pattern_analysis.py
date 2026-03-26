"""
NIFTY 50 Pattern Analysis — 2025-2026
Comprehensive statistical analysis of 1-minute candle data to find
profitable intraday patterns for a NIFTY index strategy.

Patterns analyzed:
1. Gap up/down -> first 15/30/60 min direction
2. Opening Range Breakout (5m, 10m, 15m)
3. Time-of-day momentum (which minutes are best)
4. Previous day levels (PDH/PDL/PDC touch/break)
5. First N-bar momentum continuation
6. Day-of-week effects
7. VWAP cross patterns
8. Candle body/wick patterns at open
9. Mean reversion from VWAP
10. Multi-timeframe trend alignment (5m + 15m EMA)
"""

import json
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict
import math

DATA_DIR = Path("C:/Users/BT-25/Desktop/project/resolute/data/NIFTY_50")
IST_OFFSET = 19800  # 5h30m in seconds

# ── Data loading ─────────────────────────────────────────────────────────────

def load_day(filepath):
    with open(filepath) as f:
        d = json.load(f)
    n = len(d["open"])
    bars = []
    for i in range(n):
        ts = d["timestamp"][i]
        ist = datetime.utcfromtimestamp(ts + IST_OFFSET)
        bars.append({
            "o": d["open"][i],
            "h": d["high"][i],
            "l": d["low"][i],
            "c": d["close"][i],
            "v": d["volume"][i] if "volume" in d else 0,
            "ts": ts,
            "ist": ist,
            "minute": ist.hour * 60 + ist.minute,  # minute of day
        })
    return bars

def load_all_days():
    """Load all 2025-2026 1m data files, return list of (date_str, bars)."""
    days = []
    for f in sorted(DATA_DIR.glob("2025-*_1m.json")) + sorted(DATA_DIR.glob("2026-*_1m.json")):
        date_str = f.stem.replace("_1m", "")
        try:
            bars = load_day(f)
            if len(bars) > 100:  # skip partial days
                days.append((date_str, bars))
        except Exception:
            pass
    return days

# ── Helpers ──────────────────────────────────────────────────────────────────

def ema(values, period):
    """EMA of a list of floats."""
    if not values or period < 1:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def vwap_series(bars):
    """Running VWAP from bar list. Uses typical_price * volume."""
    cum_pv = 0.0
    cum_v = 0.0
    result = []
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        vol = max(b["v"], 1)  # avoid div by 0
        cum_pv += tp * vol
        cum_v += vol
        result.append(cum_pv / cum_v if cum_v > 0 else tp)
    return result

def minute_index(bars, target_minute):
    """Find index of first bar at or after target_minute."""
    for i, b in enumerate(bars):
        if b["minute"] >= target_minute:
            return i
    return None

def bars_in_range(bars, start_min, end_min):
    """Return bars within minute range [start_min, end_min)."""
    return [b for b in bars if start_min <= b["minute"] < end_min]

# ── Pattern 1: Gap Analysis ─────────────────────────────────────────────────

def analyze_gaps(days):
    print("\n" + "="*80)
    print("PATTERN 1: GAP UP/DOWN ANALYSIS")
    print("="*80)

    results = {"gap_up_big": [], "gap_up_small": [], "gap_down_big": [], "gap_down_small": [], "flat": []}

    prev_close = None
    for date_str, bars in days:
        if not bars:
            continue
        day_open = bars[0]["o"]
        day_close = bars[-1]["c"]

        if prev_close is not None and prev_close > 0:
            gap_pct = (day_open - prev_close) / prev_close * 100

            # What happens after gap?
            # Measure: return from open to various times
            returns = {}
            for mins_after, label in [(15, "15m"), (30, "30m"), (60, "60m"), (120, "2h"), (375, "EOD")]:
                target_min = 9 * 60 + 15 + mins_after
                idx = minute_index(bars, target_min)
                if idx and idx < len(bars):
                    ret = (bars[idx]["c"] - day_open) / day_open * 100
                    returns[label] = ret

            if gap_pct > 0.3:
                results["gap_up_big"].append((gap_pct, returns))
            elif gap_pct > 0:
                results["gap_up_small"].append((gap_pct, returns))
            elif gap_pct < -0.3:
                results["gap_down_big"].append((gap_pct, returns))
            elif gap_pct < 0:
                results["gap_down_small"].append((gap_pct, returns))
            else:
                results["flat"].append((gap_pct, returns))

        prev_close = day_close

    for category, data in results.items():
        if not data:
            continue
        print(f"\n  {category.upper()} ({len(data)} days):")
        print(f"    Avg gap: {sum(d[0] for d in data)/len(data):.3f}%")
        for label in ["15m", "30m", "60m", "2h", "EOD"]:
            rets = [d[1].get(label, 0) for d in data if label in d[1]]
            if rets:
                avg = sum(rets) / len(rets)
                win = sum(1 for r in rets if r > 0) / len(rets) * 100
                print(f"    After {label}: avg={avg:+.3f}%  win={win:.1f}%  n={len(rets)}")

# ── Pattern 2: Opening Range Breakout ────────────────────────────────────────

def analyze_orb(days):
    print("\n" + "="*80)
    print("PATTERN 2: OPENING RANGE BREAKOUT (5m, 10m, 15m)")
    print("="*80)

    for orb_minutes in [5, 10, 15]:
        buy_trades = []
        sell_trades = []

        for date_str, bars in days:
            market_open = 9 * 60 + 15
            orb_end = market_open + orb_minutes

            orb_bars = bars_in_range(bars, market_open, orb_end)
            if len(orb_bars) < orb_minutes - 1:
                continue

            orb_high = max(b["h"] for b in orb_bars)
            orb_low = min(b["l"] for b in orb_bars)
            orb_range = orb_high - orb_low

            if orb_range < 10:  # skip tiny ranges
                continue

            # Check for breakout in next 2 hours
            post_orb = bars_in_range(bars, orb_end, orb_end + 120)

            breakout_dir = None
            breakout_price = None
            breakout_idx = None

            for i, b in enumerate(post_orb):
                if b["h"] > orb_high and breakout_dir is None:
                    breakout_dir = "BUY"
                    breakout_price = orb_high
                    breakout_idx = i
                    break
                elif b["l"] < orb_low and breakout_dir is None:
                    breakout_dir = "SELL"
                    breakout_price = orb_low
                    breakout_idx = i
                    break

            if breakout_dir is None:
                continue

            # Measure outcome: max favorable, max adverse, close at +30m, +60m, EOD
            remaining = post_orb[breakout_idx:]
            if not remaining:
                continue

            entry = breakout_price
            if breakout_dir == "BUY":
                max_fav = max(b["h"] for b in remaining) - entry
                max_adv = entry - min(b["l"] for b in remaining)
                # Exit at various times
                for target_bars, label in [(30, "30m"), (60, "60m")]:
                    if breakout_idx + target_bars < len(post_orb):
                        exit_price = post_orb[breakout_idx + target_bars]["c"]
                        ret_pct = (exit_price - entry) / entry * 100
                        buy_trades.append((label, ret_pct, orb_range, date_str))
                # EOD
                eod_price = bars[-1]["c"]
                eod_ret = (eod_price - entry) / entry * 100
                buy_trades.append(("EOD", eod_ret, orb_range, date_str))
            else:
                max_fav = entry - min(b["l"] for b in remaining)
                max_adv = max(b["h"] for b in remaining) - entry
                for target_bars, label in [(30, "30m"), (60, "60m")]:
                    if breakout_idx + target_bars < len(post_orb):
                        exit_price = post_orb[breakout_idx + target_bars]["c"]
                        ret_pct = (entry - exit_price) / entry * 100
                        sell_trades.append((label, ret_pct, orb_range, date_str))
                eod_price = bars[-1]["c"]
                eod_ret = (entry - eod_price) / entry * 100
                sell_trades.append(("EOD", eod_ret, orb_range, date_str))

        print(f"\n  ORB-{orb_minutes}m:")
        for dir_name, trades in [("BUY breakout", buy_trades), ("SELL breakout", sell_trades)]:
            for label in ["30m", "60m", "EOD"]:
                t = [r for l, r, _, _ in trades if l == label]
                if t:
                    avg = sum(t) / len(t)
                    win = sum(1 for r in t if r > 0) / len(t) * 100
                    print(f"    {dir_name} -> {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Pattern 3: Time-of-Day Momentum ─────────────────────────────────────────

def analyze_time_of_day(days):
    print("\n" + "="*80)
    print("PATTERN 3: TIME-OF-DAY RETURNS (per 15-minute slot)")
    print("="*80)

    slots = defaultdict(list)

    for date_str, bars in days:
        for i in range(len(bars) - 15):
            slot_start = bars[i]["minute"]
            # Only look at 15-min block starts
            if slot_start % 15 != 0 and slot_start != 9 * 60 + 15:
                continue

            block = bars[i:i+15]
            if len(block) < 15:
                continue

            ret = (block[-1]["c"] - block[0]["o"]) / block[0]["o"] * 100
            slot_label = f"{slot_start // 60:02d}:{slot_start % 60:02d}"
            slots[slot_label].append(ret)

    print(f"\n  {'Slot':<8} {'Avg':>8} {'Std':>8} {'Win%':>6} {'BuyEdge':>8} {'N':>5}")
    print(f"  {'-'*45}")
    for slot in sorted(slots.keys()):
        rets = slots[slot]
        avg = sum(rets) / len(rets)
        std = (sum((r - avg)**2 for r in rets) / len(rets)) ** 0.5
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        sharpe = avg / std if std > 0 else 0
        print(f"  {slot:<8} {avg:>+8.4f} {std:>8.4f} {win:>5.1f}% {sharpe:>+8.3f} {len(rets):>5}")

# ── Pattern 4: Previous Day High/Low/Close ──────────────────────────────────

def analyze_prev_day(days):
    print("\n" + "="*80)
    print("PATTERN 4: PREVIOUS DAY HIGH/LOW/CLOSE LEVELS")
    print("="*80)

    pdh_break_buy = []
    pdl_break_sell = []
    pdc_bounce_buy = []
    pdc_bounce_sell = []

    prev_high = prev_low = prev_close = None

    for date_str, bars in days:
        if not bars:
            continue

        day_high = max(b["h"] for b in bars)
        day_low = min(b["l"] for b in bars)
        day_close = bars[-1]["c"]
        day_open = bars[0]["o"]

        if prev_high is not None:
            # Check: does price break PDH -> BUY signal?
            for i, b in enumerate(bars):
                if b["h"] > prev_high:
                    entry = prev_high
                    # Return at +30m, +60m, EOD
                    for offset, label in [(30, "30m"), (60, "60m")]:
                        if i + offset < len(bars):
                            ret = (bars[i + offset]["c"] - entry) / entry * 100
                            pdh_break_buy.append((label, ret, date_str))
                    eod_ret = (bars[-1]["c"] - entry) / entry * 100
                    pdh_break_buy.append(("EOD", eod_ret, date_str))
                    break

            # Check: does price break PDL -> SELL signal?
            for i, b in enumerate(bars):
                if b["l"] < prev_low:
                    entry = prev_low
                    for offset, label in [(30, "30m"), (60, "60m")]:
                        if i + offset < len(bars):
                            ret = (entry - bars[i + offset]["c"]) / entry * 100
                            pdl_break_sell.append((label, ret, date_str))
                    eod_ret = (entry - bars[-1]["c"]) / entry * 100
                    pdl_break_sell.append(("EOD", eod_ret, date_str))
                    break

            # PDC bounce: price touches prev_close zone (within 0.05%) and bounces
            for i in range(1, min(60, len(bars))):
                dist = abs(bars[i]["l"] - prev_close) / prev_close * 100
                if dist < 0.05 and bars[i]["c"] > bars[i]["o"]:  # bullish bounce
                    entry = bars[i]["c"]
                    for offset, label in [(15, "15m"), (30, "30m")]:
                        if i + offset < len(bars):
                            ret = (bars[i + offset]["c"] - entry) / entry * 100
                            pdc_bounce_buy.append((label, ret, date_str))
                    break

        prev_high = day_high
        prev_low = day_low
        prev_close = day_close

    for name, trades in [("PDH Break -> BUY", pdh_break_buy), ("PDL Break -> SELL", pdl_break_sell), ("PDC Bounce -> BUY", pdc_bounce_buy)]:
        print(f"\n  {name}:")
        for label in ["15m", "30m", "60m", "EOD"]:
            t = [r for l, r, _ in trades if l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                print(f"    Exit {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Pattern 5: First N-Bar Momentum ─────────────────────────────────────────

def analyze_first_n_bars(days):
    print("\n" + "="*80)
    print("PATTERN 5: FIRST N-BAR MOMENTUM CONTINUATION")
    print("="*80)

    for n_bars in [3, 5, 10, 15]:
        continuation = []
        reversal = []

        for date_str, bars in days:
            if len(bars) < n_bars + 60:
                continue

            first_n = bars[:n_bars]
            first_ret = (first_n[-1]["c"] - first_n[0]["o"]) / first_n[0]["o"] * 100

            if abs(first_ret) < 0.05:  # skip flat opens
                continue

            direction = "BUY" if first_ret > 0 else "SELL"
            entry = first_n[-1]["c"]

            # Measure continuation at +15m, +30m, +60m, EOD
            for offset, label in [(15, "15m"), (30, "30m"), (60, "60m")]:
                idx = n_bars + offset
                if idx < len(bars):
                    if direction == "BUY":
                        ret = (bars[idx]["c"] - entry) / entry * 100
                    else:
                        ret = (entry - bars[idx]["c"]) / entry * 100
                    continuation.append((label, ret, abs(first_ret), direction, date_str))

            # EOD
            if direction == "BUY":
                eod_ret = (bars[-1]["c"] - entry) / entry * 100
            else:
                eod_ret = (entry - bars[-1]["c"]) / entry * 100
            continuation.append(("EOD", eod_ret, abs(first_ret), direction, date_str))

        print(f"\n  First {n_bars} bars momentum ({len([c for c in continuation if c[0]=='EOD'])} days):")
        for label in ["15m", "30m", "60m", "EOD"]:
            t = [r for l, r, _, _, _ in continuation if l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                # Split by strong vs weak first move
                strong = [r for l, r, m, _, _ in continuation if l == label and m > 0.2]
                weak = [r for l, r, m, _, _ in continuation if l == label and m <= 0.2]
                s_avg = sum(strong)/len(strong) if strong else 0
                s_win = sum(1 for r in strong if r > 0)/len(strong)*100 if strong else 0
                print(f"    {label}: avg={avg:+.4f}% win={win:.1f}% | strong(>{0.2}%): avg={s_avg:+.4f}% win={s_win:.1f}% n={len(strong)}")

# ── Pattern 6: Day-of-Week ──────────────────────────────────────────────────

def analyze_day_of_week(days):
    print("\n" + "="*80)
    print("PATTERN 6: DAY-OF-WEEK EFFECTS")
    print("="*80)

    dow_returns = defaultdict(list)
    dow_names = {0: "Monday", 1: "Tuesday", 2: "Wednesday", 3: "Thursday", 4: "Friday"}

    for date_str, bars in days:
        if not bars:
            continue
        d = date.fromisoformat(date_str)
        dow = d.weekday()
        day_ret = (bars[-1]["c"] - bars[0]["o"]) / bars[0]["o"] * 100

        # Also measure: first hour return, last hour return
        first_hour = bars_in_range(bars, 9*60+15, 10*60+15)
        last_hour = bars_in_range(bars, 14*60+30, 15*60+30)

        fh_ret = (first_hour[-1]["c"] - first_hour[0]["o"]) / first_hour[0]["o"] * 100 if first_hour else 0
        lh_ret = (last_hour[-1]["c"] - last_hour[0]["o"]) / last_hour[0]["o"] * 100 if last_hour else 0

        dow_returns[dow].append({"full": day_ret, "first_hour": fh_ret, "last_hour": lh_ret})

    print(f"\n  {'Day':<12} {'Full Day':>10} {'Win%':>6} {'1st Hour':>10} {'Win%':>6} {'Last Hour':>10} {'Win%':>6} {'N':>4}")
    print(f"  {'-'*70}")
    for dow in range(5):
        data = dow_returns[dow]
        if not data:
            continue
        full = [d["full"] for d in data]
        fh = [d["first_hour"] for d in data]
        lh = [d["last_hour"] for d in data]
        print(f"  {dow_names[dow]:<12} {sum(full)/len(full):>+10.4f} {sum(1 for r in full if r>0)/len(full)*100:>5.1f}% "
              f"{sum(fh)/len(fh):>+10.4f} {sum(1 for r in fh if r>0)/len(fh)*100:>5.1f}% "
              f"{sum(lh)/len(lh):>+10.4f} {sum(1 for r in lh if r>0)/len(lh)*100:>5.1f}% "
              f"{len(data):>4}")

# ── Pattern 7: VWAP Cross ───────────────────────────────────────────────────

def analyze_vwap(days):
    print("\n" + "="*80)
    print("PATTERN 7: VWAP CROSS PATTERNS")
    print("="*80)

    cross_above_buy = []
    cross_below_sell = []

    for date_str, bars in days:
        if len(bars) < 30:
            continue

        vw = vwap_series(bars)

        # Find first VWAP cross after 9:30 (15 bars in)
        for i in range(16, min(120, len(bars))):
            prev_above = bars[i-1]["c"] > vw[i-1]
            curr_above = bars[i]["c"] > vw[i]

            if not prev_above and curr_above:  # cross above -> BUY
                entry = bars[i]["c"]
                for offset, label in [(10, "10m"), (20, "20m"), (30, "30m"), (60, "60m")]:
                    if i + offset < len(bars):
                        ret = (bars[i+offset]["c"] - entry) / entry * 100
                        cross_above_buy.append((label, ret, date_str))
                break
            elif prev_above and not curr_above:  # cross below -> SELL
                entry = bars[i]["c"]
                for offset, label in [(10, "10m"), (20, "20m"), (30, "30m"), (60, "60m")]:
                    if i + offset < len(bars):
                        ret = (entry - bars[i+offset]["c"]) / entry * 100
                        cross_below_sell.append((label, ret, date_str))
                break

    for name, trades in [("VWAP Cross Above -> BUY", cross_above_buy), ("VWAP Cross Below -> SELL", cross_below_sell)]:
        print(f"\n  {name}:")
        for label in ["10m", "20m", "30m", "60m"]:
            t = [r for l, r, _ in trades if l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                print(f"    Exit {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Pattern 8: Opening Candle Body/Wick ──────────────────────────────────────

def analyze_opening_candle(days):
    print("\n" + "="*80)
    print("PATTERN 8: FIRST 5-MIN CANDLE BODY/WICK ANALYSIS")
    print("="*80)

    big_bull = []   # large bullish 5m candle at open
    big_bear = []   # large bearish 5m candle at open
    doji = []       # small body, big wick

    for date_str, bars in days:
        first5 = bars[:5]
        if len(first5) < 5:
            continue

        o = first5[0]["o"]
        c = first5[-1]["c"]
        h = max(b["h"] for b in first5)
        l = min(b["l"] for b in first5)

        body = abs(c - o)
        range_ = h - l
        body_ratio = body / range_ if range_ > 0 else 0
        ret_5m = (c - o) / o * 100

        # Measure what happens next (bars 5 to 65)
        if len(bars) < 65:
            continue

        outcomes = {}
        for offset, label in [(15, "15m"), (30, "30m"), (60, "60m")]:
            idx = 5 + offset
            if idx < len(bars):
                outcomes[label] = (bars[idx]["c"] - c) / c * 100
        outcomes["EOD"] = (bars[-1]["c"] - c) / c * 100

        if ret_5m > 0.15 and body_ratio > 0.6:  # strong bullish
            big_bull.append((ret_5m, outcomes, date_str))
        elif ret_5m < -0.15 and body_ratio > 0.6:  # strong bearish
            big_bear.append((ret_5m, outcomes, date_str))
        elif body_ratio < 0.3:  # doji
            doji.append((ret_5m, outcomes, date_str))

    for name, trades in [("STRONG BULLISH 5m open (body>60%, ret>0.15%)", big_bull),
                          ("STRONG BEARISH 5m open (body>60%, ret<-0.15%)", big_bear),
                          ("DOJI 5m open (body<30%)", doji)]:
        print(f"\n  {name}: ({len(trades)} days)")
        if not trades:
            continue
        for label in ["15m", "30m", "60m", "EOD"]:
            rets = [t[1].get(label, 0) for t in trades if label in t[1]]
            if rets:
                avg = sum(rets) / len(rets)
                # For bearish candle, continuation = further down (negative ret is continuation)
                if "BEARISH" in name:
                    win = sum(1 for r in rets if r < 0) / len(rets) * 100
                    print(f"    Continuation {label}: avg={avg:+.4f}%  cont%={win:.1f}%  n={len(rets)}")
                elif "BULLISH" in name:
                    win = sum(1 for r in rets if r > 0) / len(rets) * 100
                    print(f"    Continuation {label}: avg={avg:+.4f}%  cont%={win:.1f}%  n={len(rets)}")
                else:
                    buy_win = sum(1 for r in rets if r > 0) / len(rets) * 100
                    print(f"    After doji {label}: avg={avg:+.4f}%  buy_win={buy_win:.1f}%  n={len(rets)}")

# ── Pattern 9: Mean Reversion from VWAP ─────────────────────────────────────

def analyze_vwap_reversion(days):
    print("\n" + "="*80)
    print("PATTERN 9: MEAN REVERSION — DISTANCE FROM VWAP")
    print("="*80)

    trades = []

    for date_str, bars in days:
        if len(bars) < 60:
            continue

        vw = vwap_series(bars)

        # After first 30 bars (9:45), look for price far from VWAP
        for i in range(30, min(300, len(bars))):
            dist_pct = (bars[i]["c"] - vw[i]) / vw[i] * 100

            # If price is >0.3% above VWAP -> SELL (mean reversion)
            if dist_pct > 0.3:
                entry = bars[i]["c"]
                for offset, label in [(5, "5m"), (10, "10m"), (15, "15m"), (30, "30m")]:
                    if i + offset < len(bars):
                        ret = (entry - bars[i+offset]["c"]) / entry * 100  # SELL
                        trades.append(("SELL", label, ret, dist_pct, date_str))

            # If price is >0.3% below VWAP -> BUY (mean reversion)
            elif dist_pct < -0.3:
                entry = bars[i]["c"]
                for offset, label in [(5, "5m"), (10, "10m"), (15, "15m"), (30, "30m")]:
                    if i + offset < len(bars):
                        ret = (bars[i+offset]["c"] - entry) / entry * 100  # BUY
                        trades.append(("BUY", label, ret, dist_pct, date_str))

    for direction in ["BUY", "SELL"]:
        print(f"\n  VWAP Reversion {direction} (price {'below' if direction=='BUY' else 'above'} VWAP by >0.3%):")
        for label in ["5m", "10m", "15m", "30m"]:
            t = [r for d, l, r, _, _ in trades if d == direction and l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                print(f"    Exit {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Pattern 10: EMA Trend Alignment ─────────────────────────────────────────

def analyze_ema_alignment(days):
    print("\n" + "="*80)
    print("PATTERN 10: EMA TREND ALIGNMENT (9/21 on 5m bars)")
    print("="*80)

    trades = []

    for date_str, bars in days:
        if len(bars) < 120:
            continue

        # Build 5m candles
        closes_5m = []
        for i in range(0, len(bars) - 4, 5):
            chunk = bars[i:i+5]
            closes_5m.append(chunk[-1]["c"])

        if len(closes_5m) < 25:
            continue

        ema9 = ema([c for c in closes_5m], 9)
        ema21 = ema([c for c in closes_5m], 21)

        # Find EMA crossovers
        for i in range(22, len(ema9)):
            if i >= len(ema21):
                break

            prev_bull = ema9[i-1] > ema21[i-1]
            curr_bull = ema9[i] > ema21[i]

            if not prev_bull and curr_bull:  # bullish cross
                entry = closes_5m[i]
                # Map back to 1m bars
                bar_idx = i * 5
                for offset_5m, label in [(2, "10m"), (4, "20m"), (6, "30m"), (12, "60m")]:
                    target_idx = bar_idx + offset_5m * 5
                    if target_idx < len(bars):
                        ret = (bars[target_idx]["c"] - entry) / entry * 100
                        trades.append(("BUY", label, ret, date_str))

            elif prev_bull and not curr_bull:  # bearish cross
                entry = closes_5m[i]
                bar_idx = i * 5
                for offset_5m, label in [(2, "10m"), (4, "20m"), (6, "30m"), (12, "60m")]:
                    target_idx = bar_idx + offset_5m * 5
                    if target_idx < len(bars):
                        ret = (entry - bars[target_idx]["c"]) / entry * 100
                        trades.append(("SELL", label, ret, date_str))

    for direction in ["BUY", "SELL"]:
        print(f"\n  EMA 9/21 Cross {direction}:")
        for label in ["10m", "20m", "30m", "60m"]:
            t = [r for d, l, r, _ in trades if d == direction and l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                print(f"    Exit {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Pattern 11: Scalping — 1-min Reversal Candles ───────────────────────────

def analyze_scalping(days):
    print("\n" + "="*80)
    print("PATTERN 11: SCALPING — REVERSAL CANDLE PATTERNS")
    print("="*80)

    # Look for: 3 consecutive bearish bars -> bullish reversal bar -> BUY scalp
    buy_reversal = []
    sell_reversal = []

    for date_str, bars in days:
        for i in range(4, min(300, len(bars))):
            # 3 consecutive bearish -> 1 bullish (hammer-like)
            if (bars[i-3]["c"] < bars[i-3]["o"] and
                bars[i-2]["c"] < bars[i-2]["o"] and
                bars[i-1]["c"] < bars[i-1]["o"] and
                bars[i]["c"] > bars[i]["o"] and
                bars[i]["c"] > bars[i-1]["c"]):  # close above prev close

                entry = bars[i]["c"]
                for offset, label in [(3, "3m"), (5, "5m"), (10, "10m")]:
                    if i + offset < len(bars):
                        ret = (bars[i+offset]["c"] - entry) / entry * 100
                        buy_reversal.append((label, ret, date_str))

            # 3 consecutive bullish -> 1 bearish
            if (bars[i-3]["c"] > bars[i-3]["o"] and
                bars[i-2]["c"] > bars[i-2]["o"] and
                bars[i-1]["c"] > bars[i-1]["o"] and
                bars[i]["c"] < bars[i]["o"] and
                bars[i]["c"] < bars[i-1]["c"]):

                entry = bars[i]["c"]
                for offset, label in [(3, "3m"), (5, "5m"), (10, "10m")]:
                    if i + offset < len(bars):
                        ret = (entry - bars[i+offset]["c"]) / entry * 100
                        sell_reversal.append((label, ret, date_str))

    for name, trades in [("3-bar drop -> BUY reversal", buy_reversal), ("3-bar rally -> SELL reversal", sell_reversal)]:
        print(f"\n  {name}:")
        for label in ["3m", "5m", "10m"]:
            t = [r for l, r, _ in trades if l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                print(f"    Exit {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Pattern 12: Gap + First 5-Bar Direction Confluence ───────────────────────

def analyze_gap_momentum(days):
    print("\n" + "="*80)
    print("PATTERN 12: GAP + FIRST 5-BAR MOMENTUM CONFLUENCE")
    print("="*80)

    prev_close = None
    scenarios = {
        "gap_up + bullish_5bar": [],
        "gap_up + bearish_5bar": [],
        "gap_down + bullish_5bar": [],
        "gap_down + bearish_5bar": [],
    }

    for date_str, bars in days:
        if not bars or len(bars) < 65:
            continue

        day_open = bars[0]["o"]

        if prev_close and prev_close > 0:
            gap_pct = (day_open - prev_close) / prev_close * 100
            first5_ret = (bars[4]["c"] - bars[0]["o"]) / bars[0]["o"] * 100

            if abs(gap_pct) > 0.1:  # meaningful gap
                is_gap_up = gap_pct > 0
                is_bull_5bar = first5_ret > 0

                key = f"gap_{'up' if is_gap_up else 'down'} + {'bullish' if is_bull_5bar else 'bearish'}_5bar"

                entry = bars[4]["c"]
                # If gap_up + bullish = continuation -> BUY
                # If gap_up + bearish = reversal -> SELL
                # If gap_down + bearish = continuation -> SELL
                # If gap_down + bullish = reversal -> BUY

                direction = "BUY" if (is_gap_up and is_bull_5bar) or (not is_gap_up and is_bull_5bar) else "SELL"

                for offset, label in [(15, "15m"), (30, "30m"), (60, "60m")]:
                    idx = 5 + offset
                    if idx < len(bars):
                        if direction == "BUY":
                            ret = (bars[idx]["c"] - entry) / entry * 100
                        else:
                            ret = (entry - bars[idx]["c"]) / entry * 100
                        scenarios[key].append((label, ret, gap_pct, first5_ret, direction, date_str))

                # EOD
                if direction == "BUY":
                    eod_ret = (bars[-1]["c"] - entry) / entry * 100
                else:
                    eod_ret = (entry - bars[-1]["c"]) / entry * 100
                scenarios[key].append(("EOD", eod_ret, gap_pct, first5_ret, direction, date_str))

        prev_close = bars[-1]["c"]

    for key, trades in scenarios.items():
        eod_trades = [t for t in trades if t[0] == "EOD"]
        if not eod_trades:
            continue
        direction = eod_trades[0][4]
        print(f"\n  {key} -> {direction} ({len(eod_trades)} days):")
        for label in ["15m", "30m", "60m", "EOD"]:
            t = [r for l, r, _, _, _, _ in trades if l == label]
            if t:
                avg = sum(t) / len(t)
                win = sum(1 for r in t if r > 0) / len(t) * 100
                print(f"    {label}: avg={avg:+.4f}%  win={win:.1f}%  n={len(t)}")

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Loading NIFTY 50 1-minute data (2025-2026)...")
    days = load_all_days()
    print(f"Loaded {len(days)} trading days ({days[0][0]} to {days[-1][0]})")
    total_bars = sum(len(bars) for _, bars in days)
    print(f"Total bars: {total_bars:,}")

    analyze_gaps(days)
    analyze_orb(days)
    analyze_time_of_day(days)
    analyze_prev_day(days)
    analyze_first_n_bars(days)
    analyze_day_of_week(days)
    analyze_vwap(days)
    analyze_opening_candle(days)
    analyze_vwap_reversion(days)
    analyze_ema_alignment(days)
    analyze_scalping(days)
    analyze_gap_momentum(days)

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)
    print("\nLook for patterns with:")
    print("  - Win rate > 55%")
    print("  - Positive avg return")
    print("  - Large sample size (n > 50)")
    print("  - Consistent across timeframes")

if __name__ == "__main__":
    main()
