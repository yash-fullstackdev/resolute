"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { SignalCard } from "@/components/trading/SignalCard";
import type { Signal } from "@/types/trading";
import type { ApiResponse } from "@/types/api";
import { Zap, Radio } from "lucide-react";

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
  });

  const allSignals = liveSignals.length > 0
    ? liveSignals
    : (historicalSignals ?? []);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Signals</h1>
          <p className="mt-1 text-sm text-slate-400">Live and historical trading signals</p>
        </div>
        {liveSignals.length > 0 && (
          <div className="flex items-center gap-2 rounded-full bg-profit/10 px-3 py-1 text-xs font-medium text-profit">
            <Radio className="h-3 w-3 animate-pulse" />
            Live
          </div>
        )}
      </div>

      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : allSignals.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {allSignals.map((signal) => (
            <SignalCard key={signal.id} signal={signal} />
          ))}
        </div>
      ) : (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <Zap className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">No signals yet today</p>
            <p className="text-xs text-slate-500">Signals appear during market hours</p>
          </div>
        </div>
      )}
    </div>
  );
}
