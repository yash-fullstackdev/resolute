"use client";

import type { Strategy } from "@/types/strategy";
import { TIER_NAMES, TIER_COLORS } from "@/lib/constants";
import { formatPercentage } from "@/lib/formatters";
import { Settings, ToggleLeft, ToggleRight } from "lucide-react";

interface StrategyCardProps {
  strategy: Strategy;
  onToggle?: (strategyId: string, enabled: boolean) => void;
  onConfigure?: (strategy: Strategy) => void;
}

export function StrategyCard({ strategy, onToggle, onConfigure }: StrategyCardProps) {
  const tierClass = TIER_COLORS[strategy.min_capital_tier] ?? "bg-slate-600 text-slate-200";

  return (
    <div
      onClick={() => onConfigure?.(strategy)}
      className={`cursor-pointer rounded-lg border p-4 transition-colors hover:bg-surface-light ${
        strategy.enabled
          ? "border-accent/40 bg-surface"
          : "border-surface-border bg-surface"
      }`}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold text-white">{strategy.display_name}</h3>
            <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${tierClass}`}>
              {TIER_NAMES[strategy.min_capital_tier] ?? strategy.min_capital_tier}
            </span>
            {strategy.is_custom && (
              <span className="rounded bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent-light">
                Custom
              </span>
            )}
            {strategy.enabled && (
              <span className="rounded bg-profit/10 px-2 py-0.5 text-[10px] font-semibold text-profit">
                ACTIVE
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-slate-400">{strategy.description}</p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onConfigure?.(strategy);
            }}
            className="rounded-md p-1.5 text-slate-400 hover:bg-surface-light hover:text-white"
          >
            <Settings className="h-4 w-4" />
          </button>
          {onToggle && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onToggle(strategy.id, !strategy.enabled);
              }}
              className={`transition-colors ${
                strategy.enabled ? "text-profit" : "text-slate-500"
              }`}
            >
              {strategy.enabled ? (
                <ToggleRight className="h-6 w-6" />
              ) : (
                <ToggleLeft className="h-6 w-6" />
              )}
            </button>
          )}
        </div>
      </div>

      {/* Instruments */}
      {strategy.instruments && strategy.instruments.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {strategy.instruments.map((inst) => (
            <span
              key={inst}
              className="rounded bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent-light"
            >
              {inst}
            </span>
          ))}
        </div>
      )}

      {/* Stats */}
      {(strategy.win_rate !== undefined || strategy.total_trades !== undefined) && (
        <div className="mt-3 flex gap-4 border-t border-surface-border pt-2">
          {strategy.win_rate !== undefined && (
            <div className="text-xs">
              <span className="text-slate-500">Win Rate</span>
              <span className="ml-1 font-medium text-white">{formatPercentage(strategy.win_rate, 1)}</span>
            </div>
          )}
          {strategy.avg_return !== undefined && (
            <div className="text-xs">
              <span className="text-slate-500">Avg Return</span>
              <span className="ml-1 font-medium text-white">{formatPercentage(strategy.avg_return, 1)}</span>
            </div>
          )}
          {strategy.total_trades !== undefined && (
            <div className="text-xs">
              <span className="text-slate-500">Trades</span>
              <span className="ml-1 font-medium text-white">{strategy.total_trades}</span>
            </div>
          )}
        </div>
      )}

      {/* Category badge */}
      <div className="mt-2">
        <span
          className={`rounded px-2 py-0.5 text-[10px] font-medium ${
            strategy.category === "BUYING"
              ? "bg-profit/10 text-profit"
              : strategy.category === "SELLING"
                ? "bg-loss/10 text-loss"
                : strategy.category === "TECHNICAL"
                  ? "bg-cyan-400/10 text-cyan-400"
                  : "bg-amber-400/10 text-amber-400"
          }`}
        >
          {strategy.category}
        </span>
      </div>
    </div>
  );
}
