"""
NIFTY 50 — REAL Win Rate Analysis (No Survivorship Bias)

Every trade is simulated: entry → SL/TP/time → result.
SL hits count as LOSSES. No filtering out stopped trades.

Goal: Find pattern + filter combos with REAL 55%+ win rate.
"""

import json
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

DATA_DIR = Path("C:/Users/BT-25/Desktop/project/resolute/data/NIFTY_50")
IST_OFFSET = 19800

def load_day(filepath):
    with open(filepath) as f:
        d = json.load(f)
    n = len(d["open"])
    bars = []
    for i in range(n):
        ts = d["timestamp"][i]
        ist = datetime.utcfromtimestamp(ts + IST_OFFSET)
        bars.append({
            "o": d["open"][i], "h": d["high"][i], "l": d["low"][i], "c": d["close"][i],
            "v": d["volume"][i] if "volume" in d else 0,
            "minute": ist.hour * 60 + ist.minute,
            "dow": date.fromisoformat(filepath.stem.replace("_1m", "")).weekday(),
        })
    return bars

def load_all():
    days = []
    prev_bars = None
    for f in sorted(DATA_DIR.glob("2025-*_1m.json")) + sorted(DATA_DIR.glob("2026-*_1m.json")):
        date_str = f.stem.replace("_1m", "")
        try:
            bars = load_day(f)
            if len(bars) > 100:
                days.append((date_str, bars, prev_bars))
                prev_bars = bars
        except:
            pass
    return days


def simulate_trade(bars, entry_idx, direction, sl_pts, tp_pts, max_hold_bars):
    """Simulate a single trade with SL/TP/time exit on 1m bars.

    Returns: (result, pnl_pts, hold_bars, exit_reason)
    result: 'WIN' or 'LOSS'
    """
    entry = bars[entry_idx]["c"]
    sl = entry - sl_pts if direction == "BUY" else entry + sl_pts
    tp = entry + tp_pts if direction == "BUY" else entry - tp_pts

    for j in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(bars))):
        b = bars[j]
        hold = j - entry_idx

        if direction == "BUY":
            # Check SL first (conservative)
            if b["l"] <= sl:
                return ("LOSS", -sl_pts, hold, "SL")
            if b["h"] >= tp:
                return ("WIN", tp_pts, hold, "TP")
        else:
            if b["h"] >= sl:
                return ("LOSS", -sl_pts, hold, "SL")
            if b["l"] <= tp:
                return ("WIN", tp_pts, hold, "TP")

    # Time exit
    exit_idx = min(entry_idx + max_hold_bars, len(bars) - 1)
    exit_price = bars[exit_idx]["c"]
    if direction == "BUY":
        pnl = exit_price - entry
    else:
        pnl = entry - exit_price

    return ("WIN" if pnl > 0 else "LOSS", pnl, max_hold_bars, "TIME")


def detect_engulfing_buy(bars, i):
    if i < 1: return False
    prev, curr = bars[i-1], bars[i]
    if curr["c"] <= curr["o"]: return False  # not bullish
    if prev["c"] >= prev["o"]: return False  # prev not bearish
    body = curr["c"] - curr["o"]
    rng = curr["h"] - curr["l"]
    if body < 10 or rng == 0 or body/rng < 0.6: return False
    return curr["c"] > prev["o"] and curr["o"] < prev["c"]

def detect_engulfing_sell(bars, i):
    if i < 1: return False
    prev, curr = bars[i-1], bars[i]
    if curr["c"] >= curr["o"]: return False
    if prev["c"] <= prev["o"]: return False
    body = curr["o"] - curr["c"]
    rng = curr["h"] - curr["l"]
    if body < 10 or rng == 0 or body/rng < 0.6: return False
    return curr["c"] < prev["o"] and curr["o"] > prev["c"]

def detect_momentum_burst_buy(bars, i):
    if i < 2: return False
    for j in range(i-2, i+1):
        if bars[j]["c"] <= bars[j]["o"]: return False
        body = bars[j]["c"] - bars[j]["o"]
        rng = bars[j]["h"] - bars[j]["l"]
        if rng < 5 or body < 0.5 * rng: return False
    return True

