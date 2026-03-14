"use client";

import { useState } from "react";
import { usePositions, useExitPosition } from "@/hooks/usePositions";
import { PositionCard } from "@/components/trading/PositionCard";
import { formatINR, pnlColorClass } from "@/lib/formatters";
import { STRATEGY_NAMES } from "@/lib/constants";
import { Filter, SortAsc, SortDesc } from "lucide-react";

type SortField = "pnl" | "time" | "underlying";
type SortDir = "asc" | "desc";

export default function PositionsPage() {
  const [statusFilter, setStatusFilter] = useState<"OPEN" | "CLOSED" | undefined>("OPEN");
  const [sortField, setSortField] = useState<SortField>("time");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [strategyFilter, setStrategyFilter] = useState<string>("all");

  const { data: positions, isLoading } = usePositions(statusFilter);
  const exitMutation = useExitPosition();

  const handleExit = (positionId: string) => {
    if (confirm("Are you sure you want to exit this position?")) {
      exitMutation.mutate(positionId);
    }
  };

  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDir("desc");
    }
  };

  let filtered = positions ?? [];

  if (strategyFilter !== "all") {
    filtered = filtered.filter((p) => p.strategy_name === strategyFilter);
  }

  const sorted = [...filtered].sort((a, b) => {
    const mult = sortDir === "asc" ? 1 : -1;
    switch (sortField) {
      case "pnl":
        return (a.unrealized_pnl - b.unrealized_pnl) * mult;
      case "time":
        return (new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime()) * mult;
      case "underlying":
        return a.underlying.localeCompare(b.underlying) * mult;
      default:
        return 0;
    }
  });

  const totalPnl = sorted.reduce((sum, p) => sum + p.unrealized_pnl, 0);
  const strategies = [...new Set((positions ?? []).map((p) => p.strategy_name))];

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Positions</h1>
          <p className="mt-1 text-sm text-slate-400">
            Total P&amp;L:{" "}
            <span className={`font-semibold tabular-nums ${pnlColorClass(totalPnl)}`}>
              {formatINR(totalPnl, true)}
            </span>
          </p>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap items-center gap-2">
          {/* Status filter */}
          <div className="flex rounded-lg border border-surface-border">
            {(["OPEN", "CLOSED"] as const).map((status) => (
              <button
                key={status}
                onClick={() => setStatusFilter(status)}
                className={`px-3 py-1.5 text-xs font-medium transition-colors first:rounded-l-lg last:rounded-r-lg ${
                  statusFilter === status
                    ? "bg-accent text-white"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {status}
              </button>
            ))}
          </div>

          {/* Strategy filter */}
          <select
            value={strategyFilter}
            onChange={(e) => setStrategyFilter(e.target.value)}
            className="rounded-lg border border-surface-border bg-surface px-3 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
          >
            <option value="all">All Strategies</option>
            {strategies.map((s) => (
              <option key={s} value={s}>
                {STRATEGY_NAMES[s] ?? s}
              </option>
            ))}
          </select>

          {/* Sort buttons */}
          <div className="flex items-center gap-1">
            {(["time", "pnl", "underlying"] as const).map((field) => (
              <button
                key={field}
                onClick={() => toggleSort(field)}
                className={`flex items-center gap-1 rounded-md px-2 py-1.5 text-xs transition-colors ${
                  sortField === field
                    ? "bg-accent/10 text-accent-light"
                    : "text-slate-400 hover:text-white"
                }`}
              >
                {field === "time" ? "Time" : field === "pnl" ? "P&L" : "Symbol"}
                {sortField === field &&
                  (sortDir === "asc" ? (
                    <SortAsc className="h-3 w-3" />
                  ) : (
                    <SortDesc className="h-3 w-3" />
                  ))}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Positions list */}
      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : sorted.length > 0 ? (
        <div className="space-y-3">
          {sorted.map((pos) => (
            <PositionCard
              key={pos.id}
              position={pos}
              onExit={statusFilter === "OPEN" ? handleExit : undefined}
              isExiting={exitMutation.isPending}
            />
          ))}
        </div>
      ) : (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <Filter className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">
              No {statusFilter?.toLowerCase()} positions found
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
