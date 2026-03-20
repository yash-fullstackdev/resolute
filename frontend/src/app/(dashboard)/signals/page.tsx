"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { SignalCard } from "@/components/trading/SignalCard";
import type { Signal } from "@/types/trading";
import type { ApiResponse } from "@/types/api";
import { Zap, Radio, TrendingUp, TrendingDown, Clock, Target, ShieldAlert } from "lucide-react";

interface LiveTrade {
  symbol: string;
  direction: string;
  entry_price: number;
  sl: number;
  tp: number;
  current_price?: number;
  unrealized_pnl?: number;
  bars_held: number;
  max_hold_bars: number;
  instance_name: string;
  status: string;
}

export default function SignalsPage() {
  const liveSignals = useLiveDataStore((s) => s.signals);

  const { data: historicalSignals, isLoading } = useQuery<Signal[]>({
    queryKey: ["signals-history"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Signal[]>>("/signals", {
        params: { limit: 50 },
      });
      return res.data.data;
    },
    refetchInterval: 5_000,  // poll every 5s for live price updates
  });

  // Live trades — poll every 3 seconds
  const { data: liveTrades } = useQuery<LiveTrade[]>({
    queryKey: ["live-trades"],
    queryFn: async () => {
      const res = await apiClient.get<{ data: LiveTrade[] }>("/signals/live-trades");
      return res.data.data ?? [];
    },
    refetchInterval: 3_000,
  });

  const allSignals = liveSignals.length > 0 ? liveSignals : (historicalSignals ?? []);
  const openTrades = liveTrades ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Signals</h1>
          <p className="mt-1 text-sm text-slate-400">Live trades and historical signals</p>
        </div>
        {openTrades.length > 0 && (
          <div className="flex items-center gap-2 rounded-full bg-profit/10 px-3 py-1 text-xs font-medium text-profit">
            <Radio className="h-3 w-3 animate-pulse" />
            {openTrades.length} Open Trade{openTrades.length > 1 ? "s" : ""}
          </div>
        )}
      </div>

      {/* Open Trades — Live Tracking */}
      {openTrades.length > 0 && (
        <div className="space-y-3">
          <h2 className="text-sm font-semibold text-white flex items-center gap-2">
            <Radio className="h-4 w-4 text-profit animate-pulse" />
            Open Trades (Live)
          </h2>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {openTrades.map((trade, idx) => {
              const isBuy = trade.direction === "BUY";
              const pnl = trade.unrealized_pnl ?? 0;
              const pnlColor = pnl >= 0 ? "text-profit" : "text-loss";
              const totalRange = Math.abs(trade.tp - trade.sl);
              const priceInRange = trade.current_price
                ? isBuy
                  ? (trade.current_price - trade.sl) / totalRange
                  : (trade.sl - trade.current_price) / totalRange
                : 0.5;
              const progressPct = Math.max(0, Math.min(100, priceInRange * 100));
              const timeProgress = trade.max_hold_bars > 0
                ? Math.round((trade.bars_held / trade.max_hold_bars) * 100)
                : 0;

              return (
                <div key={idx} className={`rounded-xl border p-4 ${
                  isBuy ? "border-profit/30 bg-profit/[0.03]" : "border-loss/30 bg-loss/[0.03]"
                }`}>
                  {/* Header */}
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      {isBuy ? <TrendingUp className="h-4 w-4 text-profit" /> : <TrendingDown className="h-4 w-4 text-loss" />}
                      <span className={`text-sm font-bold ${isBuy ? "text-profit" : "text-loss"}`}>{trade.direction}</span>
                      <span className="text-sm font-semibold text-white">{trade.symbol}</span>
                    </div>
                    <span className={`text-lg font-bold tabular-nums ${pnlColor}`}>
                      {pnl >= 0 ? "+" : ""}{pnl.toFixed(1)} pts
                    </span>
                  </div>

                  {/* Price Progress Bar — SL to TP */}
                  <div className="mb-3">
                    <div className="flex items-center justify-between text-[10px] text-slate-500 mb-1">
                      <span className="text-loss">SL: {trade.sl.toFixed(1)}</span>
                      <span className="text-white font-medium">
                        {trade.current_price ? trade.current_price.toFixed(1) : "--"}
                      </span>
                      <span className="text-profit">TP: {trade.tp.toFixed(1)}</span>
                    </div>
                    <div className="h-2 rounded-full bg-surface-dark overflow-hidden relative">
                      <div className="absolute inset-y-0 left-0 bg-gradient-to-r from-loss via-slate-500 to-profit opacity-20 w-full" />
                      <div className="absolute top-0 h-full w-1 bg-white rounded-full transition-all"
                        style={{ left: `${progressPct}%` }} />
                    </div>
                  </div>

                  {/* Details */}
                  <div className="grid grid-cols-3 gap-2 text-center text-[10px]">
                    <div>
                      <p className="text-slate-500">Entry</p>
                      <p className="text-xs font-bold text-white tabular-nums">{trade.entry_price.toFixed(1)}</p>
                    </div>
                    <div>
                      <p className="text-slate-500">Bars Held</p>
                      <p className="text-xs font-bold text-white tabular-nums">{trade.bars_held}/{trade.max_hold_bars}</p>
                    </div>
                    <div>
                      <p className="text-slate-500">Time Left</p>
                      <div className="h-1.5 rounded-full bg-surface-dark mt-1 overflow-hidden">
                        <div className={`h-full rounded-full transition-all ${
                          timeProgress > 80 ? "bg-loss" : timeProgress > 50 ? "bg-amber-400" : "bg-profit"
                        }`} style={{ width: `${100 - timeProgress}%` }} />
                      </div>
                    </div>
                  </div>

                  <div className="mt-2 text-[10px] text-slate-600">{trade.instance_name}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Historical Signals */}
      <div>
        <h2 className="text-sm font-semibold text-white mb-3">Signal History</h2>
        {isLoading ? (
          <div className="flex h-32 items-center justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          </div>
        ) : allSignals.length > 0 ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {allSignals.map((signal) => (
              <SignalCard key={signal.id} signal={signal} />
            ))}
          </div>
        ) : (
          <div className="flex h-32 items-center justify-center rounded-xl border border-dashed border-surface-border">
            <div className="text-center">
              <Zap className="mx-auto h-6 w-6 text-slate-600" />
              <p className="mt-2 text-xs text-slate-500">No signals yet today</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
