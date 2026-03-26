"""
EarlyMomentumStrategy — 1:1 port of dhan-trader Rust signal engine.

Trades NSE equities in the first 10 minutes of market open based on
price momentum, volume, OI, VWAP cross, gap continuation, and spread.

10 indicators scored identically to signal_engine.rs compute_signal().
Exit logic ports exit_manager.rs check_exit() exactly.
Dynamic quantity ports dynamic_qty.rs compute_quantity() exactly.
"""

from __future__ import annotations

import math
import structlog

from .equity_base import BaseEquityStrategy, EquitySignal, EquityPosition, EquityExitResult

logger = structlog.get_logger(service="user_worker_pool", module="early_momentum")


# ---------------------------------------------------------------------------
# Default config — matches types.rs SignalConfig::default() exactly
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    # Legacy (shared) params
    "entry_bucket_start": 4,
    "entry_bucket_end": 10,
    "min_move_pct": 0.7,
    "min_volume": 5000,
    "min_score": 7,
    "tp_pct": 1.0,
    "sl_pct": 0.4,
    "hard_exit_bucket": 46,
    "quantity": 1,
    "gap_filter_min_pct": -3.5,
    "gap_filter_max_pct": 100.0,
    "sell_gap_min_pct": -1.0,
    "min_vol_rate": 150.0,
    "sell_hard_exit_bucket": 76,
    "buy_gap_max_pct": 3.0,
    "direction_filter": "BOTH",
    "capital_per_trade": 10000,
    # Per-direction TP/SL
    "buy_tp_pct": 0.0,
    "buy_sl_pct": 0.0,
    "sell_tp_pct": 0.0,
    "sell_sl_pct": 0.0,
    # Per-direction min move
    "buy_min_move_pct": 0.15,
    "sell_min_move_pct": 0.15,
    # Per-direction vol rate
    "buy_min_vol_rate": 150.0,
    "sell_min_vol_rate": 150.0,
    # Per-direction capital
    "buy_capital_per_trade": 10000,
    "sell_capital_per_trade": 10000,
    # Per-direction qty multiplier
    "buy_qty_multiplier": 1.0,
    "sell_qty_multiplier": 1.0,
    # Per-direction entry window
    "buy_entry_start": 2,
    "buy_entry_end": 3,
    "sell_entry_start": 2,
    "sell_entry_end": 4,
    # Per-direction volume
    "buy_min_volume": 100,
    "sell_min_volume": 100,
    # Per-direction score
    "buy_min_score": 3,
    "sell_min_score": 3,
    # Per-direction gap
    "buy_gap_min_pct": 0.0,
    "sell_gap_max_pct": 100.0,
    # Instruments list (populated per-instance)
    "instruments": [],
}


# ---------------------------------------------------------------------------
# Helpers — exact ports from Rust
# ---------------------------------------------------------------------------

def _ols_slope(y: list[float]) -> float:
    """OLS slope of y values (x = indices 0,1,2,...). Port of signal_engine.rs ols_slope."""
    n = len(y)
    if n < 2:
        return 0.0
    n_f = float(n)
    x_mean = (n_f - 1.0) / 2.0
    y_mean = sum(y) / n_f
    num = sum((i - x_mean) * (yi - y_mean) for i, yi in enumerate(y))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0.0:
        return 0.0
    return num / den


def _confidence_score(
    entry_price: float,
    volume_cum: int,
    volume_rate: float,
    morning_range_pct: float,
    move_pct: float,
) -> int:
    """Confidence score (0-6). Port of dynamic_qty.rs confidence_score."""
    score = 0
    if entry_price < 1000.0:
        score += 1
    if 50_000 <= volume_cum <= 500_000:
        score += 1
    if 200_000 <= volume_cum <= 500_000:
        score += 1
    if volume_rate >= 500.0:
        score += 1
    if morning_range_pct >= 0.5:
        score += 1
    if abs(move_pct) < 1.0:
        score += 1
    return score


