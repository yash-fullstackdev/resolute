"use client";

import type { Strategy, StrategyInstance } from "@/types/strategy";
import { TIER_NAMES, TIER_COLORS } from "@/lib/constants";
import { Settings, Plus, Zap, FileText, Power, Trash2, Activity, Clock, TrendingUp, Shield } from "lucide-react";

interface StrategyCardProps {
  strategy: Strategy;
  instanceStatuses?: Record<string, unknown>[];
  onConfigureInstance?: (strategy: Strategy, instance: StrategyInstance) => void;
  onAddInstance?: (strategy: Strategy) => void;
  onDeleteInstance?: (instanceId: string) => void;
  onModeChange?: (instanceId: string, mode: "live" | "paper" | "disabled") => void;
}

const MODE_CONFIG = {
  live: { bg: "bg-profit", text: "text-white", ring: "ring-profit/50", label: "LIVE", icon: Zap, dot: "bg-profit" },
  paper: { bg: "bg-amber-500", text: "text-white", ring: "ring-amber-500/50", label: "PAPER", icon: FileText, dot: "bg-amber-400" },
  disabled: { bg: "bg-slate-700", text: "text-slate-400", ring: "ring-slate-600", label: "OFF", icon: Power, dot: "bg-slate-600" },
} as const;

const SESSION_SHORT = { morning: "Morning", afternoon: "Afternoon", all: "All Day" } as const;

