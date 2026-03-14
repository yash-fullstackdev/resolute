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
import { Activity, BarChart3, Shield, TrendingUp } from "lucide-react";

interface OverviewStats {
  today_pnl: number;
  open_positions: number;
  capital_at_risk: number;
  total_signals_today: number;
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
        "/performance/daily-pnl",
        { params: { days: 7 } }
      );
      return res.data.data;
    },
  });

  const { data: recentSignals } = useQuery<Signal[]>({
    queryKey: ["recent-signals"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Signal[]>>("/signals", {
        params: { limit: 5 },
      });
      return res.data.data;
    },
    refetchInterval: 15000,
  });

  const displaySignals = liveSignals.length > 0 ? liveSignals.slice(0, 5) : (recentSignals ?? []);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-white">Overview</h1>

      {/* Circuit breaker banner */}
      {circuitBreaker && circuitBreaker.status !== "ACTIVE" && (
        <CircuitBreakerBanner state={circuitBreaker} />
      )}

      {/* Stats cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <TrendingUp className="h-4 w-4" />
            Today&apos;s P&amp;L
          </div>
          <div className="mt-2">
            <INRFormatter
              value={stats?.today_pnl ?? 0}
              showSign
              colorCode
              className="text-2xl font-bold"
            />
          </div>
        </div>

        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <BarChart3 className="h-4 w-4" />
            Open Positions
          </div>
          <p className="mt-2 text-2xl font-bold text-white">{stats?.open_positions ?? positions?.length ?? 0}</p>
          <p className="mt-0.5 text-xs text-slate-500">
            <INRFormatter value={stats?.capital_at_risk ?? 0} /> at risk
          </p>
        </div>

        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <Activity className="h-4 w-4" />
            Signals Today
          </div>
          <p className="mt-2 text-2xl font-bold text-white">{stats?.total_signals_today ?? 0}</p>
        </div>

        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <Shield className="h-4 w-4" />
            Discipline Score
          </div>
          <div className="mt-2 flex items-center gap-3">
            <DisciplineScore
              score={disciplineData?.score ?? 0}
              size={56}
              showLabel={false}
            />
            <span className="text-2xl font-bold text-white">{disciplineData?.score ?? 0}</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Positions */}
        <div className="lg:col-span-2 space-y-4">
          <h2 className="text-lg font-semibold text-white">Open Positions</h2>
          {posLoading ? (
            <div className="flex h-32 items-center justify-center">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
            </div>
          ) : positions && positions.length > 0 ? (
            <div className="space-y-3">
              {positions.map((pos) => (
                <PositionCard key={pos.id} position={pos} />
              ))}
            </div>
          ) : (
            <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-surface-border">
              <p className="text-sm text-slate-500">No open positions</p>
            </div>
          )}
        </div>

        {/* Signals */}
        <div className="space-y-4">
          <h2 className="text-lg font-semibold text-white">Live Signals</h2>
          {displaySignals.length > 0 ? (
            <div className="space-y-3">
              {displaySignals.map((sig) => (
                <SignalCard key={sig.id} signal={sig} />
              ))}
            </div>
          ) : (
            <div className="flex h-32 items-center justify-center rounded-lg border border-dashed border-surface-border">
              <p className="text-sm text-slate-500">No signals yet</p>
            </div>
          )}
        </div>
      </div>

      {/* P&L Chart */}
      <div className="rounded-xl border border-surface-border bg-surface p-4">
        <h2 className="mb-4 text-lg font-semibold text-white">P&amp;L (7 Days)</h2>
        {pnlHistory && pnlHistory.length > 0 ? (
          <PnLChart data={pnlHistory} />
        ) : (
          <div className="flex h-[300px] items-center justify-center">
            <p className="text-sm text-slate-500">No P&amp;L data available</p>
          </div>
        )}
      </div>
    </div>
  );
}
