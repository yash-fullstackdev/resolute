"""Smart Money Concepts helper functions.

detect_bos_choch — Break of Structure / Change of Character
detect_fvg       — Fair Value Gap detection
find_order_blocks — Unmitigated Order Block finder
"""

from __future__ import annotations


# ─────────────────────────────────────────────────────────────────────────────
# Swing high / low detection
# ─────────────────────────────────────────────────────────────────────────────

def _swing_highs_lows(
    highs: list[float],
    lows: list[float],
    length: int,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Detect pivot swing highs and lows.

    Returns (swing_highs, swing_lows) as lists of (index, price).
    Requires `length` bars on each side to qualify a pivot.
    """
    n = len(highs)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    for i in range(length, n - length):
        if all(highs[i] >= highs[i - j] for j in range(1, length + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, length + 1)):
            swing_highs.append((i, highs[i]))
        if all(lows[i] <= lows[i - j] for j in range(1, length + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, length + 1)):
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows


# ─────────────────────────────────────────────────────────────────────────────
# BOS / CHoCH
# ─────────────────────────────────────────────────────────────────────────────

def detect_bos_choch(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    length: int = 6,
) -> dict | None:
    """Detect Break of Structure / Change of Character.

    Returns dict:
      trend: 1 (bullish), -1 (bearish), 0 (neutral)
      last_bos: price level of most recent BOS
      last_sweep: bool — liquidity sweep on last 3 bars
      swing_highs: list of (idx, price) for downstream use
      swing_lows:  list of (idx, price) for downstream use
    """
    n = len(highs)
    min_bars = length * 3 + 5
    if n < min_bars:
        return None

    swing_highs, swing_lows = _swing_highs_lows(highs, lows, length)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    curr_close = closes[-1]
    trend = 0
    last_bos = 0.0
    last_sweep = False

    last_sh_idx, last_sh_price = swing_highs[-1]
    last_sl_idx, last_sl_price = swing_lows[-1]
    prev_sh_idx, prev_sh_price = swing_highs[-2]
    prev_sl_idx, prev_sl_price = swing_lows[-2]

    # Higher highs + higher lows → bullish structure
    if last_sh_price > prev_sh_price and last_sl_price > prev_sl_price:
        trend = 1
        last_bos = last_sh_price
    # Lower highs + lower lows → bearish structure
    elif last_sh_price < prev_sh_price and last_sl_price < prev_sl_price:
        trend = -1
        last_bos = last_sl_price

    # Liquidity sweep: wick below recent swing low then close above (bullish sweep)
    recent_low = min(lows[-3:]) if len(lows) >= 3 else lows[-1]
    recent_high = max(highs[-3:]) if len(highs) >= 3 else highs[-1]
    if recent_low < last_sl_price and curr_close > last_sl_price:
        last_sweep = True
    if recent_high > last_sh_price and curr_close < last_sh_price:
        last_sweep = True

    return {
        "trend": trend,
        "last_bos": last_bos,
        "last_sweep": last_sweep,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fair Value Gaps
# ─────────────────────────────────────────────────────────────────────────────

def detect_fvg(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    threshold: float = 0.0005,
) -> list[dict]:
    """Detect Fair Value Gaps (3-bar imbalance patterns).

    Bullish FVG: lows[i+2] > highs[i]
    Bearish FVG: highs[i+2] < lows[i]

    Returns list of {'btm', 'top', 'avg', 'bull', 'idx'} — latest 10 only.
    """
    n = len(highs)
    fvgs: list[dict] = []

    for i in range(n - 2):
        ref = closes[i] if closes[i] > 0 else 1.0

        # Bullish FVG
        gap_up = lows[i + 2] - highs[i]
        if gap_up > 0 and gap_up / ref >= threshold:
            fvgs.append({
                "btm": highs[i],
                "top": lows[i + 2],
                "avg": (highs[i] + lows[i + 2]) / 2.0,
                "bull": True,
                "idx": i,
            })

        # Bearish FVG
        gap_dn = lows[i] - highs[i + 2]
        if gap_dn > 0 and gap_dn / ref >= threshold:
            fvgs.append({
                "btm": highs[i + 2],
                "top": lows[i],
                "avg": (highs[i + 2] + lows[i]) / 2.0,
                "bull": False,
                "idx": i,
            })

    return fvgs[-10:]


# ─────────────────────────────────────────────────────────────────────────────
# Order blocks
# ─────────────────────────────────────────────────────────────────────────────

def find_order_blocks(
    data_5m: dict,
    state: dict,
    length: int = 6,
    limit: int = 5,
) -> list[dict]:
    """Find unmitigated Order Blocks aligned with the current trend.

    Bullish OB: last bearish candle before a bullish impulse, near a swing low.
    Bearish OB: last bullish candle before a bearish impulse, near a swing high.

    Returns list of {'btm', 'top', 'avg', 'bull', 'idx'}.
    """
    highs = data_5m.get("high", [])
    lows = data_5m.get("low", [])
    closes = data_5m.get("close", [])
    opens = data_5m.get("open", closes)
    n = len(closes)

    if n < length + 5:
        return []

    trend = state.get("trend", 0)
    swing_highs: list[tuple[int, float]] = state.get("swing_highs", [])
    swing_lows: list[tuple[int, float]] = state.get("swing_lows", [])
    curr_price = closes[-1]
    obs: list[dict] = []

    if trend == 1:
        # Bullish: last bearish candle at/near each swing low
        for sl_idx, sl_price in reversed(swing_lows):
            search_start = min(sl_idx, n - 1)
            search_end = max(sl_idx - length, 1)
            for j in range(search_start, search_end - 1, -1):
                if j >= n:
                    continue
                o = opens[j] if j < len(opens) else closes[j]
                # Bearish candle
                if closes[j] < o:
                    ob_top = highs[j]
                    ob_btm = lows[j]
                    ob_avg = (ob_top + ob_btm) / 2.0
                    # Unmitigated: price is above the OB low and within reach
                    if curr_price > ob_btm and curr_price < ob_top * 1.05:
                        obs.append({
                            "btm": ob_btm,
                            "top": ob_top,
                            "avg": ob_avg,
                            "bull": True,
                            "idx": j,
                        })
                    break
            if len(obs) >= limit:
                break

    elif trend == -1:
        # Bearish: last bullish candle at/near each swing high
        for sh_idx, sh_price in reversed(swing_highs):
            search_start = min(sh_idx, n - 1)
            search_end = max(sh_idx - length, 1)
            for j in range(search_start, search_end - 1, -1):
                if j >= n:
                    continue
                o = opens[j] if j < len(opens) else closes[j]
                # Bullish candle
                if closes[j] > o:
                    ob_top = highs[j]
                    ob_btm = lows[j]
                    ob_avg = (ob_top + ob_btm) / 2.0
                    # Unmitigated: price is below OB top and within reach
                    if curr_price < ob_top and curr_price > ob_btm * 0.95:
                        obs.append({
                            "btm": ob_btm,
                            "top": ob_top,
                            "avg": ob_avg,
                            "bull": False,
                            "idx": j,
                        })
                    break
            if len(obs) >= limit:
                break

    return obs[:limit]