export function StrategyCard({
  strategy, instanceStatuses = [], onConfigureInstance, onAddInstance, onDeleteInstance, onModeChange,
}: StrategyCardProps) {
  const tierClass = TIER_COLORS[strategy.min_capital_tier] ?? "bg-slate-600 text-slate-200";
  const instances = strategy.instances ?? [];
  const activeCount = instances.filter((i) => i.mode !== "disabled").length;

  return (
    <div className="rounded-2xl border border-surface-border bg-surface overflow-hidden">
      {/* Strategy Header */}
      <div className="px-5 pt-4 pb-3">
        <div className="flex items-start justify-between">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="text-sm font-bold text-white">{strategy.display_name}</h3>
              <span className={`rounded-full px-2 py-0.5 text-[9px] font-bold uppercase tracking-wide ${tierClass}`}>
                {TIER_NAMES[strategy.min_capital_tier] ?? strategy.min_capital_tier}
              </span>
            </div>
            <p className="mt-1 text-[11px] text-slate-500 line-clamp-1">{strategy.description}</p>
          </div>
          {activeCount > 0 && (
            <div className="flex items-center gap-1 rounded-full bg-profit/10 px-2.5 py-1 shrink-0 ml-2">
              <Activity className="h-3 w-3 text-profit" />
              <span className="text-[10px] font-bold text-profit">{activeCount}</span>
            </div>
          )}
        </div>
      </div>

      {/* Instances */}
      <div className="px-3 pb-3 space-y-2">
        {instances.map((inst) => {
          const mc = MODE_CONFIG[inst.mode] ?? MODE_CONFIG.disabled;
          const MIcon = mc.icon;
          const status = instanceStatuses.find(
            (s) => (s as Record<string, unknown>).instance_id === inst.instance_id
          ) as Record<string, unknown> | undefined;
          const isRunning = status?.running === true;
          const reason = status?.reason as string | undefined;
          const lastEval = status?.last_evaluated_ago_s as number | undefined;
          const signalsToday = (status?.signals_today as number) ?? 0;
          const dailyPnl = (status?.daily_pnl as number) ?? 0;
          const candleStatus = status?.candle_status as Record<string, Record<string, unknown>> | undefined;

          return (
            <div key={inst.instance_id}
              className={`rounded-xl border p-3.5 transition-all ${
                inst.mode === "live" ? "border-profit/30 bg-profit/[0.03]" :
                inst.mode === "paper" ? "border-amber-500/20 bg-amber-500/[0.02]" :
                "border-surface-border/40 bg-surface-dark/30"
              }`}>

              {/* Instance Header Row */}
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[9px] font-bold ${mc.bg} ${mc.text}`}>
                    <MIcon className="h-2.5 w-2.5" />
                    {mc.label}
                  </span>
                  <span className="text-xs font-semibold text-white truncate">{inst.instance_name}</span>
                </div>
                <div className="flex items-center gap-0.5 shrink-0">
                  <button onClick={(e) => { e.stopPropagation(); onConfigureInstance?.(strategy, inst); }}
                    className="rounded-lg p-1.5 text-slate-600 hover:bg-surface-light hover:text-white transition-colors" title="Edit">
                    <Settings className="h-3.5 w-3.5" />
                  </button>
                  <button onClick={(e) => { e.stopPropagation(); onDeleteInstance?.(inst.instance_id); }}
                    className="rounded-lg p-1.5 text-slate-700 hover:bg-loss/10 hover:text-loss transition-colors" title="Delete">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>

              {/* Info Row */}
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {inst.instruments.length > 0 && inst.instruments.map((sym) => (
                  <span key={sym} className="rounded-md bg-surface-dark px-2 py-0.5 text-[10px] font-medium text-slate-300">
                    {sym}
                  </span>
                ))}
                <span className="rounded-md bg-surface-dark/50 px-2 py-0.5 text-[10px] text-slate-500">
                  {SESSION_SHORT[inst.session] ?? inst.session}
                </span>
                {inst.bias_config?.mode === "bias_filtered" && inst.bias_config.bias_filters.length > 0 && (
                  <span className="inline-flex items-center gap-0.5 rounded-md bg-accent/10 px-2 py-0.5 text-[10px] text-accent-light">
                    <Shield className="h-2.5 w-2.5" />
                    Bias ({inst.bias_config.bias_filters.length})
                  </span>
                )}
                {inst.max_daily_loss_pts != null && (
                  <span className="rounded-md bg-loss/10 px-2 py-0.5 text-[10px] text-loss/80">
                    SL: {String(inst.max_daily_loss_pts)}pts/day
                  </span>
                )}
              </div>

              {/* Status + Mode Row */}
              <div className="mt-2.5 flex items-center justify-between gap-2">
                {/* Live Status */}
                <div className="flex items-center gap-2 text-[10px] min-w-0">
                  {inst.mode !== "disabled" && status && (
                    <>
                      <span className={`inline-flex items-center gap-1 font-semibold ${isRunning ? "text-profit" : "text-slate-500"}`}>
                        <span className={`h-1.5 w-1.5 rounded-full ${isRunning ? "bg-profit animate-pulse" : "bg-slate-600"}`} />
                        {isRunning ? "Running" : "Stopped"}
                      </span>
                      {isRunning && lastEval != null && lastEval >= 0 && (
                        <span className="text-slate-600 flex items-center gap-0.5">
                          <Clock className="h-2.5 w-2.5" />
                          {lastEval < 60 ? `${Math.round(lastEval)}s` : `${Math.round(lastEval / 60)}m`}
                        </span>
                      )}
                      {!isRunning && reason && (
                        <span className="text-slate-600 truncate max-w-[180px]" title={reason}>{reason}</span>
                      )}
                      {signalsToday > 0 && (
                        <span className="inline-flex items-center gap-0.5 text-white font-medium">
                          <TrendingUp className="h-2.5 w-2.5" />
                          {signalsToday}
                        </span>
                      )}
                      {dailyPnl !== 0 && (
                        <span className={`font-bold ${dailyPnl >= 0 ? "text-profit" : "text-loss"}`}>
                          {dailyPnl >= 0 ? "+" : ""}{dailyPnl}pts
                        </span>
                      )}
                    </>
                  )}
                </div>

                {/* Mode Buttons */}
                <div className="flex items-center rounded-lg border border-surface-border/50 p-0.5 shrink-0">
                  {(["disabled", "paper", "live"] as const).map((m) => {
                    const isActive = inst.mode === m;
                    const cfg = MODE_CONFIG[m];
                    return (
                      <button key={m}
                        onClick={(e) => { e.stopPropagation(); onModeChange?.(inst.instance_id, m); }}
                        className={`rounded-md px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider transition-all ${
                          isActive ? `${cfg.bg} ${cfg.text} shadow-sm` : "text-slate-600 hover:text-slate-400"
                        }`}>
                        {cfg.label}
                      </button>
                    );
                  })}
                </div>
              </div>

              {/* Candle Health — compact pills */}
              {candleStatus && Object.keys(candleStatus).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {Object.entries(candleStatus).map(([sym, info]) => {
                    const bars = Number(info.bars_5m ?? 0);
                    const stale = info.tick_stale === true;
                    const ok = bars >= 15 && !stale;
                    return (
                      <span key={sym} className={`rounded px-1.5 py-0.5 text-[9px] font-mono ${
                        ok ? "bg-profit/8 text-profit/70" : stale ? "bg-loss/8 text-loss/70" : "bg-amber-400/8 text-amber-400/70"
                      }`}>
                        {sym} {String(bars)}b {ok ? "✓" : stale ? "!" : "~"}
                      </span>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}

        {/* Add Instance */}
        <button onClick={() => onAddInstance?.(strategy)}
          className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-surface-border/50 py-2.5 text-[11px] text-slate-600 hover:border-accent/40 hover:text-accent-light transition-all">
          <Plus className="h-3.5 w-3.5" /> New Instance
        </button>
      </div>
    </div>
  );
}
