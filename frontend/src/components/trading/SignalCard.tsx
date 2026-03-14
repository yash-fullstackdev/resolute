"use client";

import type { Signal } from "@/types/trading";
import { STRATEGY_NAMES, REGIME_COLORS } from "@/lib/constants";
import { formatTimeIST } from "@/lib/formatters";

interface SignalCardProps {
  signal: Signal;
}

export function SignalCard({ signal }: SignalCardProps) {
  const regimeClass = REGIME_COLORS[signal.regime] ?? "text-slate-400 bg-slate-400/10";
  const strengthPct = Math.round(signal.strength * 100);

  return (
    <div className="rounded-lg border border-surface-border bg-surface p-4">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-white">{signal.direction}</span>
          <span className="text-sm text-slate-300">{signal.underlying}</span>
        </div>
        <span className="text-xs text-slate-500">{formatTimeIST(signal.created_at)}</span>
      </div>

      <div className="mt-2 flex items-center gap-3">
        <span className="rounded bg-surface-light px-2 py-0.5 text-xs text-slate-400">
          {STRATEGY_NAMES[signal.strategy_name] ?? signal.strategy_name}
        </span>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${regimeClass}`}>
          {signal.regime}
        </span>
        {signal.executed && (
          <span className="rounded bg-profit/10 px-2 py-0.5 text-xs font-medium text-profit">
            Executed
          </span>
        )}
      </div>

      {/* Strength bar */}
      <div className="mt-3">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-400">Strength</span>
          <span className="tabular-nums text-white">{strengthPct}%</span>
        </div>
        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-light">
          <div
            className={`h-full rounded-full transition-all ${
              strengthPct >= 70
                ? "bg-profit"
                : strengthPct >= 40
                  ? "bg-amber-400"
                  : "bg-loss"
            }`}
            style={{ width: `${strengthPct}%` }}
          />
        </div>
      </div>

      {/* Legs */}
      {signal.legs.length > 0 && (
        <div className="mt-3 space-y-1 border-t border-surface-border pt-2">
          {signal.legs.map((leg, idx) => (
            <div key={idx} className="text-xs text-slate-400">
              {leg.action} {leg.lots}x {leg.option_type} {leg.strike} ({leg.expiry})
            </div>
          ))}
        </div>
      )}

      {signal.rationale && (
        <p className="mt-2 text-xs text-slate-500">{signal.rationale}</p>
      )}
    </div>
  );
}
