"""
Reporting layer — converts raw Rust results into metrics, equity curves,
monthly heatmaps, and exportable data structures.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# ── Metrics ───────────────────────────────────────────────────────────────────

RISK_FREE_RATE = 0.06  # India T-bill approximation


def compute_metrics(trades: list[dict], equity_curve: list[dict], initial_capital: float) -> dict:
    """Compute all performance metrics in INDEX POINTS (not INR).

    Each trade's P&L in points = (exit - entry) * direction.
    The `pnl` field in trades is in INR (points × lot_size × qty), so we
    convert back to points for display.
    """
    if not trades or not equity_curve:
        return _empty_metrics(initial_capital)

    # P&L in index points per trade
    def _pnl_pts(t: dict) -> float:
        d = t.get("direction", 1)
        return (t.get("exit_price", 0) - t.get("entry_price", 0)) * d

    pnls = [_pnl_pts(t) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_trades = len(pnls)

    total_pnl = sum(pnls)
    final_capital = initial_capital + sum(t["pnl"] for t in trades)  # INR for CAGR
    total_return_pct = (sum(t["pnl"] for t in trades) / initial_capital * 100) if initial_capital > 0 else 0.0

    # CAGR (still on capital basis for ratio comparability)
    start_ts = equity_curve[0]["timestamp"]
    end_ts = equity_curve[-1]["timestamp"]
    years = (end_ts - start_ts) / (365.25 * 86400)
    cagr = 0.0
    if years > 0 and initial_capital > 0 and final_capital > 0:
        cagr = ((final_capital / initial_capital) ** (1.0 / years) - 1.0) * 100.0

    # Drawdown in points (cumulative P&L drawdown)
    cum = 0.0
    peak = 0.0
    max_dd_pts = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd_pts:
            max_dd_pts = dd

    max_dd_pct = max((s["drawdown_pct"] for s in equity_curve), default=0.0)

    # Sharpe & Sortino (on daily returns)
    daily_returns = _to_daily_returns(equity_curve, initial_capital)
    sharpe = _sharpe(daily_returns)
    sortino = _sortino(daily_returns)
    calmar = (cagr / max_dd_pct) if max_dd_pct > 0 else 0.0

    # Trade stats
    win_rate = len(wins) / total_trades * 100.0 if total_trades > 0 else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    avg_wl_ratio = avg_win / avg_loss if avg_loss > 0 else 0.0

    max_consec_wins = _max_consecutive(pnls, positive=True)
    max_consec_losses = _max_consecutive(pnls, positive=False)

    # Daily P&L in points
    daily_pts: dict[str, float] = defaultdict(float)
    for t, p in zip(trades, pnls):
        ts_val = t.get("exit_bar_ts", 0)
        if ts_val:
            _IST = timezone(timedelta(hours=5, minutes=30))
            day_str = datetime.fromtimestamp(ts_val, tz=_IST).strftime("%Y-%m-%d")
            daily_pts[day_str] += p
    daily_pt_values = list(daily_pts.values())
    best_day = max(daily_pt_values) if daily_pt_values else 0.0
    worst_day = min(daily_pt_values) if daily_pt_values else 0.0

    return {
        "total_return_pct": round(total_return_pct, 2),
        "total_return_inr": round(total_pnl, 2),   # points (frontend reads this as pts)
        "final_capital": round(final_capital, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "max_drawdown_inr": round(max_dd_pts, 2),  # points (max drawdown in index pts)
        "sharpe_ratio": round(sharpe, 3),
        "sortino_ratio": round(sortino, 3),
        "calmar_ratio": round(calmar, 3),
        "total_trades": total_trades,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else 9999.0,
        "avg_win_inr": round(avg_win, 2),           # avg win in points
        "avg_loss_inr": round(avg_loss, 2),          # avg loss in points
        "avg_win_loss_ratio": round(avg_wl_ratio, 3),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "best_day_inr": round(best_day, 2),          # best day in points
        "worst_day_inr": round(worst_day, 2),         # worst day in points
    }


def _pnl_pts(t: dict) -> float:
    """P&L in index points for a single trade."""
    d = t.get("direction", 1)
    return (t.get("exit_price", 0) - t.get("entry_price", 0)) * d


def compute_monthly_pnl(trades: list[dict]) -> list[dict]:
    """Group trade P&L (in index points) into month buckets for heatmap display."""
    _IST = timezone(timedelta(hours=5, minutes=30))
    monthly: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = t.get("exit_bar_ts", 0)
        if ts:
            dt = datetime.fromtimestamp(ts, tz=_IST)
            key = f"{dt.year}-{dt.month:02d}"
            monthly[key] += _pnl_pts(t)
    return [{"month": k, "pnl": round(v, 2)} for k, v in sorted(monthly.items())]


def compute_daily_pnl(trades: list[dict]) -> list[dict]:
    """Group trade P&L (in index points) by trading day."""
    _IST = timezone(timedelta(hours=5, minutes=30))
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = t.get("exit_bar_ts", 0)
        if ts:
            dt = datetime.fromtimestamp(ts, tz=_IST)
            key = dt.strftime("%Y-%m-%d")
            daily[key] += _pnl_pts(t)
    return [{"date": k, "pnl": round(v, 2)} for k, v in sorted(daily.items())]


def build_full_result(raw: dict, config_dict: dict) -> dict:
    """Build the complete BacktestResult dict from raw Rust output + config."""
    trades = raw.get("trades", [])
    equity_curve = raw.get("equity_curve", [])
    initial_capital = raw.get("initial_capital", 0.0)
    strategy_names = raw.get("strategy_names", [])

    # Overall metrics
    metrics = compute_metrics(trades, equity_curve, initial_capital)

    # Per-strategy metrics
    per_strategy_metrics = {}
    per_strategy_equity = raw.get("per_strategy_equity", {})
    per_strategy_trades = raw.get("per_strategy_trades", {})

    for name in strategy_names:
        s_trades = per_strategy_trades.get(name, [])
        # Use per-strategy equity if available, otherwise fall back to global equity
        s_equity = per_strategy_equity.get(name, equity_curve)
        # Find capital allocation for this strategy
        s_initial = initial_capital / max(len(strategy_names), 1)
        for sc in config_dict.get("strategies", []):
            if sc.get("effective_name") == name or sc.get("strategy_name") == name:
                s_initial = sc.get("capital_allocation", s_initial)
                break
        per_strategy_metrics[name] = compute_metrics(s_trades, s_equity, s_initial)

    # Equity curve with human-readable dates
    equity_with_dates = []
    for snap in equity_curve:
        ts = snap["timestamp"]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        equity_with_dates.append({
            "timestamp": ts,
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "equity": round(snap["equity"], 2),
            "drawdown_pct": round(snap["drawdown_pct"], 2),
        })

    # Trades with dates + index-point fields
    trades_with_dates = []
    for t in trades:
        ts = t.get("exit_bar_ts", 0)
        _IST = timezone(timedelta(hours=5, minutes=30))
        dt = datetime.fromtimestamp(ts, tz=_IST) if ts else None
        direction = t.get("direction", 1)
        entry = t.get("entry_price", 0)
        exit_p = t.get("exit_price", 0)
        sl = t.get("stop_loss", 0)
        tgt = t.get("target", 0)

        # P&L in index points (raw, before lot multiplication)
        pnl_pts = (exit_p - entry) if direction == 1 else (entry - exit_p)
        # SL/TP distance in points
        sl_pts = (entry - sl) if direction == 1 else (sl - entry) if sl else 0
        tp_pts = (tgt - entry) if direction == 1 else (entry - tgt) if tgt else 0
        rr_ratio = f"{tp_pts / sl_pts:.1f}" if sl_pts > 0 else "N/A"

        trades_with_dates.append({
            **t,
            "date": dt.strftime("%Y-%m-%d") if dt else "",
            "time": dt.strftime("%H:%M") if dt else "",
            "direction_label": "BUY" if direction == 1 else "SELL",
            "pnl_rounded": round(t.get("pnl", 0), 2),
            "pnl_pts": round(pnl_pts, 2),
            "sl_pts": round(sl_pts, 1),
            "tp_pts": round(tp_pts, 1),
            "rr_ratio": rr_ratio,
            "hold_candles": t.get("hold_candles", 0),
        })

    monthly_pnl = compute_monthly_pnl(trades)
    daily_pnl = compute_daily_pnl(trades)

    # Benchmark (buy-and-hold approximation from equity curve start/end prices)
    # We don't have separate index price here, so skip benchmark for now
    # (can be added when separate benchmark data is provided)

    return {
        "metrics": metrics,
        "per_strategy_metrics": per_strategy_metrics,
        "equity_curve": equity_with_dates,
        "per_strategy_equity": per_strategy_equity,
        "trades": trades_with_dates,
        "monthly_pnl": monthly_pnl,
        "daily_pnl": daily_pnl,
        "strategy_names": strategy_names,
        "initial_capital": initial_capital,
        "start_ts": raw.get("start_ts", 0),
        "end_ts": raw.get("end_ts", 0),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _empty_metrics(initial_capital: float) -> dict:
    return {
        "total_return_pct": 0.0, "total_return_inr": 0.0, "final_capital": initial_capital,
        "cagr_pct": 0.0, "max_drawdown_pct": 0.0, "max_drawdown_inr": 0.0,
        "sharpe_ratio": 0.0, "sortino_ratio": 0.0, "calmar_ratio": 0.0,
        "total_trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0,
        "avg_win_inr": 0.0, "avg_loss_inr": 0.0, "avg_win_loss_ratio": 0.0,
        "max_consecutive_wins": 0, "max_consecutive_losses": 0,
        "best_day_inr": 0.0, "worst_day_inr": 0.0,
    }


def _to_daily_returns(equity_curve: list[dict], initial_capital: float) -> list[float]:
    """Compute daily returns from equity snapshots."""
    daily: dict[str, float] = {}
    for snap in equity_curve:
        ts = snap["timestamp"]
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        day_key = dt.strftime("%Y-%m-%d")
        daily[day_key] = snap["equity"]

    sorted_days = sorted(daily.items())
    if len(sorted_days) < 2:
        return []

    returns = []
    prev_eq = initial_capital
    for _, eq in sorted_days:
        if prev_eq > 0:
            returns.append((eq - prev_eq) / prev_eq)
        prev_eq = eq
    return returns


def _sharpe(daily_returns: list[float]) -> float:
    if len(daily_returns) < 10:
        return 0.0
    n = len(daily_returns)
    mean = sum(daily_returns) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / n
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    daily_rf = RISK_FREE_RATE / 252.0
    return (mean - daily_rf) / std * math.sqrt(252)


def _sortino(daily_returns: list[float]) -> float:
    if len(daily_returns) < 10:
        return 0.0
    daily_rf = RISK_FREE_RATE / 252.0
    excess = [r - daily_rf for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    downside = [min(r, 0.0) for r in excess]
    downside_var = sum(r ** 2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var)
    if downside_std == 0:
        return 0.0
    return mean_excess / downside_std * math.sqrt(252)


def _max_consecutive(pnls: list[float], positive: bool) -> int:
    max_run, current = 0, 0
    for p in pnls:
        if (positive and p > 0) or (not positive and p < 0):
            current += 1
            max_run = max(max_run, current)
        else:
            current = 0
    return max_run


def _group_daily_pnl(trades: list[dict]) -> dict[str, float]:
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = t.get("exit_bar_ts", 0)
        if ts:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            daily[dt.strftime("%Y-%m-%d")] += t["pnl"]
    return dict(daily)
