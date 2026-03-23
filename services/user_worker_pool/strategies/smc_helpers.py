"""Smart Money Concepts helpers — BigBeluga-aligned.

detect_market_structure — BOS/CHoCH state machine + ATR-sized Order Blocks
detect_fvg              — Fair Value Gap (3-bar imbalance)

Algorithm source: BigBeluga SMC indicator (PineScript v5).

Key design decisions matching BigBeluga:
  - OBs are created ONLY on confirmed BOS/CHoCH events (not recalculated every bar).
  - OB zone = ATR-sized from the extreme candle in the structure segment:
      Bullish OB: btm=low[extreme], top=min(low+ATR, high[extreme])
      Bearish OB: top=high[extreme], btm=max(high-ATR, low[extreme])
  - Mitigation uses "Close" method: bullish OB removed when min(c,o) < ob.btm,
    bearish OB removed when max(c,o) > ob.top.
  - Sweeps: wick through BOS/CHoCH level but close back inside.
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# ATR (Wilder) — local to avoid import cycle
# ─────────────────────────────────────────────────────────────────────────────

def _atr_wilder(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    n = len(closes)
    if n < 2:
        return [0.0] * n
    trs: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    result = [0.0] * n
    if not trs:
        return result
    result[1] = trs[0]
    alpha = 1.0 / period
    for i in range(2, n):
        result[i] = result[i - 1] * (1.0 - alpha) + trs[i - 1] * alpha
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_ob(
    highs: list[float],
    lows: list[float],
    atr: list[float],
    start: int,
    end: int,         # exclusive
    use_max: bool,
    ob_length: int = 5,
) -> dict | None:
    """Find extreme candle in [start, end) and return an ATR-sized OB dict.

    BigBeluga uses atr(200) / (5/ob_length). We use ATR(50) which is practical
    for intraday data, scaled by ob_length/5 to match BigBeluga parameterization.
    """
    start = max(0, start)
    end   = min(end, len(highs))
    if start >= end:
        return None

    scale = max(ob_length, 1) / 5.0  # ob_length=5 → scale=1.0 (default)

    if use_max:
        # Highest high → bearish supply OB
        best = start
        for i in range(start + 1, end):
            if highs[i] > highs[best]:
                best = i
        curr_atr = (atr[best] if best < len(atr) else 1.0) * scale
        ob_top   = highs[best]
        ob_btm   = max(ob_top - curr_atr, lows[best])   # capped at candle low
        return {
            "top": ob_top, "btm": ob_btm,
            "avg": (ob_top + ob_btm) / 2.0,
            "idx": best, "bull": False,
        }
    else:
        # Lowest low → bullish demand OB
        best = start
        for i in range(start + 1, end):
            if lows[i] < lows[best]:
                best = i
        curr_atr = (atr[best] if best < len(atr) else 1.0) * scale
        ob_btm   = lows[best]
        ob_top   = min(ob_btm + curr_atr, highs[best])  # capped at candle high
        return {
            "top": ob_top, "btm": ob_btm,
            "avg": (ob_top + ob_btm) / 2.0,
            "idx": best, "bull": True,
        }


def _extreme_val(
    vals: list[float],
    start: int,
    end: int,
    use_max: bool,
) -> float:
    start = max(0, start)
    end   = min(end, len(vals))
    if start >= end:
        return vals[max(0, start - 1)]
    best = vals[start]
    for i in range(start + 1, end):
        if (use_max and vals[i] > best) or (not use_max and vals[i] < best):
            best = vals[i]
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Main: BOS/CHoCH state machine  (BigBeluga `structure()` function)
# ─────────────────────────────────────────────────────────────────────────────

def detect_market_structure(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    opens: list[float],
    length: int = 5,
    ob_limit: int = 5,
) -> dict | None:
    """BigBeluga SMC market structure detection.

    Runs a bar-by-bar replay over the full series and returns the final state:

      trend       — 1 (bullish) | -1 (bearish) | 0 (undefined)
      bull_obs    — active unmitigated demand zones [{top, btm, avg, idx, bull}]
      bear_obs    — active unmitigated supply zones [{top, btm, avg, idx, bull}]
      last_sweep  — True if a liquidity sweep was detected in recent bars
      bos_level   — current pending BOS level (None if not set)
      choch_level — current CHoCH invalidation level

    OB size: ATR-based (Length mode), matching BigBeluga default.
    Mitigation: "Close" method — bullish OB removed when min(c,o) < ob.btm,
                                  bearish OB removed when max(c,o) > ob.top.
    """
    n = len(closes)
    if n < 15:
        return None

    # ATR(14) — regime gate (used in smc_order_block.py evaluate())
    atr = _atr_wilder(highs, lows, closes, 14)
    # ATR(50) — OB sizing: BigBeluga uses atr(200); ATR(50) gives wider zones
    # that match institutional retracement depth in practice.
    atr_ob = _atr_wilder(highs, lows, closes, 50)

    # ── State ──────────────────────────────────────────────────────────────
    trend    = 0
    phase    = 1             # 1 = finding initial break, 2 = active
    # BigBeluga initialises: ms.bos=high[0], ms.choch=low[0]
    ms_bos   = highs[0]      # break above = first bullish structure (phase 1 only)
    ms_choch = lows[0]       # break below = first bearish structure (phase 1 only)
    ms_main  = 0.0           # running extreme (highest-high in bull, lowest-low in bear)
    ms_seg   = 0             # bar index: start of current structure segment

    run_high = highs[0]
    run_low  = lows[0]

    bull_obs: list[dict] = []   # demand zones
    bear_obs: list[dict] = []   # supply zones
    last_sweep = False

    for i in range(1, n):
        h, l, c, o = highs[i], lows[i], closes[i], opens[i]
        p_c = closes[i - 1]
        p_o = opens[i - 1]

        # Running extreme — fires once per new extreme (crossup / crossdn)
        crossup = h > run_high
        crossdn = l < run_low
        if crossup or crossdn:
            run_high = h
            run_low  = l

        # ── Phase 1: identify first structure break ─────────────────────────
        if phase == 1:
            if c >= ms_bos:
                # First bullish break
                trend    = 1
                phase    = 2
                ms_main  = h
                ms_seg   = i
                ob = _build_ob(highs, lows, atr_ob, 0, i, use_max=False, ob_length=length)
                if ob:
                    bear_obs.append(ob)   # supply OB created at the swing low
                ms_bos = None
            elif c <= ms_choch:
                # First bearish break
                trend    = -1
                phase    = 2
                ms_main  = l
                ms_seg   = i
                ob = _build_ob(highs, lows, atr_ob, 0, i, use_max=True, ob_length=length)
                if ob:
                    bull_obs.append(ob)   # demand OB created at the swing high
                ms_bos = None
            else:
                # Expand initial window — track widest range seen
                if h > ms_bos:
                    ms_bos = h
                if l < ms_choch:
                    ms_choch = l

        # ── Phase 2: active structure tracking ─────────────────────────────
        elif phase == 2:

            if trend == 1:
                # ---- Bullish -----------------------------------------------
                # Track running high (ms.main)
                if h >= ms_main:
                    ms_main = h

                # BOS setup: crossdn + 2 consecutive bearish closes
                if ms_bos is None and crossdn and c < o and p_c < p_o:
                    ms_bos = ms_main

                if ms_bos is not None:
                    if h >= ms_bos and c <= ms_bos:
                        # Upsweep: wick above BOS then close back below
                        last_sweep = True
                        ms_bos = h          # update to new wick extreme
                    elif c >= ms_bos:
                        # BOS break: bullish continuation → new demand OB
                        ob = _build_ob(highs, lows, atr_ob, ms_seg, i + 1, use_max=False, ob_length=length)
                        if ob:
                            bull_obs.append(ob)
                        # New CHoCH = lowest low in segment
                        ms_choch = _extreme_val(lows, ms_seg, i + 1, use_max=False)
                        ms_bos   = None
                        ms_seg   = i

                # CHoCH: close below invalidation → flip to bearish
                if c <= ms_choch:
                    trend   = -1
                    ob = _build_ob(highs, lows, atr_ob, ms_seg, i + 1, use_max=True, ob_length=length)
                    if ob:
                        bear_obs.append(ob)
                    ms_choch = ms_bos if ms_bos is not None else ms_choch
                    ms_bos   = None
                    ms_main  = l
                    ms_seg   = i
                elif l <= ms_choch and c >= ms_choch:
                    # Dnsweep: wick below CHoCH but close back above
                    last_sweep = True
                    ms_choch   = l

            else:
                # ---- Bearish -----------------------------------------------
                # Track running low (ms.main)
                if l <= ms_main:
                    ms_main = l

                # BOS setup: crossup + 2 consecutive bullish closes
                if ms_bos is None and crossup and c > o and p_c > p_o:
                    ms_bos = ms_main

                if ms_bos is not None:
                    if l <= ms_bos and c >= ms_bos:
                        # Dnsweep: wick below BOS then close back above
                        last_sweep = True
                        ms_bos = l
                    elif c <= ms_bos:
                        # BOS break: bearish continuation → new supply OB
                        ob = _build_ob(highs, lows, atr_ob, ms_seg, i + 1, use_max=True, ob_length=length)
                        if ob:
                            bear_obs.append(ob)
                        # New CHoCH = highest high in segment
                        ms_choch = _extreme_val(highs, ms_seg, i + 1, use_max=True)
                        ms_bos   = None
                        ms_seg   = i

                # CHoCH: close above invalidation → flip to bullish
                if c >= ms_choch:
                    trend   = 1
                    ob = _build_ob(highs, lows, atr_ob, ms_seg, i + 1, use_max=False, ob_length=length)
                    if ob:
                        bull_obs.append(ob)
                    ms_choch = ms_bos if ms_bos is not None else ms_choch
                    ms_bos   = None
                    ms_main  = h
                    ms_seg   = i
                elif h >= ms_choch and c <= ms_choch:
                    # Upsweep: wick above CHoCH but close back below
                    last_sweep = True
                    ms_choch   = h

        # ── Mitigation: BigBeluga "Close" method ───────────────────────────
        # Remove bullish OB when min(close, open) closes below ob.btm
        bull_obs = [ob for ob in bull_obs if min(c, o) >= ob["btm"]]
        # Remove bearish OB when max(close, open) closes above ob.top
        bear_obs = [ob for ob in bear_obs if max(c, o) <= ob["top"]]

        # Keep most recent ob_limit per side
        if len(bull_obs) > ob_limit:
            bull_obs = bull_obs[-ob_limit:]
        if len(bear_obs) > ob_limit:
            bear_obs = bear_obs[-ob_limit:]

    if trend == 0:
        return None

    return {
        "trend":       trend,
        "bull_obs":    bull_obs,
        "bear_obs":    bear_obs,
        "last_sweep":  last_sweep,
        "bos_level":   ms_bos,
        "choch_level": ms_choch,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fair Value Gaps (BigBeluga dFVG logic)
# ─────────────────────────────────────────────────────────────────────────────

def detect_fvg(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    threshold: float = 0.0005,
) -> list[dict]:
    """Detect Fair Value Gaps on confirmed bars (skips last forming bar).

    Bullish FVG : lows[i+2] > highs[i]   — gap above bar i
    Bearish FVG : highs[i+2] < lows[i]   — gap below bar i

    threshold: minimum gap / closes[i] to filter noise (default 0.05%).
    Returns the 10 most recent FVGs (both types combined).
    """
    n = len(highs)
    fvgs: list[dict] = []

    # Stop at n-3 so i+2 is a confirmed bar (not the last/forming bar)
    for i in range(n - 3):
        ref = closes[i] if closes[i] > 0 else 1.0

        # Bullish FVG
        gap_up = lows[i + 2] - highs[i]
        if gap_up > 0 and gap_up / ref >= threshold:
            fvgs.append({
                "btm": highs[i],
                "top": lows[i + 2],
                "avg": (highs[i] + lows[i + 2]) / 2.0,
                "bull": True,
                "idx":  i,
            })

        # Bearish FVG
        gap_dn = lows[i] - highs[i + 2]
        if gap_dn > 0 and gap_dn / ref >= threshold:
            fvgs.append({
                "btm": highs[i + 2],
                "top": lows[i],
                "avg": (highs[i + 2] + lows[i]) / 2.0,
                "bull": False,
                "idx":  i,
            })

    return fvgs[-10:]
