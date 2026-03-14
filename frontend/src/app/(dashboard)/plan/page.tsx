"use client";

import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { TradingPlan } from "@/types/discipline";
import type { ApiResponse } from "@/types/api";
import { STRATEGY_NAMES, UNDERLYINGS } from "@/lib/constants";
import { formatINR, formatDateIST } from "@/lib/formatters";
import { Lock, Unlock, ClipboardList, Hash } from "lucide-react";

const ALL_STRATEGIES = Object.keys(STRATEGY_NAMES);

export default function PlanPage() {
  const queryClient = useQueryClient();

  const { data: plan, isLoading } = useQuery<TradingPlan | null>({
    queryKey: ["today-plan"],
    queryFn: async () => {
      try {
        const res = await apiClient.get<ApiResponse<TradingPlan>>("/plan");
        return res.data.data;
      } catch {
        return null;
      }
    },
  });

  const [enabledStrategies, setEnabledStrategies] = useState<string[]>([]);
  const [activeUnderlyings, setActiveUnderlyings] = useState<string[]>([]);
  const [maxTrades, setMaxTrades] = useState(5);
  const [dailyLossLimit, setDailyLossLimit] = useState(5000);
  const [dailyProfitTarget, setDailyProfitTarget] = useState(10000);
  const [thesis, setThesis] = useState("");

  useEffect(() => {
    if (plan) {
      setEnabledStrategies(plan.enabled_strategies);
      setActiveUnderlyings(plan.active_underlyings);
      setMaxTrades(plan.max_trades_per_day ?? plan.max_trades);
      setDailyLossLimit(plan.daily_loss_limit_inr ?? plan.daily_loss_limit);
      setDailyProfitTarget(plan.daily_profit_target_inr ?? plan.daily_profit_target ?? 10000);
      setThesis(plan.notes ?? plan.thesis ?? "");
    }
  }, [plan]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        enabled_strategies: enabledStrategies,
        active_underlyings: activeUnderlyings,
        max_trades_per_day: maxTrades,
        daily_loss_limit_inr: dailyLossLimit,
        daily_profit_target_inr: dailyProfitTarget,
        notes: thesis,
      };
      await apiClient.post("/plan", payload);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["today-plan"] });
    },
  });

  const lockMutation = useMutation({
    mutationFn: async () => {
      await apiClient.post("/plan/lock");
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["today-plan"] });
    },
  });

  const toggleStrategy = (name: string) => {
    setEnabledStrategies((prev) =>
      prev.includes(name) ? prev.filter((s) => s !== name) : [...prev, name]
    );
  };

  const toggleUnderlying = (name: string) => {
    setActiveUnderlyings((prev) =>
      prev.includes(name) ? prev.filter((u) => u !== name) : [...prev, name]
    );
  };

  const isLocked = plan?.is_locked ?? false;
  const today = new Date().toLocaleDateString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
  });

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Today&apos;s Trading Plan</h1>
          <p className="mt-1 text-sm text-slate-400">{today}</p>
        </div>
        {isLocked && (
          <div className="flex items-center gap-2 rounded-full bg-amber-500/10 px-3 py-1 text-xs font-medium text-amber-400">
            <Lock className="h-3 w-3" />
            Locked {plan?.locked_at ? `at ${formatDateIST(plan.locked_at)}` : ""}
          </div>
        )}
      </div>

      {/* Enabled strategies */}
      <div className="rounded-xl border border-surface-border bg-surface p-4">
        <h2 className="mb-3 text-sm font-semibold text-white">Enabled Strategies</h2>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          {ALL_STRATEGIES.map((name) => (
            <label
              key={name}
              className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors ${
                enabledStrategies.includes(name)
                  ? "border-accent/30 bg-accent/5 text-white"
                  : "border-surface-border text-slate-400 hover:border-surface-light"
              } ${isLocked ? "pointer-events-none opacity-60" : ""}`}
            >
              <input
                type="checkbox"
                checked={enabledStrategies.includes(name)}
                onChange={() => toggleStrategy(name)}
                disabled={isLocked}
                className="rounded border-surface-border bg-surface-dark text-accent focus:ring-accent"
              />
              {STRATEGY_NAMES[name] ?? name}
            </label>
          ))}
        </div>
      </div>

      {/* Active underlyings */}
      <div className="rounded-xl border border-surface-border bg-surface p-4">
        <h2 className="mb-3 text-sm font-semibold text-white">Active Underlyings</h2>
        <div className="flex flex-wrap gap-2">
          {UNDERLYINGS.map((name) => (
            <button
              key={name}
              onClick={() => toggleUnderlying(name)}
              disabled={isLocked}
              className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors ${
                activeUnderlyings.includes(name)
                  ? "border-accent/30 bg-accent/5 text-accent-light"
                  : "border-surface-border text-slate-400 hover:text-white"
              } ${isLocked ? "pointer-events-none opacity-60" : ""}`}
            >
              {name}
            </button>
          ))}
        </div>
      </div>

      {/* Limits */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <label className="mb-1 block text-xs text-slate-400">Max trades today</label>
          <input
            type="number"
            value={maxTrades}
            onChange={(e) => setMaxTrades(Number(e.target.value))}
            disabled={isLocked}
            className="w-full rounded-md border border-surface-border bg-surface-dark px-3 py-2 text-sm text-white focus:border-accent focus:outline-none disabled:opacity-60"
          />
        </div>
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <label className="mb-1 block text-xs text-slate-400">Daily loss limit</label>
          <div className="relative">
            <span className="absolute left-3 top-2 text-sm text-slate-500">₹</span>
            <input
              type="number"
              value={dailyLossLimit}
              onChange={(e) => setDailyLossLimit(Number(e.target.value))}
              disabled={isLocked}
              className="w-full rounded-md border border-surface-border bg-surface-dark py-2 pl-7 pr-3 text-sm text-white focus:border-accent focus:outline-none disabled:opacity-60"
            />
          </div>
        </div>
        <div className="rounded-xl border border-surface-border bg-surface p-4">
          <label className="mb-1 block text-xs text-slate-400">Profit target (optional)</label>
          <div className="relative">
            <span className="absolute left-3 top-2 text-sm text-slate-500">₹</span>
            <input
              type="number"
              value={dailyProfitTarget}
              onChange={(e) => setDailyProfitTarget(Number(e.target.value))}
              disabled={isLocked}
              className="w-full rounded-md border border-surface-border bg-surface-dark py-2 pl-7 pr-3 text-sm text-white focus:border-accent focus:outline-none disabled:opacity-60"
            />
          </div>
        </div>
      </div>

      {/* Thesis */}
      <div className="rounded-xl border border-surface-border bg-surface p-4">
        <label className="mb-2 block text-sm font-semibold text-white">Pre-market Thesis</label>
        <textarea
          value={thesis}
          onChange={(e) => setThesis(e.target.value)}
          disabled={isLocked}
          rows={4}
          placeholder="What's your view on the market today? What setups are you watching?"
          className="w-full rounded-md border border-surface-border bg-surface-dark px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none disabled:opacity-60"
        />
      </div>

      {/* Plan hash */}
      {plan?.plan_hash && (
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <Hash className="h-3 w-3" />
          Plan hash: {plan.plan_hash}
        </div>
      )}

      {/* Actions */}
      {!isLocked && (
        <div className="flex gap-3">
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className="flex-1 rounded-lg bg-accent py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent-light disabled:opacity-50"
          >
            {saveMutation.isPending ? "Saving..." : plan ? "Update Plan" : "Create Plan"}
          </button>
          {plan && (
            <button
              onClick={() => {
                if (confirm("Lock this plan? It cannot be modified until market close.")) {
                  lockMutation.mutate();
                }
              }}
              disabled={lockMutation.isPending}
              className="flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2.5 text-sm font-semibold text-amber-400 transition-colors hover:bg-amber-500/20 disabled:opacity-50"
            >
              <Lock className="h-4 w-4" />
              Lock Plan
            </button>
          )}
        </div>
      )}
    </div>
  );
}
