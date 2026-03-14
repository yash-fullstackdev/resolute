"use client";

import type { ReactNode } from "react";
import { useAuthStore } from "@/stores/authStore";
import type { CapitalTier } from "@/types/strategy";
import { TIER_ORDER, TIER_NAMES } from "@/lib/constants";
import { Lock } from "lucide-react";

interface TierGateProps {
  requiredTier: CapitalTier;
  children: ReactNode;
  fallback?: ReactNode;
}

export function TierGate({ requiredTier, children, fallback }: TierGateProps) {
  const userTier = useAuthStore((s) => s.tier);
  const userTierOrder = TIER_ORDER[userTier] ?? 0;
  const requiredTierOrder = TIER_ORDER[requiredTier] ?? 0;

  if (userTierOrder >= requiredTierOrder) {
    return <>{children}</>;
  }

  if (fallback) {
    return <>{fallback}</>;
  }

  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-lg border border-surface-border bg-surface-dark/50 p-8 text-center">
      <Lock className="h-8 w-8 text-slate-500" />
      <p className="text-sm text-slate-400">
        Requires <span className="font-semibold text-accent-light">{TIER_NAMES[requiredTier]}</span> tier or higher
      </p>
      <button className="mt-2 rounded-md bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-light transition-colors">
        Upgrade Plan
      </button>
    </div>
  );
}