def _compute_quantity(
    base_qty: int,
    direction_multiplier: float,
    entry_price: float,
    volume_cum: int,
    volume_rate: float,
    morning_range_pct: float,
    move_pct: float,
) -> int:
    """Dynamic quantity. Port of dynamic_qty.rs compute_quantity."""
    score = _confidence_score(entry_price, volume_cum, volume_rate, morning_range_pct, move_pct)

    if score <= 2:
        return 0  # weak signal — skip

    if score <= 4:
        confidence_mult = 1.0
    elif score == 5:
        confidence_mult = 1.5
    else:
        confidence_mult = 2.0  # score 6

    qty = round(base_qty * confidence_mult * direction_multiplier)
    return max(1, qty)


def _morning_range_pct(snapshots: list[dict], entry_bucket: int) -> float:
    """Morning range % from snapshots up to entry bucket. Port of dynamic_qty.rs morning_range_pct."""
    morning = [
        s["ltp"]
        for s in snapshots
        if s.get("bucket", 0) >= 1 and s.get("bucket", 0) <= entry_bucket and s.get("ltp", 0) > 0
    ]
    if not morning:
        return 0.0
    max_p = max(morning)
    min_p = min(morning)
    avg_p = sum(morning) / len(morning)
    if avg_p <= 0.0:
        return 0.0
    return (max_p - min_p) / avg_p * 100.0


# ---------------------------------------------------------------------------
# Strategy implementation
# ---------------------------------------------------------------------------