def detect_momentum_burst_sell(bars, i):
    if i < 2: return False
    for j in range(i-2, i+1):
        if bars[j]["c"] >= bars[j]["o"]: return False
        body = bars[j]["o"] - bars[j]["c"]
        rng = bars[j]["h"] - bars[j]["l"]
        if rng < 5 or body < 0.5 * rng: return False
    return True

def detect_pullback_buy(bars, i):
    if i < 5: return False
    trend = bars[i-2]["c"] - bars[i-5]["o"]
    if trend < 15: return False
    if bars[i-1]["c"] >= bars[i-1]["o"]: return False  # pullback bar not bearish
    if bars[i]["c"] <= bars[i]["o"]: return False  # continuation not bullish
    return bars[i]["c"] > bars[i-1]["h"]

def detect_pullback_sell(bars, i):
    if i < 5: return False
    trend = bars[i-5]["o"] - bars[i-2]["c"]
    if trend < 15: return False
    if bars[i-1]["c"] <= bars[i-1]["o"]: return False
    if bars[i]["c"] >= bars[i]["o"]: return False
    return bars[i]["c"] < bars[i-1]["l"]

def detect_inside_bar_buy(bars, i):
    if i < 2: return False
    mother_h, mother_l = bars[i-2]["h"], bars[i-2]["l"]
    if mother_h - mother_l < 8: return False
    if bars[i-1]["h"] > mother_h or bars[i-1]["l"] < mother_l: return False
    return bars[i]["c"] > mother_h

def detect_inside_bar_sell(bars, i):
    if i < 2: return False
    mother_h, mother_l = bars[i-2]["h"], bars[i-2]["l"]
    if mother_h - mother_l < 8: return False
    if bars[i-1]["h"] > mother_h or bars[i-1]["l"] < mother_l: return False
    return bars[i]["c"] < mother_l

def ema(values, period):
    if len(values) < period: return [values[-1]] * len(values)
    k = 2.0 / (period + 1)
    result = [sum(values[:period]) / period]
    for v in values[period:]:
        result.append(v * k + result[-1] * (1 - k))
    # Pad front
    return [result[0]] * (period - 1) + result


