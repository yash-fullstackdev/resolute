"use client";

import { BacktestMetrics } from "@/types/backtest";

interface MetricsGridProps {
  metrics: BacktestMetrics;
}

function fmtPts(v: number, sign = false): string {
  const s = sign && v >= 0 ? "+" : "";
  return `${s}${v.toFixed(2)} pts`;
}

function MetricCard({
  label,
  value,
  subValue,
  color,
}: {
  label: string;
  value: string;
  subValue?: string;
  color?: string;
}) {
  return (
    <div className="rounded-xl border border-surface-border bg-surface-dark p-4">
      <p className="text-xs font-medium text-slate-400 uppercase tracking-wide">{label}</p>
      <p className={`mt-1 text-xl font-bold tabular-nums ${color ?? "text-white"}`}>{value}</p>
      {subValue && <p className="mt-0.5 text-xs text-slate-500">{subValue}</p>}
    </div>
  );
}

export function MetricsGrid({ metrics }: MetricsGridProps) {
  const totalPts = metrics.total_return_inr; // backend sends total P&L (used as points)
  const returnColor = totalPts >= 0 ? "text-profit" : "text-loss";
  const ddColor = metrics.max_drawdown_pct > 15 ? "text-loss" : metrics.max_drawdown_pct > 8 ? "text-yellow-400" : "text-white";
  const sharpeColor = metrics.sharpe_ratio >= 1.5 ? "text-profit" : metrics.sharpe_ratio >= 0.8 ? "text-yellow-400" : "text-slate-400";
  const wRateColor = metrics.win_rate_pct >= 55 ? "text-profit" : metrics.win_rate_pct >= 40 ? "text-yellow-400" : "text-loss";

  const primary = [
    {
      label: "Total P&L",
      value: fmtPts(totalPts, true),
      subValue: `${metrics.total_trades} trades`,
      color: returnColor,
    },
    {
      label: "Win Rate",
      value: `${metrics.win_rate_pct.toFixed(1)}%`,
      subValue: `${Math.round(metrics.win_rate_pct * metrics.total_trades / 100)}W / ${metrics.total_trades - Math.round(metrics.win_rate_pct * metrics.total_trades / 100)}L`,
      color: wRateColor,
    },
    {
      label: "Profit Factor",
      value: metrics.profit_factor >= 9999 ? "∞" : metrics.profit_factor.toFixed(2),
      subValue: "Gross profit / loss",
      color: metrics.profit_factor >= 1.5 ? "text-profit" : metrics.profit_factor >= 1 ? "text-yellow-400" : "text-loss",
    },
    {
      label: "Max Drawdown",
      value: fmtPts(-metrics.max_drawdown_inr),
      subValue: `-${metrics.max_drawdown_pct.toFixed(1)}%`,
      color: ddColor,
    },
    {
      label: "Sharpe Ratio",
      value: metrics.sharpe_ratio.toFixed(3),
      subValue: "Annualised (rf=6%)",
      color: sharpeColor,
    },
    {
      label: "Sortino Ratio",
      value: metrics.sortino_ratio.toFixed(3),
      subValue: "Downside deviation",
      color: metrics.sortino_ratio >= 1.5 ? "text-profit" : "text-white",
    },
  ];

  const secondary = [
    { label: "Total Trades", value: String(metrics.total_trades), color: "text-white" },
    { label: "Avg Win", value: fmtPts(metrics.avg_win_inr, true), color: "text-profit" },
    { label: "Avg Loss", value: fmtPts(-metrics.avg_loss_inr), color: "text-loss" },
    {
      label: "Win/Loss Ratio",
      value: metrics.avg_win_loss_ratio.toFixed(2),
      color: metrics.avg_win_loss_ratio >= 1.5 ? "text-profit" : "text-white",
    },
    {
      label: "Max Consec. Wins",
      value: String(metrics.max_consecutive_wins),
      color: "text-profit",
    },
    {
      label: "Max Consec. Losses",
      value: String(metrics.max_consecutive_losses),
      color: "text-loss",
    },
    { label: "Best Day", value: fmtPts(metrics.best_day_inr, true), color: "text-profit" },
    { label: "Worst Day", value: fmtPts(metrics.worst_day_inr), color: "text-loss" },
  ];

  return (
    <div className="space-y-4">
      {/* Primary metrics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        {primary.map((m) => (
          <MetricCard key={m.label} {...m} />
        ))}
      </div>
      {/* Secondary metrics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {secondary.map((m) => (
          <MetricCard key={m.label} {...m} />
        ))}
      </div>
    </div>
  );
}
