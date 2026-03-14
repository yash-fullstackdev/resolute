"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { PnLChart } from "@/components/charts/PnLChart";
import type { ApiResponse } from "@/types/api";
import { formatINR, formatPercentage, pnlColorClass, formatDateOnlyIST } from "@/lib/formatters";
import { TrendingUp, Target, BarChart3, ArrowDownRight } from "lucide-react";
import {
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Tooltip,
} from "recharts";

interface PerformanceStats {
  total_pnl: number;
  win_rate: number;
  total_trades: number;
  avg_return_pct: number;
  max_drawdown_pct: number;
  profit_factor: number;
  sharpe_ratio: number;
  best_day: number;
  worst_day: number;
  winning_trades: number;
  losing_trades: number;
}

interface DailyPnLEntry {
  date: string;
  pnl: number;
  trades: number;
  win_rate: number;
}

export default function PerformancePage() {
  const [period, setPeriod] = useState<"7d" | "30d" | "90d" | "all">("30d");

  const daysMap: Record<string, number> = { "7d": 7, "30d": 30, "90d": 90, all: 365 };

  const { data: stats } = useQuery<PerformanceStats>({
    queryKey: ["performance-stats", period],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<PerformanceStats>>("/performance", {
        params: { days: daysMap[period] },
      });
      return res.data.data;
    },
  });

  const { data: dailyPnl } = useQuery<DailyPnLEntry[]>({
    queryKey: ["daily-pnl", period],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<DailyPnLEntry[]>>("/performance/daily", {
        params: { days: daysMap[period] },
      });
      return res.data.data;
    },
  });

  const winLossData = stats
    ? [
        { name: "Wins", value: stats.winning_trades, color: "#10b981" },
        { name: "Losses", value: stats.losing_trades, color: "#ef4444" },
      ]
    : [];

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Performance</h1>
          <p className="mt-1 text-sm text-slate-400">Track your trading analytics</p>
        </div>

        {/* Period selector */}
        <div className="flex rounded-lg border border-surface-border">
          {(["7d", "30d", "90d", "all"] as const).map((p) => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-3 py-1.5 text-xs font-medium transition-colors first:rounded-l-lg last:rounded-r-lg ${
                period === p
                  ? "bg-accent text-white"
                  : "text-slate-400 hover:text-white"
              }`}
            >
              {p === "all" ? "All" : p}
            </button>
          ))}
        </div>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <TrendingUp className="h-3.5 w-3.5" /> Total P&amp;L
          </div>
          <p className={`mt-2 text-xl font-bold tabular-nums ${pnlColorClass(stats?.total_pnl ?? 0)}`}>
            {formatINR(stats?.total_pnl ?? 0, true)}
          </p>
        </div>
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <Target className="h-3.5 w-3.5" /> Win Rate
          </div>
          <p className="mt-2 text-xl font-bold text-white tabular-nums">
            {formatPercentage(stats?.win_rate ?? 0, 1)}
          </p>
        </div>
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <BarChart3 className="h-3.5 w-3.5" /> Total Trades
          </div>
          <p className="mt-2 text-xl font-bold text-white tabular-nums">
            {stats?.total_trades ?? 0}
          </p>
        </div>
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-400">
            <ArrowDownRight className="h-3.5 w-3.5" /> Max Drawdown
          </div>
          <p className="mt-2 text-xl font-bold text-loss tabular-nums">
            {formatPercentage(-(stats?.max_drawdown_pct ?? 0), 1)}
          </p>
        </div>
      </div>

      {/* Secondary stats */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <div className="rounded-lg border border-surface-border bg-surface px-4 py-3">
          <span className="text-xs text-slate-400">Avg Return</span>
          <p className={`mt-1 text-sm font-semibold tabular-nums ${pnlColorClass(stats?.avg_return_pct ?? 0)}`}>
            {formatPercentage(stats?.avg_return_pct ?? 0)}
          </p>
        </div>
        <div className="rounded-lg border border-surface-border bg-surface px-4 py-3">
          <span className="text-xs text-slate-400">Profit Factor</span>
          <p className="mt-1 text-sm font-semibold text-white tabular-nums">
            {stats?.profit_factor?.toFixed(2) ?? "N/A"}
          </p>
        </div>
        <div className="rounded-lg border border-surface-border bg-surface px-4 py-3">
          <span className="text-xs text-slate-400">Best Day</span>
          <p className="mt-1 text-sm font-semibold text-profit tabular-nums">
            {formatINR(stats?.best_day ?? 0, true)}
          </p>
        </div>
        <div className="rounded-lg border border-surface-border bg-surface px-4 py-3">
          <span className="text-xs text-slate-400">Worst Day</span>
          <p className="mt-1 text-sm font-semibold text-loss tabular-nums">
            {formatINR(stats?.worst_day ?? 0, true)}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* P&L Chart */}
        <div className="rounded-xl border border-surface-border bg-surface p-4 lg:col-span-2">
          <h2 className="mb-4 text-sm font-semibold text-white">Daily P&amp;L</h2>
          {dailyPnl && dailyPnl.length > 0 ? (
            <PnLChart data={dailyPnl} />
          ) : (
            <div className="flex h-[300px] items-center justify-center">
              <p className="text-sm text-slate-500">No data available</p>
            </div>
          )}
        </div>

        {/* Win rate donut */}
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <h2 className="mb-4 text-sm font-semibold text-white">Win / Loss</h2>
          {stats && (stats.winning_trades > 0 || stats.losing_trades > 0) ? (
            <div className="flex flex-col items-center">
              <div style={{ width: 180, height: 180 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Pie
                      data={winLossData}
                      cx="50%"
                      cy="50%"
                      innerRadius={50}
                      outerRadius={80}
                      dataKey="value"
                      strokeWidth={0}
                    >
                      {winLossData.map((entry, idx) => (
                        <Cell key={idx} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip
                      contentStyle={{
                        backgroundColor: "#1e1e2e",
                        border: "1px solid #3a3a4e",
                        borderRadius: 8,
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              <div className="mt-2 flex gap-4 text-xs">
                <span className="text-profit">{stats.winning_trades} Wins</span>
                <span className="text-loss">{stats.losing_trades} Losses</span>
              </div>
            </div>
          ) : (
            <div className="flex h-[200px] items-center justify-center">
              <p className="text-sm text-slate-500">No trades yet</p>
            </div>
          )}
        </div>
      </div>

      {/* Daily breakdown table */}
      {dailyPnl && dailyPnl.length > 0 && (
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <h2 className="mb-4 text-sm font-semibold text-white">Daily Breakdown</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-surface-border text-xs text-slate-400">
                  <th className="pb-2 pr-4">Date</th>
                  <th className="pb-2 pr-4 text-right">P&amp;L</th>
                  <th className="pb-2 pr-4 text-right">Trades</th>
                  <th className="pb-2 text-right">Win Rate</th>
                </tr>
              </thead>
              <tbody>
                {dailyPnl.map((day) => (
                  <tr key={day.date} className="border-b border-surface-border/50">
                    <td className="py-2 pr-4 text-slate-300">{formatDateOnlyIST(day.date)}</td>
                    <td className={`py-2 pr-4 text-right tabular-nums ${pnlColorClass(day.pnl)}`}>
                      {formatINR(day.pnl, true)}
                    </td>
                    <td className="py-2 pr-4 text-right tabular-nums text-white">{day.trades}</td>
                    <td className="py-2 text-right tabular-nums text-white">
                      {formatPercentage(day.win_rate, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