class EarlyMomentumStrategy(BaseEquityStrategy):
    """Early momentum equity strategy — 1:1 port of dhan-trader Rust engine.

    Scores 10 indicators in the first minutes of market open,
    computes dynamic quantity via confidence scoring,
    and exits via TP/SL/time priority.
    """

    name = "early_momentum"
    category = "HYBRID"
    min_capital_tier = "GROWTH"
    DEFAULT_CONFIG = DEFAULT_CONFIG

    # ------------------------------------------------------------------
    # evaluate_signal — port of signal_engine.rs compute_signal()
    # ------------------------------------------------------------------

    def evaluate_signal(
        self,
        snapshot: dict,
        bucket: int,
        direction: str,
        config: dict,
        fired_today: set,
        open_positions: dict,
        *,
        snapshot_history: list[dict] | None = None,
    ) -> EquitySignal | None:
        snapshots = snapshot_history or []
        symbol = snapshot.get("symbol", "")
        security_id = snapshot.get("security_id", "")

        # --- Pre-checks ---

        # Already fired today for this symbol+direction
        fire_key = f"{symbol}_{direction}"
        if fire_key in fired_today:
            return None

        # Already have open position for this symbol
        if symbol in open_positions:
            return None

        # Direction filter
        dir_filter = config.get("direction_filter", "BOTH")
        if dir_filter != "BOTH" and dir_filter != direction:
            return None

        # Need at least 2 snapshots
        if len(snapshots) < 2:
            return None

        # --- Gap filter (global) ---
        gap_pct = snapshot.get("gap_pct", 0.0)
        gap_filter_min = config.get("gap_filter_min_pct", -3.5)
        gap_filter_max = config.get("gap_filter_max_pct", 100.0)
        if gap_pct != 0.0 and (gap_pct < gap_filter_min or gap_pct > gap_filter_max):
            self._log_skip(symbol, direction, bucket, f"gap_filter:{gap_pct:.2f}")
            return None

        # --- Open price (bucket >= 1) ---
        open_snap = None
        for s in snapshots:
            if s.get("bucket", 0) >= 1:
                open_snap = s
                break
        if open_snap is None:
            open_snap = snapshots[0]
        open_price = open_snap.get("ltp", 0.0)
        if open_price <= 0.0:
            return None

        # --- Widest entry window (union of buy/sell) to determine direction ---
        buy_entry_start = config.get("buy_entry_start", 2)
        buy_entry_end = config.get("buy_entry_end", 3)
        sell_entry_start = config.get("sell_entry_start", 2)
        sell_entry_end = config.get("sell_entry_end", 4)

        wide_start = min(buy_entry_start, sell_entry_start)
        wide_end = max(buy_entry_end, sell_entry_end)

        wide_snaps = [s for s in snapshots if wide_start <= s.get("bucket", 0) <= wide_end]
        if not wide_snaps:
            return None

        wide_last = wide_snaps[-1]
        move_pct = (wide_last["ltp"] - open_price) / open_price * 100.0

        # Determine direction from move
        computed_direction = "BUY" if move_pct > 0.0 else "SELL"
        if computed_direction != direction:
            return None

        dir_sign = 1.0 if direction == "BUY" else -1.0

        # --- Per-direction entry window ---
        if direction == "BUY":
            dir_entry_start = buy_entry_start
            dir_entry_end = buy_entry_end
        else:
            dir_entry_start = sell_entry_start
            dir_entry_end = sell_entry_end

        # Current bucket must be within entry window
        if bucket < dir_entry_start or bucket > dir_entry_end:
            return None

        entry_snaps = [s for s in snapshots if dir_entry_start <= s.get("bucket", 0) <= dir_entry_end]
        if not entry_snaps:
            return None

        last = entry_snaps[-1]

        # --- Per-direction min_move filter ---
        dir_move_pct = (last["ltp"] - open_price) / open_price * 100.0
        if direction == "BUY":
            dir_min_move = config.get("buy_min_move_pct", 0.15)
        else:
            dir_min_move = config.get("sell_min_move_pct", 0.15)

        if abs(dir_move_pct) < dir_min_move:
            self._log_skip(symbol, direction, bucket, f"min_move:{abs(dir_move_pct):.3f}<{dir_min_move}")
            return None

        # --- SELL-specific gap filter ---
        if direction == "SELL" and gap_pct != 0.0:
            sell_gap_min = config.get("sell_gap_min_pct", -1.0)
            if gap_pct < sell_gap_min:
                self._log_skip(symbol, direction, bucket, f"sell_gap_min:{gap_pct:.2f}<{sell_gap_min}")
                return None

        # --- BUY-specific gap filter (max) ---
        if direction == "BUY" and gap_pct != 0.0:
            buy_gap_max = config.get("buy_gap_max_pct", 3.0)
            if gap_pct > buy_gap_max:
                self._log_skip(symbol, direction, bucket, f"buy_gap_max:{gap_pct:.2f}>{buy_gap_max}")
                return None

        # --- BUY gap_min filter ---
        if direction == "BUY" and gap_pct != 0.0:
            buy_gap_min = config.get("buy_gap_min_pct", 0.0)
            if gap_pct < buy_gap_min:
                self._log_skip(symbol, direction, bucket, f"buy_gap_min:{gap_pct:.2f}<{buy_gap_min}")
                return None

        # --- SELL gap_max filter ---
        if direction == "SELL" and gap_pct != 0.0:
            sell_gap_max = config.get("sell_gap_max_pct", 100.0)
            if gap_pct > sell_gap_max:
                self._log_skip(symbol, direction, bucket, f"sell_gap_max:{gap_pct:.2f}>{sell_gap_max}")
                return None

        # --- Per-direction volume rate filter ---
        if direction == "BUY":
            dir_min_vol_rate = config.get("buy_min_vol_rate", 150.0)
        else:
            dir_min_vol_rate = config.get("sell_min_vol_rate", 150.0)

        last_vol_rate = last.get("volume_rate", 0.0)
        if last_vol_rate < dir_min_vol_rate:
            self._log_skip(symbol, direction, bucket, f"vol_rate:{last_vol_rate:.1f}<{dir_min_vol_rate}")
            return None

        # --- Volume in entry window ---
        vol_entry = sum(s.get("volume_delta", 0) for s in entry_snaps)

        # --- OI indicators REMOVED ---
        # OI data is not available from equity poller (Dhan quote API
        # does not return real-time OI for equity cash segment).
        # Removed: oiDir, oiSpike, oiAcc (3 indicators, 3 points max)

        # --- VWAP cross ---
        vwap_cross = False
        for s in entry_snaps:
            vwap = s.get("vwap", 0.0)
            if vwap <= 0.0:
                continue
            if direction == "BUY" and s.get("ltp", 0.0) > vwap:
                vwap_cross = True
                break
            if direction == "SELL" and s.get("ltp", 0.0) < vwap:
                vwap_cross = True
                break

        # --- Gap continuation ---
        gap_continuation = abs(gap_pct) > 0.3 and (gap_pct * dir_sign) > 0.0

        # --- Candle body ---
        candle_body = last.get("candle_body_ratio", 0.0) > 0.6

        # --- Spread penalty ---
        spread_bad = last.get("spread_pct", 0.0) > 0.15

        # ----------------------------------------------------------------
        # SCORING — exact match of signal_engine.rs
        # ----------------------------------------------------------------
        score: int = 0
        fired: list[str] = []

        min_move_pct_cfg = config.get("min_move_pct", 0.7)

        if abs(move_pct) >= min_move_pct_cfg:
            score += 2
            fired.append("pm✓")
        if abs(move_pct) >= min_move_pct_cfg * 2.0:
            score += 2
            fired.append("pm2✓")

        if direction == "BUY":
            dir_min_volume = config.get("buy_min_volume", 100)
        else:
            dir_min_volume = config.get("sell_min_volume", 100)

        if vol_entry >= dir_min_volume:
            score += 1
            fired.append("vol✓")
        if vol_entry >= dir_min_volume * 2:
            score += 2
            fired.append("vol2✓")

        if vwap_cross:
            score += 1
            fired.append("vwap✓")
        if gap_continuation:
            score += 1
            fired.append("gap✓")
        if candle_body:
            score += 1
            fired.append("body✓")
        if spread_bad:
            score -= 2
            fired.append("spread✗")

        score = max(0, score)

        # Per-direction min_score
        if direction == "BUY":
            dir_min_score = config.get("buy_min_score", 3)
        else:
            dir_min_score = config.get("sell_min_score", 3)

        if score < dir_min_score:
            self._log_skip(symbol, direction, bucket, f"score_below_min:{score}<{dir_min_score}")
            return None

        entry_price = last["ltp"]

        # ----------------------------------------------------------------
        # Dynamic quantity — port of dynamic_qty.rs
        # ----------------------------------------------------------------
        if direction == "BUY":
            dir_multiplier = config.get("buy_qty_multiplier", 1.0)
        else:
            dir_multiplier = config.get("sell_qty_multiplier", 1.0)

        vol_cum = last.get("volume_cum", 0)
        morning_range = _morning_range_pct(snapshots, last.get("bucket", bucket))

        base_qty = config.get("quantity", 1)

        # Capital-based quantity override
        capital_per_trade = config.get("capital_per_trade", 0)
        if capital_per_trade > 0 and entry_price > 0:
            base_qty = math.floor(capital_per_trade / entry_price)
            if base_qty < 1:
                base_qty = 1

        qty = _compute_quantity(
            base_qty, dir_multiplier, entry_price, vol_cum,
            last_vol_rate, morning_range, move_pct,
        )
        if qty == 0:
            self._log_skip(symbol, direction, bucket, "dynamic_qty_zero")
            return None

        # ----------------------------------------------------------------
        # TP / SL prices — per-direction, port of signal_engine.rs
        # ----------------------------------------------------------------
        if direction == "BUY":
            dir_tp_pct = config.get("buy_tp_pct", 0.0)
            dir_sl_pct = config.get("buy_sl_pct", 0.0)
        else:
            dir_tp_pct = config.get("sell_tp_pct", 0.0)
            dir_sl_pct = config.get("sell_sl_pct", 0.0)

        # Fallback to shared tp/sl if per-direction is 0
        if dir_tp_pct == 0.0:
            dir_tp_pct = config.get("tp_pct", 1.0)
        if dir_sl_pct == 0.0:
            dir_sl_pct = config.get("sl_pct", 0.4)

        tp_price = entry_price * (1.0 + dir_sign * dir_tp_pct / 100.0)
        sl_price = entry_price * (1.0 - dir_sign * dir_sl_pct / 100.0)

        # Hard exit bucket
        if direction == "BUY":
            hard_exit = config.get("hard_exit_bucket", 46)
        else:
            hard_exit = config.get("sell_hard_exit_bucket", 76)

        return EquitySignal(
            strategy_name="early_momentum",
            symbol=symbol,
            security_id=security_id,
            direction=direction,
            entry_price=entry_price,
            entry_bucket=last.get("bucket", bucket),
            score=score,
            signals_fired=fired,
            quantity=qty,
            tp_price=tp_price,
            sl_price=sl_price,
            hard_exit_bucket=hard_exit,
            gap_pct=gap_pct,
            move_pct=move_pct,
            vol_rate=last_vol_rate,
            open_price=open_price,
        )

    # ------------------------------------------------------------------
    # check_exit — port of exit_manager.rs check_exit()
    # ------------------------------------------------------------------

    def check_exit(
        self,
        position: EquityPosition,
        snapshot: dict,
        bucket: int,
        config: dict,
    ) -> EquityExitResult | None:
        current_ltp = snapshot.get("ltp", 0.0)
        if current_ltp <= 0.0:
            return None

        entry_price = position.entry_price
        tp_price = position.tp_price
        sl_price = position.sl_price

        # TP/SL active checks (disabled when price == entry_price, i.e. pct was 0)
        tp_active = abs(tp_price - entry_price) > 0.001
        sl_active = abs(sl_price - entry_price) > 0.001

        # TP check
        if tp_active:
            if position.direction == "BUY" and current_ltp >= tp_price:
                should_exit_tp = True
            elif position.direction == "SELL" and current_ltp <= tp_price:
                should_exit_tp = True
            else:
                should_exit_tp = False
        else:
            should_exit_tp = False

        # SL check
        if sl_active:
            if position.direction == "BUY" and current_ltp <= sl_price:
                should_exit_sl = True
            elif position.direction == "SELL" and current_ltp >= sl_price:
                should_exit_sl = True
            else:
                should_exit_sl = False
        else:
            should_exit_sl = False

        # Time check — per-direction hard exit bucket
        if position.direction == "BUY":
            effective_exit_bucket = config.get("hard_exit_bucket", 46)
        else:
            effective_exit_bucket = config.get("sell_hard_exit_bucket", 76)

        should_exit_time = bucket >= effective_exit_bucket

        # Priority: TP > SL > Time
        if should_exit_tp:
            reason = "TP"
        elif should_exit_sl:
            reason = "SL"
        elif should_exit_time:
            reason = "TIME"
        else:
            return None

        # Return pct and PnL
        if position.direction == "BUY":
            return_pct = (current_ltp - entry_price) / entry_price * 100.0
        else:
            return_pct = (entry_price - current_ltp) / entry_price * 100.0

        pnl_rupees = entry_price * (return_pct / 100.0) * position.quantity

        return EquityExitResult(
            reason=reason,
            exit_price=current_ltp,
            exit_bucket=bucket,
            return_pct=return_pct,
            pnl_rupees=pnl_rupees,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _log_skip(symbol: str, direction: str, bucket: int, reason: str) -> None:
        logger.info(
            "equity_signal_skip",
            symbol=symbol,
            direction=direction,
            bucket=bucket,
            reason=reason,
        )