def run_analysis(days):
    print("="*80)
    print("NIFTY REAL WIN RATE ANALYSIS (No Survivorship Bias)")
    print("="*80)
    print(f"Data: {len(days)} trading days")

    # ══════════════════════════════════════════════════════════════════
    # TEST 1: Individual patterns — raw, no filters
    # ══════════════════════════════════════════════════════════════════

    configs = [
        # (sl, tp, hold, label)
        (10, 15, 10, "SL10 TP15 10m"),
        (10, 20, 15, "SL10 TP20 15m"),
        (15, 20, 10, "SL15 TP20 10m"),
        (15, 25, 15, "SL15 TP25 15m"),
        (15, 30, 20, "SL15 TP30 20m"),
        (20, 25, 10, "SL20 TP25 10m"),
        (20, 30, 15, "SL20 TP30 15m"),
        (20, 40, 20, "SL20 TP40 20m"),
    ]

    patterns = [
        ("Engulfing BUY", detect_engulfing_buy, "BUY"),
        ("Engulfing SELL", detect_engulfing_sell, "SELL"),
        ("Momentum BUY", detect_momentum_burst_buy, "BUY"),
        ("Momentum SELL", detect_momentum_burst_sell, "SELL"),
        ("Pullback BUY", detect_pullback_buy, "BUY"),
        ("Pullback SELL", detect_pullback_sell, "SELL"),
        ("Inside Bar BUY", detect_inside_bar_buy, "BUY"),
        ("Inside Bar SELL", detect_inside_bar_sell, "SELL"),
    ]

    print("\n" + "="*80)
    print("TEST 1: Individual Patterns — Raw (No Filters)")
    print("="*80)

    for p_name, p_func, p_dir in patterns:
        results = {c[3]: [] for c in configs}

        for date_str, bars, prev in days:
            fired = False
            for i in range(6, min(350, len(bars))):
                if fired: break
                if p_func(bars, i):
                    fired = True  # one trade per day per pattern
                    for sl, tp, hold, label in configs:
                        result, pnl, hb, reason = simulate_trade(bars, i, p_dir, sl, tp, hold)
                        results[label].append((result, pnl, reason))

        print(f"\n  {p_name}:")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            avg_pnl = sum(p for _, p, _ in trades) / n
            tp_exits = sum(1 for _, _, r in trades if r == "TP")
            sl_exits = sum(1 for _, _, r in trades if r == "SL")
            time_exits = sum(1 for _, _, r in trades if r == "TIME")
            marker = " <<<" if wr >= 50 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts n={n} (TP:{tp_exits} SL:{sl_exits} TIME:{time_exits}){marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 2: Confluence — 2+ patterns must agree
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 2: Confluence — 2+ Patterns Must Agree (same bar)")
    print("="*80)

    for direction in ["BUY", "SELL"]:
        if direction == "BUY":
            detectors = [detect_engulfing_buy, detect_momentum_burst_buy, detect_pullback_buy, detect_inside_bar_buy]
        else:
            detectors = [detect_engulfing_sell, detect_momentum_burst_sell, detect_pullback_sell, detect_inside_bar_sell]

        for min_conf in [2, 3]:
            results = {c[3]: [] for c in configs}

            for date_str, bars, prev in days:
                fired = False
                for i in range(6, min(350, len(bars))):
                    if fired: break
                    count = sum(1 for d in detectors if d(bars, i))
                    if count >= min_conf:
                        fired = True
                        for sl, tp, hold, label in configs:
                            result, pnl, hb, reason = simulate_trade(bars, i, direction, sl, tp, hold)
                            results[label].append((result, pnl, reason))

            print(f"\n  {direction} with {min_conf}+ patterns agreeing:")
            for sl, tp, hold, label in configs:
                trades = results[label]
                if not trades: continue
                n = len(trades)
                wins = sum(1 for r, _, _ in trades if r == "WIN")
                wr = wins / n * 100
                avg_pnl = sum(p for _, p, _ in trades) / n
                marker = " <<<" if wr >= 50 else ""
                print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts n={n}{marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 3: Pattern + EMA Trend Filter (only trade with trend)
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 3: Pattern + EMA20 Trend Filter")
    print("="*80)

    for p_name, p_func, p_dir in patterns:
        results = {c[3]: [] for c in configs}

        for date_str, bars, prev in days:
            closes = [b["c"] for b in bars]
            ema20 = ema(closes, 20)

            fired = False
            for i in range(25, min(350, len(bars))):
                if fired: break
                if not p_func(bars, i): continue

                # Trend filter: BUY only above EMA20, SELL only below
                if p_dir == "BUY" and bars[i]["c"] <= ema20[i]: continue
                if p_dir == "SELL" and bars[i]["c"] >= ema20[i]: continue

                fired = True
                for sl, tp, hold, label in configs:
                    result, pnl, hb, reason = simulate_trade(bars, i, p_dir, sl, tp, hold)
                    results[label].append((result, pnl, reason))

        total = sum(len(v) for v in results.values()) // len(configs)
        if total == 0: continue
        print(f"\n  {p_name} + EMA20 filter ({total} days with trades):")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            avg_pnl = sum(p for _, p, _ in trades) / n
            marker = " <<<" if wr >= 50 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts n={n}{marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 4: Pattern + Time-of-Day Filter (best time slots only)
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 4: Pattern + Time-of-Day Filter (10:00-11:30 IST only)")
    print("="*80)

    for p_name, p_func, p_dir in patterns:
        results = {c[3]: [] for c in configs}

        for date_str, bars, prev in days:
            fired = False
            for i in range(6, min(350, len(bars))):
                if fired: break
                # Only 10:00-11:30 IST (minute 600-690)
                if bars[i]["minute"] < 600 or bars[i]["minute"] > 690: continue
                if not p_func(bars, i): continue

                fired = True
                for sl, tp, hold, label in configs:
                    result, pnl, hb, reason = simulate_trade(bars, i, p_dir, sl, tp, hold)
                    results[label].append((result, pnl, reason))

        total = sum(len(v) for v in results.values()) // len(configs)
        if total == 0: continue
        print(f"\n  {p_name} (10:00-11:30 only, {total} trades):")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            avg_pnl = sum(p for _, p, _ in trades) / n
            marker = " <<<" if wr >= 50 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts n={n}{marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 5: Pattern + Day-of-Week Filter (Monday BUY, Tuesday SELL)
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 5: Pattern + Day-of-Week (Mon=BUY, Tue=SELL)")
    print("="*80)

    for p_name, p_func, p_dir in patterns:
        results = {c[3]: [] for c in configs}

        for date_str, bars, prev in days:
            if not bars: continue
            dow = bars[0]["dow"]
            # Monday(0)=BUY only, Tuesday(1)=SELL only
            if p_dir == "BUY" and dow != 0: continue
            if p_dir == "SELL" and dow != 1: continue

            fired = False
            for i in range(6, min(350, len(bars))):
                if fired: break
                if not p_func(bars, i): continue
                fired = True
                for sl, tp, hold, label in configs:
                    result, pnl, hb, reason = simulate_trade(bars, i, p_dir, sl, tp, hold)
                    results[label].append((result, pnl, reason))

        total = sum(len(v) for v in results.values()) // len(configs)
        if total == 0: continue
        print(f"\n  {p_name} (DOW filtered, {total} trades):")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            avg_pnl = sum(p for _, p, _ in trades) / n
            marker = " <<<" if wr >= 50 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts n={n}{marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 6: EMA Trend + Confluence + Time Filter (the ultimate combo)
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 6: ULTIMATE COMBO — EMA20 trend + 2+ patterns + time filter")
    print("="*80)

    for direction in ["BUY", "SELL"]:
        if direction == "BUY":
            detectors = [detect_engulfing_buy, detect_momentum_burst_buy, detect_pullback_buy, detect_inside_bar_buy]
        else:
            detectors = [detect_engulfing_sell, detect_momentum_burst_sell, detect_pullback_sell, detect_inside_bar_sell]

        results = {c[3]: [] for c in configs}

        for date_str, bars, prev in days:
            closes = [b["c"] for b in bars]
            ema20 = ema(closes, 20)

            fired = False
            for i in range(25, min(350, len(bars))):
                if fired: break

                # Time filter: 9:30-11:30 or 14:00-15:00
                m = bars[i]["minute"]
                if not ((570 <= m <= 690) or (840 <= m <= 900)): continue

                # EMA trend filter
                if direction == "BUY" and bars[i]["c"] <= ema20[i]: continue
                if direction == "SELL" and bars[i]["c"] >= ema20[i]: continue

                # Confluence: 2+ patterns
                count = sum(1 for d in detectors if d(bars, i))
                if count < 2: continue

                fired = True
                for sl, tp, hold, label in configs:
                    result, pnl, hb, reason = simulate_trade(bars, i, direction, sl, tp, hold)
                    results[label].append((result, pnl, reason))

        total = sum(len(v) for v in results.values()) // len(configs)
        print(f"\n  {direction} — EMA20 + 2+ confluence + time filter ({total} trades):")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            avg_pnl = sum(p for _, p, _ in trades) / n
            tp_count = sum(1 for _, _, r in trades if r == "TP")
            sl_count = sum(1 for _, _, r in trades if r == "SL")
            time_count = sum(1 for _, _, r in trades if r == "TIME")
            marker = " <<<" if wr >= 55 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts n={n} (TP:{tp_count} SL:{sl_count} T:{time_count}){marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 7: Wide TP with tight SL (asymmetric RR)
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 7: Asymmetric RR — wide TP, tight SL, long hold")
    print("="*80)

    asym_configs = [
        (10, 50, 30, "SL10 TP50 30m"),
        (15, 50, 30, "SL15 TP50 30m"),
        (15, 75, 45, "SL15 TP75 45m"),
        (20, 50, 20, "SL20 TP50 20m"),
        (20, 75, 30, "SL20 TP75 30m"),
        (20, 100, 60, "SL20 TP100 60m"),
        (25, 50, 15, "SL25 TP50 15m"),
        (25, 75, 30, "SL25 TP75 30m"),
        (30, 60, 20, "SL30 TP60 20m"),
        (30, 100, 45, "SL30 TP100 45m"),
    ]

    for p_name, p_func, p_dir in patterns[:4]:  # just engulfing + momentum
        results = {c[3]: [] for c in asym_configs}

        for date_str, bars, prev in days:
            closes = [b["c"] for b in bars]
            ema20 = ema(closes, 20)

            fired = False
            for i in range(25, min(350, len(bars))):
                if fired: break
                if not p_func(bars, i): continue
                if p_dir == "BUY" and bars[i]["c"] <= ema20[i]: continue
                if p_dir == "SELL" and bars[i]["c"] >= ema20[i]: continue

                fired = True
                for sl, tp, hold, label in asym_configs:
                    result, pnl, hb, reason = simulate_trade(bars, i, p_dir, sl, tp, hold)
                    results[label].append((result, pnl, reason))

        total = sum(len(v) for v in results.values()) // len(asym_configs)
        if total == 0: continue
        print(f"\n  {p_name} + EMA20 ({total} trades):")
        for sl, tp, hold, label in asym_configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            total_pnl = sum(p for _, p, _ in trades)
            avg_pnl = total_pnl / n
            marker = " <<<" if wr >= 50 or avg_pnl > 3 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts total={total_pnl:+.0f}pts n={n}{marker}")

    # ══════════════════════════════════════════════════════════════════
    # TEST 8: Gap-based entries (from macro analysis)
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("TEST 8: Gap-Based Entries (from V1 macro analysis)")
    print("="*80)

    gap_configs = [
        (10, 20, 15, "SL10 TP20 15m"),
        (15, 25, 15, "SL15 TP25 15m"),
        (15, 30, 20, "SL15 TP30 20m"),
        (20, 30, 15, "SL20 TP30 15m"),
        (20, 40, 30, "SL20 TP40 30m"),
        (25, 50, 30, "SL25 TP50 30m"),
    ]

    scenarios = {
        "Gap Down Big -> BUY (mean revert)": {"gap": "down_big", "dir": "BUY", "time_range": (555, 585)},
        "Gap Down Big -> BUY (9:30-10:00)": {"gap": "down_big", "dir": "BUY", "time_range": (570, 600)},
        "Gap Up Big -> SELL (first 15m)": {"gap": "up_big", "dir": "SELL", "time_range": (555, 570)},
        "Gap Up Big -> SELL (with engulfing)": {"gap": "up_big", "dir": "SELL", "pattern": detect_engulfing_sell},
    }

    prev_close = None
    day_gaps = {}
    for date_str, bars, prev in days:
        if prev_close and bars:
            gap = (bars[0]["o"] - prev_close) / prev_close * 100
            day_gaps[date_str] = gap
        prev_close = bars[-1]["c"] if bars else prev_close

    for scenario_name, cfg in scenarios.items():
        results = {c[3]: [] for c in gap_configs}

        for date_str, bars, prev in days:
            gap = day_gaps.get(date_str, 0)

            if cfg["gap"] == "down_big" and gap >= -0.3: continue
            if cfg["gap"] == "up_big" and gap <= 0.3: continue

            direction = cfg["dir"]
            time_lo, time_hi = cfg.get("time_range", (555, 690))
            pattern_fn = cfg.get("pattern", None)

            fired = False
            for i in range(1, min(120, len(bars))):
                if fired: break
                m = bars[i]["minute"]
                if m < time_lo or m > time_hi: continue

                if pattern_fn and not pattern_fn(bars, i): continue

                # If no pattern required, use simple directional bar
                if not pattern_fn:
                    if direction == "BUY" and bars[i]["c"] <= bars[i]["o"]: continue
                    if direction == "SELL" and bars[i]["c"] >= bars[i]["o"]: continue

                fired = True
                for sl, tp, hold, label in gap_configs:
                    result, pnl, hb, reason = simulate_trade(bars, i, direction, sl, tp, hold)
                    results[label].append((result, pnl, reason))

        total = sum(len(v) for v in results.values()) // len(gap_configs)
        if total == 0: continue
        print(f"\n  {scenario_name} ({total} trades):")
        for sl, tp, hold, label in gap_configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r, _, _ in trades if r == "WIN")
            wr = wins / n * 100
            avg_pnl = sum(p for _, p, _ in trades) / n
            total_pnl = sum(p for _, p, _ in trades)
            marker = " <<<" if wr >= 50 else ""
            print(f"    {label}: WR={wr:.1f}% avg={avg_pnl:+.1f}pts total={total_pnl:+.0f}pts n={n}{marker}")


def main():
    print("Loading NIFTY 50 1-minute data (2025-2026)...")
    days = load_all()
    print(f"Loaded {len(days)} trading days")
    run_analysis(days)

    print("\n" + "="*80)
    print("ANALYSIS COMPLETE")
    print("="*80)
    print("\nLook for: WR >= 50% with positive avg PnL and n >= 30")

if __name__ == "__main__":
    main()
