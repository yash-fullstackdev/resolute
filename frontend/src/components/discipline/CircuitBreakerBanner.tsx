"use client";

import { AlertTriangle, ShieldOff } from "lucide-react";
import type { CircuitBreakerState } from "@/types/discipline";
import { formatINR } from "@/lib/formatters";

interface CircuitBreakerBannerProps {
  state: CircuitBreakerState;
}

export function CircuitBreakerBanner({ state }: CircuitBreakerBannerProps) {
  if (state.status === "ACTIVE") return null;

  const isHalted = state.status === "HALTED";

  return (
    <div
      className={`flex items-center gap-3 rounded-lg border px-4 py-3 ${
        isHalted
          ? "border-loss/40 bg-loss/10 text-loss"
          : "border-amber-500/40 bg-amber-500/10 text-amber-400"
      }`}
    >
      {isHalted ? (
        <ShieldOff className="h-5 w-5 flex-shrink-0" />
      ) : (
        <AlertTriangle className="h-5 w-5 flex-shrink-0" />
      )}

      <div className="flex-1">
        <p className="text-sm font-semibold">
          {isHalted ? "Trading HALTED" : "Circuit Breaker Cooldown"}
        </p>
        <p className="text-xs opacity-80">
          {state.reason ?? "Daily loss limit exceeded"}
          {" | "}
          Loss: {formatINR(state.daily_loss)} / {formatINR(state.daily_loss_limit)}
          {state.consecutive_losses > 0 &&
            ` | ${state.consecutive_losses} consecutive losses`}
        </p>
      </div>

      {state.resume_at && (
        <span className="text-xs opacity-70">
          Resumes at {new Date(state.resume_at).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" })} IST
        </span>
      )}
    </div>
  );
}
