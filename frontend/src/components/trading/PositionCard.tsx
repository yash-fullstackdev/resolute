"use client";

import type { Position } from "@/types/trading";
import { STRATEGY_NAMES } from "@/lib/constants";
import { formatINR, formatPercentage, pnlColorClass, pnlBgClass } from "@/lib/formatters";
import { formatTimeIST } from "@/lib/formatters";
import { X } from "lucide-react";

interface PositionCardProps {
  position: Position;
  onExit?: (positionId: string) => void;
  isExiting?: boolean;
}

export function PositionCard({ position, onExit, isExiting }: PositionCardProps) {
  const pnl = position.unrealized_pnl;
  const pnlPct = position.total_pnl_pct;

  return (
    <div className="rounded-lg border border-surface-border bg-surface p-4 transition-colors hover:bg-surface-light">
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-white">{position.underlying}</span>
            <span className="rounded bg-surface-light px-2 py-0.5 text-xs text-slate-400">
              {STRATEGY_NAMES[position.strategy_name] ?? position.strategy_name}
            </span>
          </div>
          <p className="mt-1 text-xs text-slate-500">
            {position.direction} | {formatTimeIST(position.entry_time)}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right">
            <p className={`text-lg font-bold tabular-nums ${pnlColorClass(pnl)}`}>
              {formatINR(pnl, true)}
            </p>
            <span className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium ${pnlBgClass(pnlPct)}`}>
              {formatPercentage(pnlPct)}
            </span>
          </div>

          {onExit && position.status === "OPEN" && (
            <button
              onClick={() => onExit(position.id)}
              disabled={isExiting}
              className="rounded-md border border-loss/30 p-2 text-loss transition-colors hover:bg-loss/10 disabled:opacity-50"
              title="Exit position"
            >
              <X className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>

      {/* Legs */}
      {position.legs.length > 0 && (
        <div className="mt-3 space-y-1 border-t border-surface-border pt-2">
          {position.legs.map((leg, idx) => (
            <div key={idx} className="flex items-center justify-between text-xs text-slate-400">
              <span>
                {leg.action} {leg.option_type} {leg.strike} ({leg.expiry})
              </span>
              <span className={`tabular-nums ${pnlColorClass(leg.pnl)}`}>
                {formatINR(leg.pnl, true)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
