"""
NIFTY Reverse Engineering Analysis — Find what precedes big moves

Approach:
1. Scan every 1m bar for 15-50pt moves in next 5-15 bars
2. Look BACKWARD at what happened before each big move
3. Catalog the pre-conditions (the "setup")
4. Find which setups have the highest hit rate

This is the OPPOSITE of pattern-first analysis.
We start from the OUTCOME and work backward.
"""

import json
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict
import math

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
            "dow": date.fromisoformat(filepath.stem.replace("_1m","")).weekday(),
        })
    return bars

def load_all():
    days = []
    prev_bars = None
    for f in sorted(DATA_DIR.glob("2025-*_1m.json")) + sorted(DATA_DIR.glob("2026-*_1m.json")):
        ds = f.stem.replace("_1m","")
        try:
            bars = load_day(f)
            if len(bars) > 100:
                days.append((ds, bars, prev_bars))
                prev_bars = bars
        except: pass
    return days

def ema_val(closes, period):
    if len(closes) < period: return closes[-1] if closes else 0
    k = 2.0/(period+1)
    v = sum(closes[:period])/period
    for c in closes[period:]:
        v = c*k + v*(1-k)
    return v


def main():
    print("Loading data...")
    days = load_all()
    print(f"Loaded {len(days)} days\n")

    # ══════════════════════════════════════════════════════════════════
    # STEP 1: Find ALL 15-50pt moves within 5-15 bars
    # ══════════════════════════════════════════════════════════════════

    print("="*80)
    print("STEP 1: Cataloging all 15-50pt moves (within 10 bars)")
    print("="*80)

    big_moves = []  # (date, bar_idx, direction, max_move_pts, time_to_peak)

    for ds, bars, prev in days:
        for i in range(10, len(bars) - 15):
            entry = bars[i]["c"]

            # Look forward 10 bars for max favorable move
            max_up = 0
            max_down = 0
            up_time = 0
            down_time = 0

            for j in range(i+1, min(i+11, len(bars))):
                up = bars[j]["h"] - entry
                down = entry - bars[j]["l"]
                if up > max_up:
                    max_up = up
                    up_time = j - i
                if down > max_down:
                    max_down = down
                    down_time = j - i

            if max_up >= 20 and max_up > max_down:
                big_moves.append({
                    "date": ds, "idx": i, "dir": "BUY", "move": max_up,
                    "time": up_time, "bars": bars, "prev": prev,
                    "minute": bars[i]["minute"], "dow": bars[i]["dow"],
                })
            elif max_down >= 20 and max_down > max_up:
                big_moves.append({
                    "date": ds, "idx": i, "dir": "SELL", "move": max_down,
                    "time": down_time, "bars": bars, "prev": prev,
                    "minute": bars[i]["minute"], "dow": bars[i]["dow"],
                })

    buy_moves = [m for m in big_moves if m["dir"] == "BUY"]
    sell_moves = [m for m in big_moves if m["dir"] == "SELL"]
    print(f"  Total big moves (>=20pts in 10 bars): {len(big_moves)}")
    print(f"  BUY moves: {len(buy_moves)}, SELL moves: {len(sell_moves)}")
    print(f"  Avg per day: {len(big_moves)/len(days):.1f}")

    # ══════════════════════════════════════════════════════════════════
    # STEP 2: Analyze what happened BEFORE each big move
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("STEP 2: Pre-conditions before big moves")
    print("="*80)

    def analyze_preconditions(moves, label):
        """For each big move, check what was true at the entry bar."""

        conditions = defaultdict(lambda: {"total": 0, "with": 0})

        for m in moves:
            bars = m["bars"]
            i = m["idx"]
            if i < 10: continue

            entry = bars[i]["c"]
            direction = m["dir"]

            # ── Condition 1: Previous bar direction ──
            prev_bull = bars[i-1]["c"] > bars[i-1]["o"]
            prev_bear = bars[i-1]["c"] < bars[i-1]["o"]
            curr_bull = bars[i]["c"] > bars[i]["o"]
            curr_bear = bars[i]["c"] < bars[i]["o"]

            conditions["same_dir_prev_bar"]["total"] += 1
            if (direction == "BUY" and curr_bull and prev_bull) or \
               (direction == "SELL" and curr_bear and prev_bear):
                conditions["same_dir_prev_bar"]["with"] += 1

            # ── Condition 2: 3-bar streak ──
            conditions["3bar_streak"]["total"] += 1
            if i >= 2:
                streak = all(bars[i-j]["c"] > bars[i-j]["o"] for j in range(3)) if direction == "BUY" else \
                         all(bars[i-j]["c"] < bars[i-j]["o"] for j in range(3))
                if streak:
                    conditions["3bar_streak"]["with"] += 1

            # ── Condition 3: Current bar is strong (body > 60% range) ──
            body = abs(bars[i]["c"] - bars[i]["o"])
            rng = bars[i]["h"] - bars[i]["l"]
            conditions["strong_curr_bar"]["total"] += 1
            if rng > 0 and body/rng > 0.6:
                conditions["strong_curr_bar"]["with"] += 1

            # ── Condition 4: Current bar body > 8pts ──
            conditions["body_gt_8"]["total"] += 1
            if body > 8:
                conditions["body_gt_8"]["with"] += 1

            # ── Condition 5: Current bar body > 12pts ──
            conditions["body_gt_12"]["total"] += 1
            if body > 12:
                conditions["body_gt_12"]["with"] += 1

            # ── Condition 6: Prev bar was opposite (reversal) ──
            conditions["prev_opposite"]["total"] += 1
            if (direction == "BUY" and prev_bear) or (direction == "SELL" and prev_bull):
                conditions["prev_opposite"]["with"] += 1

            # ── Condition 7: Price above/below EMA20 ──
            closes_to_i = [bars[j]["c"] for j in range(max(0,i-25), i+1)]
            ema20 = ema_val(closes_to_i, 20)
            conditions["with_ema_trend"]["total"] += 1
            if (direction == "BUY" and entry > ema20) or (direction == "SELL" and entry < ema20):
                conditions["with_ema_trend"]["with"] += 1

            # ── Condition 8: Recent tight range (last 5 bars range < 15pts) ──
            recent_high = max(bars[j]["h"] for j in range(i-5, i))
            recent_low = min(bars[j]["l"] for j in range(i-5, i))
            conditions["tight_range_5bar"]["total"] += 1
            if recent_high - recent_low < 15:
                conditions["tight_range_5bar"]["with"] += 1

            # ── Condition 9: Recent tight range (last 5 bars range < 20pts) ──
            conditions["tight_range_20"]["total"] += 1
            if recent_high - recent_low < 20:
                conditions["tight_range_20"]["with"] += 1

            # ── Condition 10: Gap from prev day ──
            if m["prev"]:
                pdc = m["prev"][-1]["c"]
                day_open = bars[0]["o"]
                gap = (day_open - pdc) / pdc * 100
                conditions["gap_up"]["total"] += 1
                conditions["gap_down"]["total"] += 1
                if gap > 0.3:
                    conditions["gap_up"]["with"] += 1
                if gap < -0.3:
                    conditions["gap_down"]["with"] += 1

            # ── Condition 11: Time of day ──
            minute = bars[i]["minute"]
            for tl, th, name in [
                (555, 585, "9:15-9:45"), (585, 630, "9:45-10:30"),
                (630, 690, "10:30-11:30"), (690, 780, "11:30-13:00"),
                (780, 870, "13:00-14:30"), (870, 930, "14:30-15:30"),
            ]:
                conditions[f"time_{name}"]["total"] += 1
                if tl <= minute <= th:
                    conditions[f"time_{name}"]["with"] += 1

            # ── Condition 12: Day of week ──
            dow_names = {0:"Mon",1:"Tue",2:"Wed",3:"Thu",4:"Fri"}
            for d in range(5):
                conditions[f"dow_{dow_names[d]}"]["total"] += 1
                if bars[i]["dow"] == d:
                    conditions[f"dow_{dow_names[d]}"]["with"] += 1

            # ── Condition 13: Consecutive narrow bars then expansion ──
            if i >= 3:
                narrow = all((bars[i-j-1]["h"] - bars[i-j-1]["l"]) < 10 for j in range(3))
                expansion = rng > 12
                conditions["narrow_then_expand"]["total"] += 1
                if narrow and expansion:
                    conditions["narrow_then_expand"]["with"] += 1

            # ── Condition 14: Close near high (BUY) or near low (SELL) ──
            conditions["close_near_extreme"]["total"] += 1
            if rng > 3:
                if direction == "BUY" and (bars[i]["c"] - bars[i]["l"]) / rng > 0.75:
                    conditions["close_near_extreme"]["with"] += 1
                elif direction == "SELL" and (bars[i]["h"] - bars[i]["c"]) / rng > 0.75:
                    conditions["close_near_extreme"]["with"] += 1

            # ── Condition 15: Engulfing bar ──
            conditions["engulfing"]["total"] += 1
            if i >= 1:
                top_i = max(bars[i]["o"], bars[i]["c"])
                bot_i = min(bars[i]["o"], bars[i]["c"])
                top_p = max(bars[i-1]["o"], bars[i-1]["c"])
                bot_p = min(bars[i-1]["o"], bars[i-1]["c"])
                if top_i > top_p and bot_i < bot_p and body > 8:
                    conditions["engulfing"]["with"] += 1

            # ── Condition 16: Bar breaks previous 5-bar high/low ──
            prev5_high = max(bars[j]["h"] for j in range(i-5, i))
            prev5_low = min(bars[j]["l"] for j in range(i-5, i))
            conditions["breaks_5bar_level"]["total"] += 1
            if (direction == "BUY" and bars[i]["c"] > prev5_high) or \
               (direction == "SELL" and bars[i]["c"] < prev5_low):
                conditions["breaks_5bar_level"]["with"] += 1

            # ── Condition 17: Bar breaks previous 10-bar high/low ──
            prev10_high = max(bars[j]["h"] for j in range(i-10, i))
            prev10_low = min(bars[j]["l"] for j in range(i-10, i))
            conditions["breaks_10bar_level"]["total"] += 1
            if (direction == "BUY" and bars[i]["c"] > prev10_high) or \
               (direction == "SELL" and bars[i]["c"] < prev10_low):
                conditions["breaks_10bar_level"]["with"] += 1

        print(f"\n  {label} ({len(moves)} big moves):")
        print(f"  {'Condition':<30} {'Present%':>10} {'Count':>8}")
        print(f"  {'-'*50}")

        sorted_conds = sorted(conditions.items(), key=lambda x: x[1]["with"]/max(x[1]["total"],1), reverse=True)
        for name, data in sorted_conds:
            pct = data["with"] / max(data["total"], 1) * 100
            print(f"  {name:<30} {pct:>9.1f}% {data['with']:>7}/{data['total']}")

    analyze_preconditions(buy_moves, "Before BUY 20pt+ moves")
    analyze_preconditions(sell_moves, "Before SELL 20pt+ moves")

    # ══════════════════════════════════════════════════════════════════
    # STEP 3: Test the best pre-conditions as actual entry signals
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("STEP 3: Testing pre-conditions as entry signals (REAL trades)")
    print("="*80)
    print("  For each condition: enter when condition is true, measure SL/TP/time result")

    def simulate(bars, i, direction, sl, tp, max_hold):
        entry = bars[i]["c"]
        sl_price = entry - sl if direction == "BUY" else entry + sl
        tp_price = entry + tp if direction == "BUY" else entry - tp
        for j in range(i+1, min(i+max_hold+1, len(bars))):
            if direction == "BUY":
                if bars[j]["l"] <= sl_price: return "LOSS", -sl
                if bars[j]["h"] >= tp_price: return "WIN", tp
            else:
                if bars[j]["h"] >= sl_price: return "LOSS", -sl
                if bars[j]["l"] <= tp_price: return "WIN", tp
        exit_p = bars[min(i+max_hold, len(bars)-1)]["c"]
        pnl = (exit_p - entry) if direction == "BUY" else (entry - exit_p)
        return ("WIN" if pnl > 0 else "LOSS"), pnl

    # Best conditions from Step 2 — test as signals
    setups = [
        {
            "name": "Strong bull bar (body>60%, >8pts) + close near high",
            "check": lambda bars, i: (
                bars[i]["c"] > bars[i]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                abs(bars[i]["c"]-bars[i]["o"])/(bars[i]["h"]-bars[i]["l"]) > 0.6 and
                (bars[i]["c"]-bars[i]["l"])/(bars[i]["h"]-bars[i]["l"]) > 0.75
            ),
            "dir": "BUY",
        },
        {
            "name": "Strong bear bar (body>60%, >8pts) + close near low",
            "check": lambda bars, i: (
                bars[i]["c"] < bars[i]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                abs(bars[i]["c"]-bars[i]["o"])/(bars[i]["h"]-bars[i]["l"]) > 0.6 and
                (bars[i]["h"]-bars[i]["c"])/(bars[i]["h"]-bars[i]["l"]) > 0.75
            ),
            "dir": "SELL",
        },
        {
            "name": "Bull bar breaks 10-bar high",
            "check": lambda bars, i: (
                i >= 10 and
                bars[i]["c"] > bars[i]["o"] and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-10, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Bear bar breaks 10-bar low",
            "check": lambda bars, i: (
                i >= 10 and
                bars[i]["c"] < bars[i]["o"] and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-10, i))
            ),
            "dir": "SELL",
        },
        {
            "name": "3 narrow bars (<10pt range) then bull expansion (>12pt range, body>60%)",
            "check": lambda bars, i: (
                i >= 3 and
                all((bars[i-j-1]["h"]-bars[i-j-1]["l"]) < 10 for j in range(3)) and
                (bars[i]["h"]-bars[i]["l"]) > 12 and
                bars[i]["c"] > bars[i]["o"] and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                abs(bars[i]["c"]-bars[i]["o"])/(bars[i]["h"]-bars[i]["l"]) > 0.6
            ),
            "dir": "BUY",
        },
        {
            "name": "3 narrow bars (<10pt range) then bear expansion (>12pt range, body>60%)",
            "check": lambda bars, i: (
                i >= 3 and
                all((bars[i-j-1]["h"]-bars[i-j-1]["l"]) < 10 for j in range(3)) and
                (bars[i]["h"]-bars[i]["l"]) > 12 and
                bars[i]["c"] < bars[i]["o"] and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                abs(bars[i]["c"]-bars[i]["o"])/(bars[i]["h"]-bars[i]["l"]) > 0.6
            ),
            "dir": "SELL",
        },
        {
            "name": "Strong bull + breaks 5-bar high + same dir as prev bar",
            "check": lambda bars, i: (
                i >= 5 and
                bars[i]["c"] > bars[i]["o"] and
                bars[i-1]["c"] > bars[i-1]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Strong bear + breaks 5-bar low + same dir as prev bar",
            "check": lambda bars, i: (
                i >= 5 and
                bars[i]["c"] < bars[i]["o"] and
                bars[i-1]["c"] < bars[i-1]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
        {
            "name": "Bull engulfing + breaks 5-bar high",
            "check": lambda bars, i: (
                i >= 5 and
                bars[i]["c"] > bars[i]["o"] and bars[i-1]["c"] < bars[i-1]["o"] and
                bars[i]["c"] > max(bars[i-1]["o"], bars[i-1]["c"]) and
                bars[i]["o"] < min(bars[i-1]["o"], bars[i-1]["c"]) and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Bear engulfing + breaks 5-bar low",
            "check": lambda bars, i: (
                i >= 5 and
                bars[i]["c"] < bars[i]["o"] and bars[i-1]["c"] > bars[i-1]["o"] and
                bars[i]["c"] < min(bars[i-1]["o"], bars[i-1]["c"]) and
                bars[i]["o"] > max(bars[i-1]["o"], bars[i-1]["c"]) and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
        {
            "name": "Narrow consolidation (5 bars <15pts) + bull break + body>10",
            "check": lambda bars, i: (
                i >= 5 and
                (max(bars[j]["h"] for j in range(i-5,i)) - min(bars[j]["l"] for j in range(i-5,i))) < 15 and
                bars[i]["c"] > bars[i]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 10 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Narrow consolidation (5 bars <15pts) + bear break + body>10",
            "check": lambda bars, i: (
                i >= 5 and
                (max(bars[j]["h"] for j in range(i-5,i)) - min(bars[j]["l"] for j in range(i-5,i))) < 15 and
                bars[i]["c"] < bars[i]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 10 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
    ]

    configs = [
        (15, 20, 10, "SL15 TP20 10m"),
        (15, 25, 15, "SL15 TP25 15m"),
        (20, 25, 10, "SL20 TP25 10m"),
        (20, 30, 15, "SL20 TP30 15m"),
        (20, 40, 20, "SL20 TP40 20m"),
        (25, 30, 10, "SL25 TP30 10m"),
        (25, 40, 15, "SL25 TP40 15m"),
        (25, 50, 20, "SL25 TP50 20m"),
    ]

    for setup in setups:
        results = {c[3]: [] for c in configs}

        for ds, bars, prev in days:
            fired = False
            for i in range(10, min(350, len(bars))):
                if fired: break
                try:
                    if setup["check"](bars, i):
                        fired = True
                        for sl, tp, hold, label in configs:
                            res, pnl = simulate(bars, i, setup["dir"], sl, tp, hold)
                            results[label].append((res, pnl))
                except: continue

        total = len(results[configs[0][3]])
        if total < 20: continue

        print(f"\n  {setup['name']} ({setup['dir']}, {total} trades):")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r,_ in trades if r == "WIN")
            wr = wins/n*100
            avg = sum(p for _,p in trades)/n
            tot = sum(p for _,p in trades)
            marker = " <<<" if wr >= 55 else (" <" if wr >= 50 else "")
            print(f"    {label}: WR={wr:.1f}% avg={avg:+.1f}pts total={tot:+.0f}pts n={n}{marker}")

    # ══════════════════════════════════════════════════════════════════
    # STEP 4: Combine best setups with time + DOW filters
    # ══════════════════════════════════════════════════════════════════

    print("\n" + "="*80)
    print("STEP 4: Best setups + time/DOW filters")
    print("="*80)

    combo_setups = [
        {
            "name": "Narrow consolidation bull break + Monday",
            "check": lambda bars, i: (
                i >= 5 and bars[i]["dow"] == 0 and
                (max(bars[j]["h"] for j in range(i-5,i)) - min(bars[j]["l"] for j in range(i-5,i))) < 15 and
                bars[i]["c"] > bars[i]["o"] and abs(bars[i]["c"]-bars[i]["o"]) > 10 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Strong bull + 5bar break + 10:00-11:30",
            "check": lambda bars, i: (
                i >= 5 and 600 <= bars[i]["minute"] <= 690 and
                bars[i]["c"] > bars[i]["o"] and bars[i-1]["c"] > bars[i-1]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Strong bear + 5bar break + 10:00-11:30",
            "check": lambda bars, i: (
                i >= 5 and 600 <= bars[i]["minute"] <= 690 and
                bars[i]["c"] < bars[i]["o"] and bars[i-1]["c"] < bars[i-1]["o"] and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
        {
            "name": "Narrow consolidation bear break + Tuesday",
            "check": lambda bars, i: (
                i >= 5 and bars[i]["dow"] == 1 and
                (max(bars[j]["h"] for j in range(i-5,i)) - min(bars[j]["l"] for j in range(i-5,i))) < 15 and
                bars[i]["c"] < bars[i]["o"] and abs(bars[i]["c"]-bars[i]["o"]) > 10 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
        {
            "name": "Bull engulfing + 5bar break + close>75% of range",
            "check": lambda bars, i: (
                i >= 5 and
                bars[i]["c"] > bars[i]["o"] and bars[i-1]["c"] < bars[i-1]["o"] and
                bars[i]["c"] > max(bars[i-1]["o"], bars[i-1]["c"]) and
                bars[i]["o"] < min(bars[i-1]["o"], bars[i-1]["c"]) and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                (bars[i]["c"]-bars[i]["l"])/(bars[i]["h"]-bars[i]["l"]) > 0.75 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Bear engulfing + 5bar break + close<25% of range",
            "check": lambda bars, i: (
                i >= 5 and
                bars[i]["c"] < bars[i]["o"] and bars[i-1]["c"] > bars[i-1]["o"] and
                bars[i]["c"] < min(bars[i-1]["o"], bars[i-1]["c"]) and
                bars[i]["o"] > max(bars[i-1]["o"], bars[i-1]["c"]) and
                abs(bars[i]["c"]-bars[i]["o"]) > 8 and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                (bars[i]["h"]-bars[i]["c"])/(bars[i]["h"]-bars[i]["l"]) > 0.75 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
        {
            "name": "Narrow 5bar + bull break + body>12 + close near high",
            "check": lambda bars, i: (
                i >= 5 and
                (max(bars[j]["h"] for j in range(i-5,i)) - min(bars[j]["l"] for j in range(i-5,i))) < 15 and
                bars[i]["c"] > bars[i]["o"] and abs(bars[i]["c"]-bars[i]["o"]) > 12 and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                (bars[i]["c"]-bars[i]["l"])/(bars[i]["h"]-bars[i]["l"]) > 0.75 and
                bars[i]["c"] > max(bars[j]["h"] for j in range(i-5, i))
            ),
            "dir": "BUY",
        },
        {
            "name": "Narrow 5bar + bear break + body>12 + close near low",
            "check": lambda bars, i: (
                i >= 5 and
                (max(bars[j]["h"] for j in range(i-5,i)) - min(bars[j]["l"] for j in range(i-5,i))) < 15 and
                bars[i]["c"] < bars[i]["o"] and abs(bars[i]["c"]-bars[i]["o"]) > 12 and
                (bars[i]["h"]-bars[i]["l"]) > 0 and
                (bars[i]["h"]-bars[i]["c"])/(bars[i]["h"]-bars[i]["l"]) > 0.75 and
                bars[i]["c"] < min(bars[j]["l"] for j in range(i-5, i))
            ),
            "dir": "SELL",
        },
    ]

    for setup in combo_setups:
        results = {c[3]: [] for c in configs}

        for ds, bars, prev in days:
            fired = False
            for i in range(10, min(350, len(bars))):
                if fired: break
                try:
                    if setup["check"](bars, i):
                        fired = True
                        for sl, tp, hold, label in configs:
                            res, pnl = simulate(bars, i, setup["dir"], sl, tp, hold)
                            results[label].append((res, pnl))
                except: continue

        total = len(results[configs[0][3]])
        if total < 10: continue

        print(f"\n  {setup['name']} ({setup['dir']}, {total} trades):")
        for sl, tp, hold, label in configs:
            trades = results[label]
            if not trades: continue
            n = len(trades)
            wins = sum(1 for r,_ in trades if r == "WIN")
            wr = wins/n*100
            avg = sum(p for _,p in trades)/n
            tot = sum(p for _,p in trades)
            marker = " <<<" if wr >= 55 else (" <" if wr >= 50 else "")
            print(f"    {label}: WR={wr:.1f}% avg={avg:+.1f}pts total={tot:+.0f}pts n={n}{marker}")


if __name__ == "__main__":
    main()
