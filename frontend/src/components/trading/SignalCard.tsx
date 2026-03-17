"use client";

import type { Signal } from "@/types/trading";
import { STRATEGY_NAMES, REGIME_COLORS } from "@/lib/constants";
import { formatTimeIST } from "@/lib/formatters";

interface SignalCardProps {
  signal: Signal;
}

function fmt(n: number | null | undefined) {
  if (n == null) return "—";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(n);
}

export function SignalCard({ signal }: SignalCardProps) {
  const regimeClass = REGIME_COLORS[signal.regime] ?? "text-slate-400 bg-slate-400/10";
  const strengthPct = Math.round((signal.strength ?? 0) * 100);
  const isDirect = signal.signal_type === "DIRECT" || signal.legs.length === 0;
  const isBullish = signal.direction === "BULLISH" || signal.direction === "BUY_CALL";

  return (
    <div className={`rounded-lg border bg-surface p-4 ${isDirect ? "border-amber-500/30" : "border-surface-border"}`}>
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-2">
          <span className={`text-sm font-bold ${isBullish ? "text-profit" : "text-loss"}`}>
            {isBullish ? "▲" : "▼"} {signal.direction}
          </span>
          <span className="text-sm font-semibold text-white">{signal.underlying}</span>
          {isDirect && (
            <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-amber-400">
              DIRECT
            </span>
          )}
        </div>
        <span className="text-xs text-slate-500">{formatTimeIST(signal.created_at)}</span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2">
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
          <span className="text-slate-400">Confidence</span>
          <span className="tabular-nums text-white">{strengthPct}%</span>
        </div>
        <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-light">
          <div
            className={`h-full rounded-full transition-all ${
              strengthPct >= 70 ? "bg-profit" : strengthPct >= 40 ? "bg-amber-400" : "bg-loss"
            }`}
            style={{ width: `${strengthPct}%` }}
          />
        </div>
      </div>

      {/* DIRECT signal — price-based entry/target/stop */}
      {isDirect && (
        <div className="mt-3 grid grid-cols-3 gap-2 rounded-md border border-surface-border bg-surface-dark p-2">
          <div className="text-center">
            <p className="text-[10px] text-slate-500">Entry</p>
            <p className="text-xs font-semibold text-white">₹{fmt(signal.entry_price)}</p>
          </div>
          <div className="text-center">
            <p className="text-[10px] text-slate-500">Target</p>
            <p className="text-xs font-semibold text-profit">₹{fmt(signal.target_price)}</p>
          </div>
          <div className="text-center">
            <p className="text-[10px] text-slate-500">Stop</p>
            <p className="text-xs font-semibold text-loss">₹{fmt(signal.stop_loss_price)}</p>
          </div>
        </div>
      )}

      {/* OPTIONS signal — option legs */}
      {!isDirect && signal.legs.length > 0 && (
        <div className="mt-3 space-y-1 border-t border-surface-border pt-2">
          {signal.legs.map((leg, idx) => (
            <div key={idx} className="flex items-center justify-between text-xs">
              <span className={leg.action === "BUY" ? "text-profit" : "text-loss"}>
                {leg.action} {leg.lots}× {leg.option_type} {leg.strike}
              </span>
              <span className="text-slate-500">{leg.expiry}</span>
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
