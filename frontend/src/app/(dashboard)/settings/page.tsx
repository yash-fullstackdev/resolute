"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { TIER_NAMES, TIER_COLORS } from "@/lib/constants";
import { User, Link2, CreditCard, Save, Check, AlertCircle } from "lucide-react";

export default function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const tier = useAuthStore((s) => s.tier);

  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [brokerApiKey, setBrokerApiKey] = useState("");
  const [brokerApiSecret, setBrokerApiSecret] = useState("");

  const profileMutation = useMutation({
    mutationFn: async () => {
      await apiClient.put("/user/profile", { full_name: fullName });
    },
  });

  const brokerMutation = useMutation({
    mutationFn: async () => {
      await apiClient.post("/user/broker/connect", {
        api_key: brokerApiKey,
        api_secret: brokerApiSecret,
      });
    },
  });

  const tierClass = TIER_COLORS[tier] ?? "bg-slate-600 text-slate-200";

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <h1 className="text-2xl font-bold text-white">Settings</h1>

      {/* Profile */}
      <div className="rounded-xl border border-surface-border bg-surface p-6">
        <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
          <User className="h-4 w-4 text-accent-light" />
          Profile
        </h2>
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-xs text-slate-400">Email</label>
            <input
              type="email"
              value={user?.email ?? ""}
              disabled
              className="w-full rounded-lg border border-surface-border bg-surface-dark px-4 py-2.5 text-sm text-slate-400"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-400">Full Name</label>
            <input
              type="text"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              className="w-full rounded-lg border border-surface-border bg-surface-dark px-4 py-2.5 text-sm text-white focus:border-accent focus:outline-none"
            />
          </div>
          <button
            onClick={() => profileMutation.mutate()}
            disabled={profileMutation.isPending}
            className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-light disabled:opacity-50"
          >
            {profileMutation.isSuccess ? (
              <>
                <Check className="h-4 w-4" /> Saved
              </>
            ) : (
              <>
                <Save className="h-4 w-4" /> {profileMutation.isPending ? "Saving..." : "Save Profile"}
              </>
            )}
          </button>
        </div>
      </div>

      {/* Broker connection */}
      <div className="rounded-xl border border-surface-border bg-surface p-6">
        <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
          <Link2 className="h-4 w-4 text-accent-light" />
          Broker Connection
        </h2>
        <div className="mb-4 flex items-center gap-2">
          <span
            className={`rounded-full px-2 py-0.5 text-xs font-medium ${
              user?.broker_connected
                ? "bg-profit/10 text-profit"
                : "bg-loss/10 text-loss"
            }`}
          >
            {user?.broker_connected ? "Connected" : "Not Connected"}
          </span>
        </div>
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-xs text-slate-400">API Key</label>
            <input
              type="text"
              value={brokerApiKey}
              onChange={(e) => setBrokerApiKey(e.target.value)}
              placeholder="Enter broker API key"
              className="w-full rounded-lg border border-surface-border bg-surface-dark px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-400">API Secret</label>
            <input
              type="password"
              value={brokerApiSecret}
              onChange={(e) => setBrokerApiSecret(e.target.value)}
              placeholder="Enter broker API secret"
              className="w-full rounded-lg border border-surface-border bg-surface-dark px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
            />
          </div>
          {brokerMutation.isError && (
            <div className="flex items-center gap-2 rounded-lg border border-loss/30 bg-loss/10 px-3 py-2 text-xs text-loss">
              <AlertCircle className="h-4 w-4" />
              Connection failed. Please check your credentials.
            </div>
          )}
          <button
            onClick={() => brokerMutation.mutate()}
            disabled={brokerMutation.isPending || !brokerApiKey || !brokerApiSecret}
            className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-light disabled:opacity-50"
          >
            {brokerMutation.isSuccess ? (
              <>
                <Check className="h-4 w-4" /> Connected
              </>
            ) : (
              <>
                <Link2 className="h-4 w-4" /> {brokerMutation.isPending ? "Connecting..." : "Connect Broker"}
              </>
            )}
          </button>
        </div>
      </div>

      {/* Subscription */}
      <div className="rounded-xl border border-surface-border bg-surface p-6">
        <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold text-white">
          <CreditCard className="h-4 w-4 text-accent-light" />
          Subscription
        </h2>
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm text-slate-300">Current Plan</p>
            <span className={`mt-1 inline-block rounded-full px-3 py-1 text-sm font-semibold ${tierClass}`}>
              {TIER_NAMES[tier] ?? tier}
            </span>
          </div>
          <button className="rounded-lg border border-accent/30 bg-accent/10 px-4 py-2 text-sm font-semibold text-accent-light transition-colors hover:bg-accent/20">
            Upgrade Plan
          </button>
        </div>
      </div>
    </div>
  );
}
