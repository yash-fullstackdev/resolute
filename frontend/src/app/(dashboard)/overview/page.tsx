"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { usePositions } from "@/hooks/usePositions";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { PositionCard } from "@/components/trading/PositionCard";
import { SignalCard } from "@/components/trading/SignalCard";
import { PnLChart } from "@/components/charts/PnLChart";
import { DisciplineScore } from "@/components/discipline/DisciplineScore";
import { CircuitBreakerBanner } from "@/components/discipline/CircuitBreakerBanner";
import { INRFormatter } from "@/components/common/INRFormatter";
import type { ApiResponse } from "@/types/api";
import type { Signal } from "@/types/trading";
import type { DisciplineScore as DisciplineScoreType } from "@/types/discipline";
import { Activity, BarChart3, Shield, TrendingUp, Radio, Zap, Clock, ChevronRight } from "lucide-react";
import Link from "next/link";

interface OverviewStats {
  today_pnl: number;
  open_positions: number;
  capital_at_risk: number;
  total_signals_today: number;
}

interface InstanceStatus {
  instance_id: string;
  instance_name: string;
  running: boolean;
  signals_today: number;
  daily_pnl: number;
}

export default function OverviewPage() {
  const { data: positions, isLoading: posLoading } = usePositions("OPEN");
  const liveSignals = useLiveDataStore((s) => s.signals);
  const circuitBreaker = useLiveDataStore((s) => s.circuitBreaker);

  const { data: stats } = useQuery<OverviewStats>({
    queryKey: ["overview-stats"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<OverviewStats>>("/dashboard/stats");
      return res.data.data;
    },
    refetchInterval: 10000,
  });

  const { data: disciplineData } = useQuery<DisciplineScoreType>({
    queryKey: ["discipline-score"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<DisciplineScoreType>>("/discipline/score");
      return res.data.data;
    },
    refetchInterval: 30000,
  });

  const { data: pnlHistory } = useQuery<Array<{ date: string; pnl: number }>>({
    queryKey: ["pnl-history-7d"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Array<{ date: string; pnl: number }>>>(
        "/performance/daily",
        { params: { days: 7 } }
      );
      return res.data.data;
    },
  });

  const { data: recentSignals } = useQuery<Signal[]>({
    queryKey: ["recent-signals"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Signal[]>>("/signals", {
        params: { limit: 50 },
      });
      return res.data.data;
    },
    refetchInterval: 15000,
  });

  const { data: instanceStatuses } = useQuery<InstanceStatus[]>({
    queryKey: ["strategy-status"],
    queryFn: async () => {
      try {
        const res = await apiClient.get<{ success: boolean; data: InstanceStatus[] }>("/strategies/status");
        return res.data.data ?? [];
      } catch {
        return [];
      }
    },
    refetchInterval: 10000,
  });

  // Only show today's signals
  const todayStr = new Date().toISOString().slice(0, 10);
  const todaySignals = (recentSignals ?? []).filter((s) => s.created_at?.startsWith(todayStr));
  const displaySignals = liveSignals.length > 0 ? liveSignals.slice(0, 6) : todaySignals.slice(0, 6);
  // Show all instances (not just running) — "running" only becomes true after first evaluation
  const runningInstances = instanceStatuses ?? [];
  const totalSignalsToday = runningInstances.reduce((sum, s) => sum + (s.signals_today ?? 0), 0);
  const totalDailyPnl = runningInstances.reduce((sum, s) => sum + (s.daily_pnl ?? 0), 0);

  // Market hours check (IST)
  const now = new Date();
  const istHour = (now.getUTCHours() + 5) % 24 + (now.getUTCMinutes() + 30 >= 60 ? 1 : 0);
  const istMin = (now.getUTCMinutes() + 30) % 60;
  const marketOpen = istHour >= 9 && istHour < 16;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Overview</h1>
        <div className="flex items-center gap-3">
          {marketOpen ? (
            <span className="flex items-center gap-1.5 rounded-full bg-profit/10 px-3 py-1 text-xs font-medium text-profit">
              <span className="h-2 w-2 rounded-full bg-profit animate-pulse" />
              Market Open
            </span>
          ) : (
            <span className="flex items-center gap-1.5 rounded-full bg-slate-700/50 px-3 py-1 text-xs font-medium text-slate-400">
              <span className="h-2 w-2 rounded-full bg-slate-500" />
              Market Closed
            </span>
          )}
          <span className="text-xs text-slate-500 tabular-nums">
            {runningInstances.length} strateg{runningInstances.length !== 1 ? "ies" : "y"} active
          </span>
        </div>
      </div>

      {/* Circuit breaker banner */}
      {circuitBreaker && circuitBreaker.status !== "ACTIVE" && (
        <CircuitBreakerBanner state={circuitBreaker} />
      )}

      {/* Stats cards */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <TrendingUp className="h-3.5 w-3.5" />
            Today&apos;s P&amp;L
          </div>
          <div className="mt-2">
            {stats?.today_pnl != null ? (
              <INRFormatter value={stats.today_pnl} showSign colorCode className="text-2xl font-bold" />
            ) : totalDailyPnl !== 0 ? (
              <span className={`text-2xl font-bold tabular-nums ${totalDailyPnl >= 0 ? "text-profit" : "text-loss"}`}>
                {totalDailyPnl >= 0 ? "+" : ""}{totalDailyPnl.toFixed(1)} pts
              </span>
            ) : (
              <span className="text-2xl font-bold text-slate-600">--</span>
            )}
          </div>
        </div>

        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <BarChart3 className="h-3.5 w-3.5" />
            Open Positions
          </div>
          <p className="mt-2 text-2xl font-bold text-white">{stats?.open_positions ?? positions?.length ?? 0}</p>
          {(stats?.capital_at_risk ?? 0) > 0 && (
            <p className="mt-0.5 text-[10px] text-slate-600">
              <INRFormatter value={stats?.capital_at_risk ?? 0} /> at risk
            </p>
          )}
        </div>

        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <Activity className="h-3.5 w-3.5" />
            Signals Today
          </div>
          <p className="mt-2 text-2xl font-bold text-white">
            {stats?.total_signals_today ?? totalSignalsToday ?? 0}
          </p>
        </div>

        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <Shield className="h-3.5 w-3.5" />
            Discipline
          </div>
          <div className="mt-2 flex items-center gap-2">
            <DisciplineScore score={disciplineData?.score ?? 100} size={44} showLabel={false} />
            <span className="text-xl font-bold text-white">{disciplineData?.score ?? 100}</span>
          </div>
        </div>
      </div>

      {/* Active Strategies Summary */}
      {runningInstances.length > 0 && (
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-white flex items-center gap-2">
              <Radio className="h-3.5 w-3.5 text-profit animate-pulse" />
              Active Strategies
            </h2>
            <Link href="/strategies" className="text-[10px] text-accent-light hover:text-accent flex items-center gap-0.5">
              Manage <ChevronRight className="h-3 w-3" />
            </Link>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {runningInstances.slice(0, 6).map((inst) => (
              <div key={inst.instance_id} className="flex items-center justify-between rounded-lg bg-surface-dark/50 px-3 py-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span className="h-1.5 w-1.5 rounded-full bg-profit flex-shrink-0" />
                  <span className="text-xs text-white truncate">{inst.instance_name}</span>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  {inst.signals_today > 0 && (
                    <span className="text-[10px] text-slate-500">{inst.signals_today} sig</span>
                  )}
                  {inst.daily_pnl !== 0 && (
                    <span className={`text-[10px] font-medium tabular-nums ${inst.daily_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                      {inst.daily_pnl >= 0 ? "+" : ""}{inst.daily_pnl.toFixed(0)}
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Positions */}
        <div className="lg:col-span-2 space-y-4">
          <h2 className="text-sm font-semibold text-white">Open Positions</h2>
          {posLoading ? (
            <div className="flex h-24 items-center justify-center">
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-accent border-t-transparent" />
            </div>
          ) : positions && positions.length > 0 ? (
            <div className="space-y-3">
              {positions.map((pos) => (
                <PositionCard key={pos.id} position={pos} />
              ))}
            </div>
          ) : (
            <div className="flex h-24 items-center justify-center rounded-lg border border-dashed border-surface-border/50">
              <div className="text-center">
                <BarChart3 className="mx-auto h-5 w-5 text-slate-700" />
                <p className="mt-1 text-xs text-slate-600">No open positions</p>
              </div>
            </div>
          )}

          {/* P&L Chart */}
          <div className="rounded-xl border border-surface-border bg-surface p-4">
            <h2 className="mb-3 text-sm font-semibold text-white">P&amp;L (7 Days)</h2>
            {pnlHistory && pnlHistory.length > 0 ? (
              <PnLChart data={pnlHistory} />
            ) : (
              <div className="flex h-[200px] items-center justify-center">
                <div className="text-center">
                  <TrendingUp className="mx-auto h-5 w-5 text-slate-700" />
                  <p className="mt-1 text-xs text-slate-600">P&amp;L data will appear after first trade</p>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Recent Signals */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-white">
              Today&apos;s Signals {displaySignals.length > 0 && <span className="text-slate-500 font-normal">({displaySignals.length})</span>}
            </h2>
            <Link href="/signals" className="text-[10px] text-accent-light hover:text-accent flex items-center gap-0.5">
              View all <ChevronRight className="h-3 w-3" />
            </Link>
          </div>
          {displaySignals.length > 0 ? (
            <div className="space-y-3">
              {displaySignals.map((sig) => (
                <SignalCard key={sig.id} signal={sig} />
              ))}
            </div>
          ) : (
            <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-surface-border/50">
              <div className="text-center">
                <Zap className="mx-auto h-5 w-5 text-slate-700" />
                <p className="mt-1 text-xs text-slate-600">
                  {marketOpen ? "Waiting for signals..." : "Signals will appear during market hours"}
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
