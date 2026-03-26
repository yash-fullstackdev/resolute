"""
NIFTY 50 Deep Scalping Analysis — Sniper Entry Patterns
2025-2026 data, 1-minute and 5-minute timeframes

Goal: Find HIGH MOMENTUM entries with LOW SL and SHORT hold (2-20 min)
Focus: Patterns that give 0.1-0.3% moves in 2-20 minutes with <0.05% adverse

Patterns analyzed:
1. Momentum burst detection (sudden vol + directional bar cluster)
2. 1m candle engulfing patterns -> immediate follow-through
3. Price rejection at round numbers (psychological levels)
4. Micro pullback in trend (1-2 bar dip in strong move)
5. Opening drive scalp (9:16-9:25 directional burst)
6. Breakout of 5m consolidation (narrow range -> expansion)
7. VWAP reclaim/rejection within 2 bars
8. Previous close level sniper (first touch of PDC)
9. Intraday high/low break scalp
10. Volume spike + directional bar (institutional footprint)
11. 3-bar inside breakout (coiling -> explosion)
12. First pullback after strong open (buy the first dip / sell the first bounce)
13. Power of 3 (AMD) — Accumulation/Manipulation/Distribution
14. First 1-min candle analysis (body, wick, size -> next 5 min prediction)
15. RR analysis at different SL levels (5pt, 10pt, 15pt, 20pt)
"""

import json
import os
from datetime import datetime, date
from pathlib import Path
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
            "ts": ts, "ist": ist,
            "minute": ist.hour * 60 + ist.minute,
            "body": abs(d["close"][i] - d["open"][i]),
            "range": d["high"][i] - d["low"][i],
            "is_bull": d["close"][i] > d["open"][i],
            "is_bear": d["close"][i] < d["open"][i],
        })
    return bars

def load_all_days():
    days = []
    prev_bars = None
    for f in sorted(DATA_DIR.glob("2025-*_1m.json")) + sorted(DATA_DIR.glob("2026-*_1m.json")):
        date_str = f.stem.replace("_1m", "")
        try:
            bars = load_day(f)
            if len(bars) > 100:
                days.append((date_str, bars, prev_bars))
                prev_bars = bars
        except Exception:
            pass
    return days

def build_5m(bars):
    result = []
    for i in range(0, len(bars) - 4, 5):
        chunk = bars[i:i+5]
        result.append({
            "o": chunk[0]["o"], "h": max(b["h"] for b in chunk),
            "l": min(b["l"] for b in chunk), "c": chunk[-1]["c"],
            "minute": chunk[0]["minute"],
            "body": abs(chunk[-1]["c"] - chunk[0]["o"]),
            "range": max(b["h"] for b in chunk) - min(b["l"] for b in chunk),
            "is_bull": chunk[-1]["c"] > chunk[0]["o"],
            "is_bear": chunk[-1]["c"] < chunk[0]["o"],
            "bar_idx": i,  # index into 1m bars
        })
    return result

def measure_scalp(bars, entry_idx, direction, entry_price, sl_points_list=[5, 10, 15, 20]):
    """Measure scalp outcome: max favorable excursion, time to target, SL hits."""
    results = {}
    for sl_pts in sl_points_list:
        sl_price = entry_price - sl_pts if direction == "BUY" else entry_price + sl_pts
        max_fav = 0
        max_fav_time = 0
        exit_price = entry_price
        exit_reason = "TIME"
        exit_time = 0

        for j in range(entry_idx + 1, min(entry_idx + 21, len(bars))):  # max 20 bars
            b = bars[j]
            hold_time = j - entry_idx

            if direction == "BUY":
                fav = b["h"] - entry_price
                adv = entry_price - b["l"]
                if b["l"] <= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    exit_time = hold_time
                    break
            else:
                fav = entry_price - b["l"]
                adv = b["h"] - entry_price
                if b["h"] >= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    exit_time = hold_time
                    break

            if fav > max_fav:
                max_fav = fav
                max_fav_time = hold_time

        # If no SL hit, measure at various exit times
        exit_returns = {}
        for exit_after in [2, 3, 5, 10, 15, 20]:
            idx = entry_idx + exit_after
            if idx < len(bars):
                if direction == "BUY":
                    ret = bars[idx]["c"] - entry_price
                else:
                    ret = entry_price - bars[idx]["c"]
                # Check if SL was hit before this time
                sl_hit_before = False
                for k in range(entry_idx + 1, min(idx + 1, len(bars))):
                    if direction == "BUY" and bars[k]["l"] <= sl_price:
                        sl_hit_before = True
                        break
                    elif direction == "SELL" and bars[k]["h"] >= sl_price:
                        sl_hit_before = True
                        break
                if not sl_hit_before:
                    exit_returns[f"{exit_after}m"] = ret

        results[sl_pts] = {
            "max_fav": max_fav,
            "max_fav_time": max_fav_time,
            "exit_reason": exit_reason,
            "exit_time": exit_time,
            "exit_returns": exit_returns,
        }
    return results


