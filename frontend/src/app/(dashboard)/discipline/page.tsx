"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { DisciplineScore } from "@/components/discipline/DisciplineScore";
import { CircuitBreakerBanner } from "@/components/discipline/CircuitBreakerBanner";
import type { DisciplineScore as DisciplineScoreType, CircuitBreakerState, OverrideRequest } from "@/types/discipline";
import type { ApiResponse } from "@/types/api";
import { formatINR, formatDateIST, pnlColorClass } from "@/lib/formatters";
import { Shield, AlertTriangle, History } from "lucide-react";

export default function DisciplinePage() {
  const circuitBreaker = useLiveDataStore((s) => s.circuitBreaker);

  const { data: disciplineData, isLoading: scoreLoading } = useQuery<DisciplineScoreType>({
    queryKey: ["discipline-score"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<DisciplineScoreType>>("/discipline/score");
      return res.data.data;
    },
  });

  const { data: cbState } = useQuery<CircuitBreakerState>({
    queryKey: ["circuit-breaker"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<CircuitBreakerState>>("/discipline/circuit-breaker");
      return res.data.data;
    },
    refetchInterval: 10000,
  });

  const { data: overrides } = useQuery<OverrideRequest[]>({
    queryKey: ["override-history"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<OverrideRequest[]>>("/discipline/overrides", {
        params: { limit: 10 },
      });
      return res.data.data;
    },
  });

  const activeCB = circuitBreaker ?? cbState;
  const totalOverrideImpact = (overrides ?? []).reduce((sum, o) => sum + (o.pnl_impact ?? 0), 0);

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-white">Discipline</h1>

      {/* Circuit breaker status */}
      {activeCB && activeCB.status !== "ACTIVE" && (
        <CircuitBreakerBanner state={activeCB} />
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Score */}
        <div className="rounded-xl border border-surface-border bg-surface p-6">
          <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
            <Shield className="h-4 w-4 text-accent-light" />
            Discipline Score
          </h2>
          {scoreLoading ? (
            <div className="flex h-40 items-center justify-center">
              <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
            </div>
          ) : (
            <div className="flex flex-col items-center">
              <DisciplineScore score={disciplineData?.score ?? 0} size={160} />
              {disciplineData?.trend && (
                <span
                  className={`mt-3 rounded-full px-3 py-1 text-xs font-medium ${
                    disciplineData.trend === "IMPROVING"
                      ? "bg-profit/10 text-profit"
                      : disciplineData.trend === "DECLINING"
                        ? "bg-loss/10 text-loss"
                        : "bg-slate-500/10 text-slate-400"
                  }`}
                >
                  {disciplineData.trend}
                </span>
              )}
            </div>
          )}
        </div>

        {/* Score breakdown */}
        <div className="rounded-xl border border-surface-border bg-surface p-6">
          <h2 className="mb-4 text-sm font-semibold text-white">Score Breakdown</h2>
          {disciplineData?.components && (
            <div className="space-y-3">
              {Object.entries(disciplineData.components).map(([key, value]) => (
                <div key={key}>
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-slate-400">
                      {key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
                    </span>
                    <span className="tabular-nums text-white">{value}</span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-light">
                    <div
                      className="h-full rounded-full bg-accent transition-all"
                      style={{ width: `${Math.min(value, 100)}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Circuit breaker info */}
        <div className="rounded-xl border border-surface-border bg-surface p-6">
          <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
            <AlertTriangle className="h-4 w-4 text-amber-400" />
            Circuit Breaker
          </h2>
          {activeCB ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-400">Status</span>
                <span
                  className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                    activeCB.status === "ACTIVE"
                      ? "bg-profit/10 text-profit"
                      : activeCB.status === "HALTED"
                        ? "bg-loss/10 text-loss"
                        : "bg-amber-500/10 text-amber-400"
                  }`}
                >
                  {activeCB.status}
                </span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-400">Daily Loss</span>
                <span className={`tabular-nums ${pnlColorClass(-activeCB.daily_loss)}`}>
                  {formatINR(activeCB.daily_loss)} / {formatINR(activeCB.daily_loss_limit)}
                </span>
              </div>
              <div className="flex items-center justify-between text-sm">
                <span className="text-slate-400">Consecutive Losses</span>
                <span className="tabular-nums text-white">
                  {activeCB.consecutive_losses} / {activeCB.max_consecutive_losses}
                </span>
              </div>
              {/* Loss bar */}
              <div className="h-2 overflow-hidden rounded-full bg-surface-light">
                <div
                  className="h-full rounded-full bg-loss transition-all"
                  style={{
                    width: `${Math.min(
                      (activeCB.daily_loss / activeCB.daily_loss_limit) * 100,
                      100
                    )}%`,
                  }}
                />
              </div>
            </div>
          ) : (
            <p className="text-sm text-slate-400">Loading...</p>
          )}
        </div>
      </div>

      {/* Override history */}
      <div className="rounded-xl border border-surface-border bg-surface p-6">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="flex items-center gap-2 text-sm font-semibold text-white">
            <History className="h-4 w-4 text-slate-400" />
            Override History
          </h2>
          <span className={`text-sm font-medium ${pnlColorClass(totalOverrideImpact)}`}>
            Net impact: {formatINR(totalOverrideImpact, true)}
          </span>
        </div>

        {overrides && overrides.length > 0 ? (
          <div className="space-y-2">
            {overrides.map((override) => (
              <div
                key={override.id}
                className="flex items-center justify-between rounded-lg bg-surface-dark px-4 py-3"
              >
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-white">
                      {override.override_type.replace(/_/g, " ")}
                    </span>
                    <span className="text-xs text-slate-500">
                      {formatDateIST(override.created_at)}
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-slate-400">
                    {override.original_value} &rarr; {override.proposed_value}
                  </p>
                  <p className="text-xs text-slate-500">{override.reason}</p>
                </div>
                {override.pnl_impact !== undefined && (
                  <span className={`text-sm font-semibold tabular-nums ${pnlColorClass(override.pnl_impact)}`}>
                    {formatINR(override.pnl_impact, true)}
                  </span>
                )}
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-400">No overrides recorded</p>
        )}
      </div>
    </div>
  );
}
