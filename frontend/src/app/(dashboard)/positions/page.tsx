"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { usePositions, useExitPosition } from "@/hooks/usePositions";
import { PositionCard } from "@/components/trading/PositionCard";
import { apiClient } from "@/lib/api";
import { formatINR, pnlColorClass } from "@/lib/formatters";
import { STRATEGY_NAMES } from "@/lib/constants";
import { Filter, SortAsc, SortDesc, Briefcase, TrendingUp, Package, TrendingDown } from "lucide-react";

type PageTab = "strategy" | "broker" | "holdings";
type SortField = "pnl" | "time" | "underlying";
type SortDir = "asc" | "desc";

interface BrokerPosition {
  symbol: string;
  security_id: string;
  exchange: string;
  direction: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  pnl: number;
  product: string;
}

interface Holding {
  symbol: string;
  security_id: string;
  isin: string;
  quantity: number;
  avg_cost: number;
  ltp: number;
  current_value: number;
  invested: number;
  pnl: number;
  pnl_pct: number;
}

interface HoldingsSummary {
  total_invested: number;
  total_current: number;
  total_pnl: number;
  total_pnl_pct: number;
  holdings_count: number;
}

export default function PositionsPage() {
  const [tab, setTab] = useState<PageTab>("strategy");
  const [statusFilter, setStatusFilter] = useState<"OPEN" | "CLOSED">("OPEN");
  const [sortField, setSortField] = useState<SortField>("time");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [strategyFilter, setStrategyFilter] = useState<string>("all");

  const { data: positions, isLoading } = usePositions(statusFilter);
  const exitMutation = useExitPosition();

  const { data: brokerPositions, isLoading: brokerLoading } = useQuery<BrokerPosition[]>({
    queryKey: ["broker-positions"],
    queryFn: async () => {
      const res = await apiClient.get<{ success: boolean; data: BrokerPosition[] }>("/positions/broker/positions");
      return res.data.data ?? [];
    },
    refetchInterval: 5000,
    enabled: tab === "broker",
  });

  const { data: holdingsData, isLoading: holdingsLoading } = useQuery<{ data: Holding[]; summary: HoldingsSummary }>({
    queryKey: ["broker-holdings"],
    queryFn: async () => {
      const res = await apiClient.get<{ success: boolean; data: Holding[]; summary: HoldingsSummary }>("/positions/broker/holdings");
      return { data: res.data.data ?? [], summary: res.data.summary };
    },
    enabled: tab === "holdings",
  });

  const handleExit = (positionId: string) => {
    if (confirm("Are you sure you want to exit this position?")) {
      exitMutation.mutate(positionId);
    }
  };

  const toggleSort = (field: SortField) => {
    if (sortField === field) setSortDir(sortDir === "asc" ? "desc" : "asc");
    else { setSortField(field); setSortDir("desc"); }
  };

  let filtered = positions ?? [];
  if (strategyFilter !== "all") filtered = filtered.filter((p) => p.strategy_name === strategyFilter);
  const sorted = [...filtered].sort((a, b) => {
    const m = sortDir === "asc" ? 1 : -1;
    if (sortField === "pnl") return (a.unrealized_pnl - b.unrealized_pnl) * m;
    if (sortField === "time") return (new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime()) * m;
    return a.underlying.localeCompare(b.underlying) * m;
  });
  const totalPnl = sorted.reduce((sum, p) => sum + p.unrealized_pnl, 0);
  const strategies = [...new Set((positions ?? []).map((p) => p.strategy_name))];

  return (
    <div className="space-y-6">
      {/* Header + Tabs */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Positions</h1>
        </div>
        <div className="flex rounded-lg border border-surface-border">
          {([
            { key: "strategy", label: "Strategy Positions", icon: TrendingUp },
            { key: "broker", label: "Broker Positions", icon: Briefcase },
            { key: "holdings", label: "Holdings", icon: Package },
          ] as const).map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setTab(key)}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium transition-colors first:rounded-l-lg last:rounded-r-lg ${
                tab === key ? "bg-accent text-white" : "text-slate-400 hover:text-white"
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* ── TAB: Strategy Positions (from our DB) ── */}
      {tab === "strategy" && (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <p className="text-sm text-slate-400">
              Total P&L:{" "}
              <span className={`font-semibold tabular-nums ${pnlColorClass(totalPnl)}`}>
                {formatINR(totalPnl, true)}
              </span>
            </p>
            <div className="flex rounded-lg border border-surface-border">
              {(["OPEN", "CLOSED"] as const).map((s) => (
                <button key={s} onClick={() => setStatusFilter(s)}
                  className={`px-3 py-1.5 text-xs font-medium first:rounded-l-lg last:rounded-r-lg ${
                    statusFilter === s ? "bg-accent text-white" : "text-slate-400 hover:text-white"
                  }`}>
                  {s}
                </button>
              ))}
            </div>
            <select value={strategyFilter} onChange={(e) => setStrategyFilter(e.target.value)}
              className="rounded-lg border border-surface-border bg-surface px-3 py-1.5 text-xs text-white focus:outline-none">
              <option value="all">All Strategies</option>
              {strategies.map((s) => <option key={s} value={s}>{STRATEGY_NAMES[s] ?? s}</option>)}
            </select>
            <div className="flex items-center gap-1">
              {(["time", "pnl", "underlying"] as const).map((f) => (
                <button key={f} onClick={() => toggleSort(f)}
                  className={`flex items-center gap-1 rounded-md px-2 py-1.5 text-xs ${
                    sortField === f ? "bg-accent/10 text-accent-light" : "text-slate-400 hover:text-white"
                  }`}>
                  {f === "time" ? "Time" : f === "pnl" ? "P&L" : "Symbol"}
                  {sortField === f && (sortDir === "asc" ? <SortAsc className="h-3 w-3" /> : <SortDesc className="h-3 w-3" />)}
                </button>
              ))}
            </div>
          </div>
          {isLoading ? (
            <div className="flex h-40 items-center justify-center"><div className="h-5 w-5 animate-spin rounded-full border-2 border-accent border-t-transparent" /></div>
          ) : sorted.length > 0 ? (
            <div className="space-y-3">
              {sorted.map((pos) => (
                <PositionCard key={pos.id} position={pos} onExit={statusFilter === "OPEN" ? handleExit : undefined} isExiting={exitMutation.isPending} />
              ))}
            </div>
          ) : (
            <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-surface-border/50">
              <div className="text-center">
                <TrendingUp className="mx-auto h-6 w-6 text-slate-700" />
                <p className="mt-2 text-xs text-slate-500">No {statusFilter.toLowerCase()} strategy positions</p>
                <p className="text-[10px] text-slate-600 mt-1">Positions will appear when strategies fire live trades</p>
              </div>
            </div>
          )}
        </>
      )}

      {/* ── TAB: Broker Positions (live from Dhan) ── */}
      {tab === "broker" && (
        <>
          {brokerLoading ? (
            <div className="flex h-40 items-center justify-center"><div className="h-5 w-5 animate-spin rounded-full border-2 border-accent border-t-transparent" /></div>
          ) : (brokerPositions ?? []).length > 0 ? (
            <div className="space-y-3">
              {(brokerPositions ?? []).map((pos, idx) => {
                const isBuy = pos.direction === "BUY";
                const pnlColor = pos.pnl >= 0 ? "text-profit" : "text-loss";
                return (
                  <div key={idx} className={`rounded-xl border p-4 ${isBuy ? "border-profit/20" : "border-loss/20"} bg-surface`}>
                    <div className="flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        {isBuy ? <TrendingUp className="h-4 w-4 text-profit" /> : <TrendingDown className="h-4 w-4 text-loss" />}
                        <span className={`text-sm font-bold ${isBuy ? "text-profit" : "text-loss"}`}>{pos.direction}</span>
                        <span className="text-sm font-semibold text-white">{pos.symbol}</span>
                        <span className="rounded bg-surface-dark px-2 py-0.5 text-[10px] text-slate-400">{pos.product}</span>
                        <span className="text-[10px] text-slate-500">Qty: {pos.quantity}</span>
                      </div>
                      <span className={`text-lg font-bold tabular-nums ${pnlColor}`}>
                        {pos.pnl >= 0 ? "+" : ""}{pos.pnl.toFixed(2)}
                      </span>
                    </div>
                    <div className="mt-2 grid grid-cols-3 gap-4 text-center text-[10px]">
                      <div><p className="text-slate-500">Entry</p><p className="text-xs font-bold text-white tabular-nums">{pos.entry_price.toFixed(2)}</p></div>
                      <div><p className="text-slate-500">Current</p><p className="text-xs font-bold text-white tabular-nums">{pos.current_price.toFixed(2)}</p></div>
                      <div><p className="text-slate-500">Exchange</p><p className="text-xs font-bold text-white">{pos.exchange}</p></div>
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-surface-border/50">
              <div className="text-center">
                <Briefcase className="mx-auto h-6 w-6 text-slate-700" />
                <p className="mt-2 text-xs text-slate-500">No open intraday positions on broker</p>
                <p className="text-[10px] text-slate-600 mt-1">Positions appear during market hours when trades execute</p>
              </div>
            </div>
          )}
        </>
      )}

      {/* ── TAB: Holdings (delivery stocks from Dhan) ── */}
      {tab === "holdings" && (
        <>
          {/* Summary */}
          {holdingsData?.summary && (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <div className="rounded-lg border border-surface-border bg-surface p-3 text-center">
                <p className="text-[10px] text-slate-500">Invested</p>
                <p className="text-lg font-bold text-white tabular-nums">{formatINR(holdingsData.summary.total_invested)}</p>
              </div>
              <div className="rounded-lg border border-surface-border bg-surface p-3 text-center">
                <p className="text-[10px] text-slate-500">Current Value</p>
                <p className="text-lg font-bold text-white tabular-nums">{formatINR(holdingsData.summary.total_current)}</p>
              </div>
              <div className="rounded-lg border border-surface-border bg-surface p-3 text-center">
                <p className="text-[10px] text-slate-500">Total P&L</p>
                <p className={`text-lg font-bold tabular-nums ${holdingsData.summary.total_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                  {holdingsData.summary.total_pnl >= 0 ? "+" : ""}{formatINR(holdingsData.summary.total_pnl)}
                </p>
              </div>
              <div className="rounded-lg border border-surface-border bg-surface p-3 text-center">
                <p className="text-[10px] text-slate-500">Return %</p>
                <p className={`text-lg font-bold tabular-nums ${holdingsData.summary.total_pnl_pct >= 0 ? "text-profit" : "text-loss"}`}>
                  {holdingsData.summary.total_pnl_pct >= 0 ? "+" : ""}{holdingsData.summary.total_pnl_pct.toFixed(2)}%
                </p>
              </div>
            </div>
          )}

          {holdingsLoading ? (
            <div className="flex h-40 items-center justify-center"><div className="h-5 w-5 animate-spin rounded-full border-2 border-accent border-t-transparent" /></div>
          ) : (holdingsData?.data ?? []).length > 0 ? (
            <div className="rounded-xl border border-surface-border overflow-hidden">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-surface-border bg-surface-dark/50">
                    <th className="px-3 py-2 text-left text-slate-500 font-medium">Symbol</th>
                    <th className="px-3 py-2 text-right text-slate-500 font-medium">Qty</th>
                    <th className="px-3 py-2 text-right text-slate-500 font-medium">Avg Cost</th>
                    <th className="px-3 py-2 text-right text-slate-500 font-medium">LTP</th>
                    <th className="px-3 py-2 text-right text-slate-500 font-medium">Current Value</th>
                    <th className="px-3 py-2 text-right text-slate-500 font-medium">P&L</th>
                    <th className="px-3 py-2 text-right text-slate-500 font-medium">Return %</th>
                  </tr>
                </thead>
                <tbody>
                  {(holdingsData?.data ?? []).map((h, idx) => (
                    <tr key={idx} className="border-b border-surface-border/30 hover:bg-surface-dark/20 transition-colors">
                      <td className="px-3 py-2.5">
                        <span className="font-semibold text-white">{h.symbol}</span>
                      </td>
                      <td className="px-3 py-2.5 text-right text-slate-300 tabular-nums">{h.quantity}</td>
                      <td className="px-3 py-2.5 text-right text-slate-300 tabular-nums">{h.avg_cost.toFixed(2)}</td>
                      <td className="px-3 py-2.5 text-right text-white font-medium tabular-nums">{h.ltp.toFixed(2)}</td>
                      <td className="px-3 py-2.5 text-right text-white tabular-nums">{formatINR(h.current_value)}</td>
                      <td className={`px-3 py-2.5 text-right font-medium tabular-nums ${h.pnl >= 0 ? "text-profit" : "text-loss"}`}>
                        {h.pnl >= 0 ? "+" : ""}{formatINR(h.pnl)}
                      </td>
                      <td className={`px-3 py-2.5 text-right font-medium tabular-nums ${h.pnl_pct >= 0 ? "text-profit" : "text-loss"}`}>
                        {h.pnl_pct >= 0 ? "+" : ""}{h.pnl_pct.toFixed(2)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-surface-border/50">
              <div className="text-center">
                <Package className="mx-auto h-6 w-6 text-slate-700" />
                <p className="mt-2 text-xs text-slate-500">No holdings found</p>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