def print_scalp_stats(name, trades, sl_pts_list=[5, 10, 15, 20]):
    """Print detailed scalp statistics."""
    if not trades:
        return
    print(f"\n  {name} ({len(trades)} trades):")

    for sl in sl_pts_list:
        sl_trades = [t for t in trades if sl in t["scalp"]]
        if not sl_trades:
            continue

        sl_hit = sum(1 for t in sl_trades if t["scalp"][sl]["exit_reason"] == "SL")
        sl_rate = sl_hit / len(sl_trades) * 100

        # Max favorable excursion stats
        mfes = [t["scalp"][sl]["max_fav"] for t in sl_trades]
        avg_mfe = sum(mfes) / len(mfes) if mfes else 0
        mfe_gt_20 = sum(1 for m in mfes if m >= 20) / len(mfes) * 100
        mfe_gt_30 = sum(1 for m in mfes if m >= 30) / len(mfes) * 100
        mfe_gt_50 = sum(1 for m in mfes if m >= 50) / len(mfes) * 100

        print(f"    SL={sl}pts: SL_hit={sl_rate:.1f}% | MFE: avg={avg_mfe:.1f}pts, >20pts={mfe_gt_20:.1f}%, >30pts={mfe_gt_30:.1f}%, >50pts={mfe_gt_50:.1f}%")

        # Exit time returns (survived trades only)
        for exit_t in ["2m", "3m", "5m", "10m", "15m", "20m"]:
            rets = [t["scalp"][sl]["exit_returns"].get(exit_t) for t in sl_trades if exit_t in t["scalp"][sl]["exit_returns"]]
            rets = [r for r in rets if r is not None]
            if rets:
                avg = sum(rets) / len(rets)
                win = sum(1 for r in rets if r > 0) / len(rets) * 100
                avg_pts = avg
                rr = avg / sl if sl > 0 else 0
                print(f"      Exit {exit_t}: avg={avg_pts:+.1f}pts  win={win:.1f}%  RR={rr:+.2f}  n={len(rets)}")


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 1: Momentum Burst (3+ strong directional bars in a row)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_momentum_burst(days):
    print("\n" + "="*80)
    print("SCALP 1: MOMENTUM BURST (3+ strong directional 1m bars)")
    print("="*80)
    print("  Entry: after 3 consecutive strong bullish/bearish bars (body > 50% of range)")
    print("  Hypothesis: momentum continues for 2-10 more minutes")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        for i in range(3, min(350, len(bars))):
            # 3 strong bullish bars
            if (bars[i-2]["is_bull"] and bars[i-1]["is_bull"] and bars[i]["is_bull"] and
                bars[i-2]["body"] > bars[i-2]["range"] * 0.5 and
                bars[i-1]["body"] > bars[i-1]["range"] * 0.5 and
                bars[i]["body"] > bars[i]["range"] * 0.5 and
                bars[i]["range"] > 5):  # min 5pt range per bar

                entry = bars[i]["c"]
                scalp = measure_scalp(bars, i, "BUY", entry)
                buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": bars[i]["minute"]})

            # 3 strong bearish bars
            if (bars[i-2]["is_bear"] and bars[i-1]["is_bear"] and bars[i]["is_bear"] and
                bars[i-2]["body"] > bars[i-2]["range"] * 0.5 and
                bars[i-1]["body"] > bars[i-1]["range"] * 0.5 and
                bars[i]["body"] > bars[i]["range"] * 0.5 and
                bars[i]["range"] > 5):

                entry = bars[i]["c"]
                scalp = measure_scalp(bars, i, "SELL", entry)
                sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": bars[i]["minute"]})

    print_scalp_stats("BUY after 3 bull bars (>5pt each, body>50%)", buy_trades)
    print_scalp_stats("SELL after 3 bear bars (>5pt each, body>50%)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 2: Engulfing Candle on 1m -> Immediate Follow-Through
# ══════════════════════════════════════════════════════════════════════════════

def pattern_engulfing(days):
    print("\n" + "="*80)
    print("SCALP 2: 1m ENGULFING CANDLE -> IMMEDIATE FOLLOW-THROUGH")
    print("="*80)
    print("  Entry: bullish/bearish engulfing on 1m candle")
    print("  Filter: engulfing body > 10pts (meaningful move)")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        for i in range(1, min(350, len(bars))):
            prev_bar = bars[i-1]
            curr = bars[i]

            # Bullish engulfing: prev bearish, curr bullish, curr body engulfs prev body
            if (prev_bar["is_bear"] and curr["is_bull"] and
                curr["c"] > prev_bar["o"] and curr["o"] < prev_bar["c"] and
                curr["body"] > 10):

                entry = curr["c"]
                scalp = measure_scalp(bars, i, "BUY", entry)
                buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": curr["minute"]})

            # Bearish engulfing
            if (prev_bar["is_bull"] and curr["is_bear"] and
                curr["c"] < prev_bar["o"] and curr["o"] > prev_bar["c"] and
                curr["body"] > 10):

                entry = curr["c"]
                scalp = measure_scalp(bars, i, "SELL", entry)
                sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": curr["minute"]})

    print_scalp_stats("Bullish engulfing (body>10pts)", buy_trades)
    print_scalp_stats("Bearish engulfing (body>10pts)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 3: Round Number Rejection (psychological levels)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_round_numbers(days):
    print("\n" + "="*80)
    print("SCALP 3: ROUND NUMBER REJECTION (50/100 pt levels)")
    print("="*80)
    print("  Entry: price touches round number and rejects (wick > body)")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        for i in range(1, min(350, len(bars))):
            b = bars[i]
            # Find nearest 100-level
            nearest_100 = round(b["c"] / 100) * 100
            nearest_50 = round(b["c"] / 50) * 50

            for level in [nearest_100, nearest_50]:
                # Bullish rejection: low touches level, closes above with wick
                if (b["l"] <= level + 5 and b["l"] >= level - 5 and
                    b["c"] > level + 3 and b["is_bull"] and
                    (b["o"] - b["l"]) > b["body"] * 0.5):  # lower wick > 50% of body

                    entry = b["c"]
                    scalp = measure_scalp(bars, i, "BUY", entry)
                    buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b["minute"], "level": level})
                    break

                # Bearish rejection: high touches level, closes below
                if (b["h"] >= level - 5 and b["h"] <= level + 5 and
                    b["c"] < level - 3 and b["is_bear"] and
                    (b["h"] - b["o"]) > b["body"] * 0.5):

                    entry = b["c"]
                    scalp = measure_scalp(bars, i, "SELL", entry)
                    sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b["minute"], "level": level})
                    break

    print_scalp_stats("BUY rejection at round level", buy_trades)
    print_scalp_stats("SELL rejection at round level", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 4: Micro Pullback in Trend (1-2 bar dip in strong move)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_micro_pullback(days):
    print("\n" + "="*80)
    print("SCALP 4: MICRO PULLBACK IN TREND (1-2 bar dip in strong uptrend)")
    print("="*80)
    print("  Entry: 3+ bullish bars, then 1-2 bearish bars, then bullish continuation bar")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        for i in range(6, min(350, len(bars))):
            # Uptrend: bars i-5 to i-3 are bullish, bar i-2 or i-1 is bearish (pullback), bar i is bullish
            trend_bars = bars[i-5:i-2]
            if len(trend_bars) < 3:
                continue

            bullish_trend = sum(1 for b in trend_bars if b["is_bull"]) >= 2
            trend_move = trend_bars[-1]["c"] - trend_bars[0]["o"]

            if bullish_trend and trend_move > 15:  # 15pt uptrend
                # Pullback: 1-2 bearish bars
                pullback = bars[i-2:i]
                if any(b["is_bear"] for b in pullback):
                    # Continuation: current bar is bullish
                    if bars[i]["is_bull"] and bars[i]["c"] > bars[i-1]["h"]:
                        entry = bars[i]["c"]
                        scalp = measure_scalp(bars, i, "BUY", entry)
                        buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": bars[i]["minute"]})

            # Downtrend version
            bearish_trend = sum(1 for b in trend_bars if b["is_bear"]) >= 2
            trend_move_down = trend_bars[0]["o"] - trend_bars[-1]["c"]

            if bearish_trend and trend_move_down > 15:
                pullback = bars[i-2:i]
                if any(b["is_bull"] for b in pullback):
                    if bars[i]["is_bear"] and bars[i]["c"] < bars[i-1]["l"]:
                        entry = bars[i]["c"]
                        scalp = measure_scalp(bars, i, "SELL", entry)
                        sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": bars[i]["minute"]})

    print_scalp_stats("BUY pullback in uptrend (trend>15pts)", buy_trades)
    print_scalp_stats("SELL pullback in downtrend (trend>15pts)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 5: Opening Drive Scalp (9:16-9:25)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_opening_drive(days):
    print("\n" + "="*80)
    print("SCALP 5: OPENING DRIVE (entry at bar 2-3, ride the momentum)")
    print("="*80)
    print("  Entry: if bar 2 continues bar 1 direction with body>60%, enter on bar 2 close")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        if len(bars) < 25:
            continue

        b1 = bars[0]
        b2 = bars[1]

        # Both bullish, bar2 body > 60% of range, total move > 10pts
        if (b1["is_bull"] and b2["is_bull"] and
            b2["body"] > b2["range"] * 0.6 and
            b2["c"] - b1["o"] > 10):

            entry = b2["c"]
            scalp = measure_scalp(bars, 1, "BUY", entry)
            buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b2["minute"]})

        # Both bearish
        if (b1["is_bear"] and b2["is_bear"] and
            b2["body"] > b2["range"] * 0.6 and
            b1["o"] - b2["c"] > 10):

            entry = b2["c"]
            scalp = measure_scalp(bars, 1, "SELL", entry)
            sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b2["minute"]})

    print_scalp_stats("BUY opening drive (2 bull bars, >10pts)", buy_trades)
    print_scalp_stats("SELL opening drive (2 bear bars, >10pts)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 6: 5m Consolidation Breakout (narrow range -> expansion)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_5m_breakout(days):
    print("\n" + "="*80)
    print("SCALP 6: 5m CONSOLIDATION BREAKOUT (3 narrow bars -> expansion)")
    print("="*80)

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        bars_5m = build_5m(bars)
        if len(bars_5m) < 10:
            continue

        for i in range(3, len(bars_5m)):
            prev3 = bars_5m[i-3:i]
            curr = bars_5m[i]

            # 3 narrow range bars followed by expansion
            avg_range = sum(b["range"] for b in prev3) / 3
            if avg_range < 20:  # tight consolidation (<20pts per 5m bar)
                if curr["range"] > avg_range * 2:  # expansion > 2x
                    bar_idx = curr["bar_idx"]
                    if bar_idx + 20 >= len(bars):
                        continue

                    if curr["is_bull"]:
                        entry = bars[bar_idx + 4]["c"]  # enter at end of 5m bar
                        scalp = measure_scalp(bars, bar_idx + 4, "BUY", entry)
                        buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": curr["minute"]})
                    elif curr["is_bear"]:
                        entry = bars[bar_idx + 4]["c"]
                        scalp = measure_scalp(bars, bar_idx + 4, "SELL", entry)
                        sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": curr["minute"]})

    print_scalp_stats("BUY 5m breakout (3 narrow -> bull expansion)", buy_trades)
    print_scalp_stats("SELL 5m breakout (3 narrow -> bear expansion)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 7: Previous Close Level Sniper (first touch of PDC)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_pdc_sniper(days):
    print("\n" + "="*80)
    print("SCALP 7: PREVIOUS CLOSE SNIPER (first touch of PDC in first 60 min)")
    print("="*80)

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev_bars in days:
        if prev_bars is None or not bars:
            continue

        pdc = prev_bars[-1]["c"]
        day_open = bars[0]["o"]
        gap = day_open - pdc

        # Only if there's a meaningful gap (price is away from PDC)
        if abs(gap) < 10:
            continue

        # Wait for first touch of PDC
        for i in range(1, min(60, len(bars))):
            b = bars[i]

            # Price comes DOWN to PDC from above (gap up) -> BUY at PDC
            if gap > 10 and b["l"] <= pdc + 3 and b["c"] > pdc:
                entry = b["c"]
                scalp = measure_scalp(bars, i, "BUY", entry)
                buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b["minute"], "gap": gap})
                break

            # Price comes UP to PDC from below (gap down) -> SELL at PDC
            if gap < -10 and b["h"] >= pdc - 3 and b["c"] < pdc:
                entry = b["c"]
                scalp = measure_scalp(bars, i, "SELL", entry)
                sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b["minute"], "gap": gap})
                break

    print_scalp_stats("BUY at PDC (gap up, price dips to PDC)", buy_trades)
    print_scalp_stats("SELL at PDC (gap down, price rallies to PDC)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 8: Volume Spike + Directional Bar
# ══════════════════════════════════════════════════════════════════════════════

def pattern_volume_spike(days):
    print("\n" + "="*80)
    print("SCALP 8: VOLUME SPIKE + STRONG DIRECTIONAL BAR")
    print("="*80)
    print("  Entry: bar with volume > 3x avg AND body > 10pts AND body > 70% of range")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        if len(bars) < 30:
            continue

        # Running volume average
        vol_sum = sum(b["v"] for b in bars[:20])
        vol_count = 20

        for i in range(20, min(350, len(bars))):
            avg_vol = vol_sum / vol_count if vol_count > 0 else 1
            curr_vol = bars[i]["v"]

            vol_sum += curr_vol
            vol_count += 1

            if avg_vol < 1:
                continue

            vol_ratio = curr_vol / avg_vol
            b = bars[i]
            body_ratio = b["body"] / b["range"] if b["range"] > 0 else 0

            if vol_ratio > 3 and b["body"] > 10 and body_ratio > 0.7:
                if b["is_bull"]:
                    entry = b["c"]
                    scalp = measure_scalp(bars, i, "BUY", entry)
                    buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b["minute"], "vol_ratio": vol_ratio})
                elif b["is_bear"]:
                    entry = b["c"]
                    scalp = measure_scalp(bars, i, "SELL", entry)
                    sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": b["minute"], "vol_ratio": vol_ratio})

    print_scalp_stats("BUY on volume spike + bull bar", buy_trades)
    print_scalp_stats("SELL on volume spike + bear bar", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 9: Inside Bar Breakout (coiling -> explosion)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_inside_bar(days):
    print("\n" + "="*80)
    print("SCALP 9: INSIDE BAR BREAKOUT (1m timeframe)")
    print("="*80)
    print("  Setup: bar whose H and L are within previous bar's H and L")
    print("  Entry: break of inside bar's high (BUY) or low (SELL)")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        for i in range(2, min(350, len(bars))):
            mother = bars[i-1]
            inside = bars[i]

            # Inside bar: inside bar's range is within mother bar's range
            if inside["h"] <= mother["h"] and inside["l"] >= mother["l"] and mother["range"] > 8:
                # Wait for breakout on next bar
                if i + 1 < len(bars):
                    next_bar = bars[i+1]
                    if next_bar["h"] > mother["h"]:  # breakout up
                        entry = mother["h"]
                        scalp = measure_scalp(bars, i+1, "BUY", entry)
                        buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": next_bar["minute"]})
                    elif next_bar["l"] < mother["l"]:  # breakout down
                        entry = mother["l"]
                        scalp = measure_scalp(bars, i+1, "SELL", entry)
                        sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": next_bar["minute"]})

    print_scalp_stats("BUY inside bar breakout up", buy_trades)
    print_scalp_stats("SELL inside bar breakout down", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 10: First Pullback After Strong Open
# ══════════════════════════════════════════════════════════════════════════════

def pattern_first_pullback(days):
    print("\n" + "="*80)
    print("SCALP 10: FIRST PULLBACK AFTER STRONG OPEN (buy first dip / sell first bounce)")
    print("="*80)
    print("  Setup: strong first 5 bars (>20pts move), then 1-3 bars pullback")
    print("  Entry: when pullback bar closes in direction of trend")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        if len(bars) < 30:
            continue

        # Measure first 5 bars
        first5_move = bars[4]["c"] - bars[0]["o"]

        if abs(first5_move) < 20:
            continue

        # Look for pullback in bars 5-15
        if first5_move > 20:  # bullish open
            # Find first bearish bar (pullback start)
            for i in range(5, min(15, len(bars))):
                if bars[i]["is_bear"]:
                    # Find pullback end: first bullish bar after the bearish bar
                    for j in range(i+1, min(i+4, len(bars))):
                        if bars[j]["is_bull"] and bars[j]["c"] > bars[j-1]["h"]:
                            entry = bars[j]["c"]
                            scalp = measure_scalp(bars, j, "BUY", entry)
                            buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str,
                                             "minute": bars[j]["minute"], "open_move": first5_move})
                            break
                    break

        elif first5_move < -20:  # bearish open
            for i in range(5, min(15, len(bars))):
                if bars[i]["is_bull"]:
                    for j in range(i+1, min(i+4, len(bars))):
                        if bars[j]["is_bear"] and bars[j]["c"] < bars[j-1]["l"]:
                            entry = bars[j]["c"]
                            scalp = measure_scalp(bars, j, "SELL", entry)
                            sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str,
                                              "minute": bars[j]["minute"], "open_move": first5_move})
                            break
                    break

    print_scalp_stats("BUY first pullback in bullish open (>20pts)", buy_trades)
    print_scalp_stats("SELL first pullback in bearish open (>20pts)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 11: Power of 3 (AMD pattern)
# ══════════════════════════════════════════════════════════════════════════════

def pattern_power_of_3(days):
    print("\n" + "="*80)
    print("SCALP 11: POWER OF 3 / AMD (Accumulation 9:15-9:30, Manipulation 9:30-9:45, Distribution 9:45+)")
    print("="*80)
    print("  Setup: Range forms 9:15-9:30, fake breakout 9:30-9:45, real move 9:45+")
    print("  Entry: when price returns inside the 9:15-9:30 range after fake breakout")

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        if len(bars) < 60:
            continue

        # Accumulation: first 15 bars (9:15-9:30)
        acc_bars = bars[:15]
        acc_high = max(b["h"] for b in acc_bars)
        acc_low = min(b["l"] for b in acc_bars)
        acc_range = acc_high - acc_low

        if acc_range < 15:  # skip very tight accumulation
            continue

        # Manipulation: bars 15-30 (9:30-9:45)
        manip_bars = bars[15:30]
        manip_high = max(b["h"] for b in manip_bars)
        manip_low = min(b["l"] for b in manip_bars)

        # Fake breakout UP (manipulation sweeps accumulation high, then reverses)
        if manip_high > acc_high + 3:
            # Look for price to come back inside accumulation range
            for i in range(30, min(60, len(bars))):
                if bars[i]["c"] < acc_high and bars[i]["is_bear"]:
                    entry = bars[i]["c"]
                    scalp = measure_scalp(bars, i, "SELL", entry)
                    sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str,
                                       "minute": bars[i]["minute"], "acc_range": acc_range})
                    break

        # Fake breakout DOWN (manipulation sweeps accumulation low)
        if manip_low < acc_low - 3:
            for i in range(30, min(60, len(bars))):
                if bars[i]["c"] > acc_low and bars[i]["is_bull"]:
                    entry = bars[i]["c"]
                    scalp = measure_scalp(bars, i, "BUY", entry)
                    buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str,
                                       "minute": bars[i]["minute"], "acc_range": acc_range})
                    break

    print_scalp_stats("BUY after fake breakdown (AMD bull)", buy_trades)
    print_scalp_stats("SELL after fake breakout (AMD bear)", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 12: Intraday High/Low Break Scalp
# ══════════════════════════════════════════════════════════════════════════════

def pattern_intraday_hl_break(days):
    print("\n" + "="*80)
    print("SCALP 12: INTRADAY HIGH/LOW BREAK (break of rolling 30-bar high/low)")
    print("="*80)

    buy_trades = []
    sell_trades = []

    for date_str, bars, prev in days:
        fired_buy = False
        fired_sell = False

        for i in range(30, min(300, len(bars))):
            lookback = bars[i-30:i]
            rolling_high = max(b["h"] for b in lookback)
            rolling_low = min(b["l"] for b in lookback)

            # Break above rolling 30-bar high
            if not fired_buy and bars[i]["c"] > rolling_high and bars[i]["is_bull"] and bars[i]["body"] > 5:
                entry = bars[i]["c"]
                scalp = measure_scalp(bars, i, "BUY", entry)
                buy_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": bars[i]["minute"]})
                fired_buy = True

            # Break below rolling 30-bar low
            if not fired_sell and bars[i]["c"] < rolling_low and bars[i]["is_bear"] and bars[i]["body"] > 5:
                entry = bars[i]["c"]
                scalp = measure_scalp(bars, i, "SELL", entry)
                sell_trades.append({"entry": entry, "scalp": scalp, "date": date_str, "minute": bars[i]["minute"]})
                fired_sell = True

    print_scalp_stats("BUY 30-bar high breakout", buy_trades)
    print_scalp_stats("SELL 30-bar low breakout", sell_trades)


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN 13: First 1-min Candle Analysis
# ══════════════════════════════════════════════════════════════════════════════

def pattern_first_candle(days):
    print("\n" + "="*80)
    print("SCALP 13: FIRST 1-MIN CANDLE ANALYSIS (9:15 bar -> what happens next)")
    print("="*80)

    big_bull_first = []
    big_bear_first = []
    hammer_first = []
    shooting_first = []

    for date_str, bars, prev in days:
        if len(bars) < 25:
            continue

        b = bars[0]
        body_ratio = b["body"] / b["range"] if b["range"] > 0 else 0

        outcomes = {}
        for offset, label in [(2, "2m"), (3, "3m"), (5, "5m"), (10, "10m"), (15, "15m"), (20, "20m")]:
            if offset < len(bars):
                outcomes[label] = bars[offset]["c"] - b["c"]

        if b["is_bull"] and b["body"] > 15 and body_ratio > 0.6:
            big_bull_first.append(outcomes)
        elif b["is_bear"] and b["body"] > 15 and body_ratio > 0.6:
            big_bear_first.append(outcomes)
        elif b["is_bull"] and (b["o"] - b["l"]) > b["body"] * 2:  # hammer
            hammer_first.append(outcomes)
        elif b["is_bear"] and (b["h"] - b["o"]) > b["body"] * 2:  # shooting star
            shooting_first.append(outcomes)

    for name, trades in [
        ("Big bull 1st bar (body>15pts, >60%)", big_bull_first),
        ("Big bear 1st bar (body>15pts, >60%)", big_bear_first),
        ("Hammer 1st bar (lower wick > 2x body)", hammer_first),
        ("Shooting star 1st bar (upper wick > 2x body)", shooting_first),
    ]:
        if not trades:
            continue
        print(f"\n  {name} ({len(trades)} days):")
        for label in ["2m", "3m", "5m", "10m", "15m", "20m"]:
            pts = [t.get(label, 0) for t in trades if label in t]
            if pts:
                avg = sum(pts) / len(pts)
                if "bull" in name.lower() or "hammer" in name.lower():
                    win = sum(1 for p in pts if p > 0) / len(pts) * 100
                else:
                    win = sum(1 for p in pts if p < 0) / len(pts) * 100  # continuation = bearish
                print(f"    After {label}: avg={avg:+.1f}pts  cont%={win:.1f}%  n={len(pts)}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("Loading NIFTY 50 1-minute data (2025-2026)...")
    days = load_all_days()
    print(f"Loaded {len(days)} trading days")

    pattern_momentum_burst(days)
    pattern_engulfing(days)
    pattern_round_numbers(days)
    pattern_micro_pullback(days)
    pattern_opening_drive(days)
    pattern_5m_breakout(days)
    pattern_pdc_sniper(days)
    pattern_volume_spike(days)
    pattern_inside_bar(days)
    pattern_first_pullback(days)
    pattern_power_of_3(days)
    pattern_intraday_hl_break(days)
    pattern_first_candle(days)

    print("\n" + "="*80)
    print("DEEP SCALP ANALYSIS COMPLETE")
    print("="*80)
    print("\nBest patterns for scalping (look for):")
    print("  - High win% at 3-5m exit")
    print("  - Low SL hit rate at 10pt SL")
    print("  - MFE (max favorable excursion) > 20pts in >40% of trades")
    print("  - Positive RR at target exit time")

if __name__ == "__main__":
    main()
