"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { StrategyCard } from "@/components/strategy/StrategyCard";
import type { Strategy, StrategyCategory } from "@/types/strategy";
import type { ApiResponse } from "@/types/api";
import { Layers, Plus } from "lucide-react";
import Link from "next/link";

const CATEGORIES: Array<{ value: StrategyCategory | "ALL"; label: string }> = [
  { value: "ALL", label: "All" },
  { value: "BUYING", label: "Buying" },
  { value: "SELLING", label: "Selling" },
  { value: "HYBRID", label: "Hybrid" },
];

export default function StrategiesPage() {
  const [categoryFilter, setCategoryFilter] = useState<StrategyCategory | "ALL">("ALL");
  const queryClient = useQueryClient();

  const { data: strategies, isLoading } = useQuery<Strategy[]>({
    queryKey: ["strategies"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Strategy[]>>("/strategies");
      return res.data.data;
    },
  });

  const toggleMutation = useMutation({
    mutationFn: async ({ strategyId, enabled }: { strategyId: string; enabled: boolean }) => {
      await apiClient.patch(`/strategies/${strategyId}`, { enabled });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  const handleToggle = (strategyId: string, enabled: boolean) => {
    toggleMutation.mutate({ strategyId, enabled });
  };

  const filtered =
    categoryFilter === "ALL"
      ? (strategies ?? [])
      : (strategies ?? []).filter((s) => s.category === categoryFilter);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Strategies</h1>
          <p className="mt-1 text-sm text-slate-400">Configure built-in and custom strategies</p>
        </div>
        <Link
          href="/strategies/builder"
          className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-light"
        >
          <Plus className="h-4 w-4" />
          Build Custom Strategy
        </Link>
      </div>

      {/* Category filter */}
      <div className="flex gap-1 rounded-lg border border-surface-border p-1">
        {CATEGORIES.map((cat) => (
          <button
            key={cat.value}
            onClick={() => setCategoryFilter(cat.value)}
            className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
              categoryFilter === cat.value
                ? "bg-accent text-white"
                : "text-slate-400 hover:text-white"
            }`}
          >
            {cat.label}
          </button>
        ))}
      </div>

      {/* Strategy grid */}
      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : filtered.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((strategy) => (
            <StrategyCard
              key={strategy.id}
              strategy={strategy}
              onToggle={handleToggle}
            />
          ))}
        </div>
      ) : (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <Layers className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">No strategies found</p>
          </div>
        </div>
      )}
    </div>
  );
}
