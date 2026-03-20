"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { StrategyCard } from "@/components/strategy/StrategyCard";
import { StrategyConfigModal } from "@/components/strategy/StrategyConfigModal";
import type { Strategy, StrategyCategory, StrategyInstance } from "@/types/strategy";
import type { ApiResponse } from "@/types/api";
import { Layers, Plus } from "lucide-react";
import Link from "next/link";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";

const CATEGORIES: Array<{ value: StrategyCategory | "ALL"; label: string }> = [
  { value: "ALL", label: "All Strategies" },
  { value: "TECHNICAL", label: "Technical" },
];

export default function StrategiesPage() {
  const [categoryFilter, setCategoryFilter] = useState<StrategyCategory | "ALL">("ALL");
  const [editState, setEditState] = useState<{
    strategy: Strategy;
    instance?: StrategyInstance;
  } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);
  const [liveTarget, setLiveTarget] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const { data: strategies, isLoading } = useQuery<Strategy[]>({
    queryKey: ["strategies"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Strategy[]>>("/strategies");
      return res.data.data;
    },
  });

  // Poll instance status every 10 seconds
  const { data: instanceStatuses } = useQuery<Record<string, unknown>[]>({
    queryKey: ["strategy-status"],
    queryFn: async () => {
      const res = await apiClient.get<{ data: Record<string, unknown>[] }>("/strategies/status");
      return res.data.data ?? [];
    },
    refetchInterval: 10_000,
  });

  // Mode change mutation
  const modeMutation = useMutation({
    mutationFn: async ({ instanceId, mode }: { instanceId: string; mode: string }) => {
      await apiClient.patch(`/strategies/instances/${instanceId}`, {
        mode,
        enabled: mode !== "disabled",
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: async (instanceId: string) => {
      await apiClient.delete(`/strategies/instances/${instanceId}`);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
    },
  });

  const handleConfigureInstance = (strategy: Strategy, instance: StrategyInstance) => {
    setEditState({ strategy, instance });
  };

  const handleAddInstance = (strategy: Strategy) => {
    setEditState({ strategy, instance: undefined });
  };

  const handleDeleteInstance = (instanceId: string) => {
    // Find instance name for the dialog
    const allInstances = (strategies ?? []).flatMap((s) => s.instances ?? []);
    const inst = allInstances.find((i) => i.instance_id === instanceId);
    setDeleteTarget({ id: instanceId, name: inst?.instance_name ?? "this instance" });
  };

  const handleModeChange = (instanceId: string, mode: "live" | "paper" | "disabled") => {
    if (mode === "live") {
      setLiveTarget(instanceId);
      return;
    }
    modeMutation.mutate({ instanceId, mode });
  };

  const filtered =
    categoryFilter === "ALL"
      ? (strategies ?? [])
      : (strategies ?? []).filter((s) => s.category === categoryFilter);

  // Count active instances across all strategies
  const totalInstances = (strategies ?? []).reduce((acc, s) => acc + (s.instances?.length ?? 0), 0);
  const liveInstances = (strategies ?? []).reduce(
    (acc, s) => acc + (s.instances?.filter((i) => i.mode === "live").length ?? 0), 0
  );
  const paperInstances = (strategies ?? []).reduce(
    (acc, s) => acc + (s.instances?.filter((i) => i.mode === "paper").length ?? 0), 0
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Strategies</h1>
          <p className="mt-1 text-sm text-slate-400">
            {totalInstances} instance{totalInstances !== 1 ? "s" : ""}
            {liveInstances > 0 && <span className="text-profit ml-1">({liveInstances} live)</span>}
            {paperInstances > 0 && <span className="text-amber-400 ml-1">({paperInstances} paper)</span>}
          </p>
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
              instanceStatuses={instanceStatuses ?? []}
              onConfigureInstance={handleConfigureInstance}
              onAddInstance={handleAddInstance}
              onDeleteInstance={handleDeleteInstance}
              onModeChange={handleModeChange}
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

      {/* Config Modal */}
      {editState && (
        <StrategyConfigModal
          strategy={editState.strategy}
          instance={editState.instance}
          onClose={() => setEditState(null)}
        />
      )}

      {/* Delete Confirmation */}
      <ConfirmDialog
        open={deleteTarget !== null}
        variant="danger"
        title="Delete Instance"
        description={`Are you sure you want to delete "${deleteTarget?.name}"? All configuration, bias filters, and parameters will be permanently removed. This action cannot be undone.`}
        confirmLabel="Delete Instance"
        cancelLabel="Keep It"
        loading={deleteMutation.isPending}
        onConfirm={() => {
          if (deleteTarget) {
            deleteMutation.mutate(deleteTarget.id, { onSuccess: () => setDeleteTarget(null) });
          }
        }}
        onCancel={() => setDeleteTarget(null)}
      />

      {/* Live Mode Confirmation */}
      <ConfirmDialog
        open={liveTarget !== null}
        variant="warning"
        title="Switch to Live Trading"
        description="This instance will start placing real orders with real money. Make sure you have tested it thoroughly in Paper mode first. You can switch back to Paper or Disabled at any time."
        confirmLabel="Go Live"
        cancelLabel="Stay in Paper"
        loading={modeMutation.isPending}
        onConfirm={() => {
          if (liveTarget) {
            modeMutation.mutate({ instanceId: liveTarget, mode: "live" }, { onSuccess: () => setLiveTarget(null) });
          }
        }}
        onCancel={() => setLiveTarget(null)}
      />
    </div>
  );
}
