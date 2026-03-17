"use client";

import { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { Strategy } from "@/types/strategy";
import { TIER_COLORS, TIER_NAMES } from "@/lib/constants";
import { X, ToggleLeft, ToggleRight, Check } from "lucide-react";

const AVAILABLE_INSTRUMENTS = [
  "NIFTY",
  "BANKNIFTY",
  "FINNIFTY",
  "MIDCPNIFTY",
  "RELIANCE",
  "HDFCBANK",
  "INFY",
  "TCS",
  "ICICIBANK",
  "SBIN",
];

interface StrategyConfigModalProps {
  strategy: Strategy;
  onClose: () => void;
}

export function StrategyConfigModal({ strategy, onClose }: StrategyConfigModalProps) {
  const queryClient = useQueryClient();
  const [enabled, setEnabled] = useState(strategy.enabled);
  const [selectedInstruments, setSelectedInstruments] = useState<string[]>(
    strategy.instruments ?? []
  );
  const [paramValues, setParamValues] = useState<Record<string, number | string>>({});
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const initial: Record<string, number | string> = {};
    for (const p of strategy.params) {
      initial[p.name] = p.current_value as number;
    }
    setParamValues(initial);
  }, [strategy.params]);

  const saveMutation = useMutation({
    mutationFn: async () => {
      await apiClient.patch(`/strategies/${strategy.id}`, {
        enabled,
        instruments: selectedInstruments,
        params: paramValues,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  const toggleInstrument = (inst: string) => {
    setSelectedInstruments((prev) =>
      prev.includes(inst) ? prev.filter((i) => i !== inst) : [...prev, inst]
    );
  };

  const handleParamChange = (name: string, value: string) => {
    const num = parseFloat(value);
    if (!isNaN(num)) {
      setParamValues((prev) => ({ ...prev, [name]: num }));
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="relative w-full max-w-lg rounded-xl border border-surface-border bg-surface-dark p-6 shadow-2xl">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-bold text-white">{strategy.display_name}</h2>
              <span
                className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                  TIER_COLORS[strategy.min_capital_tier] ?? "bg-slate-600 text-slate-200"
                }`}
              >
                {TIER_NAMES[strategy.min_capital_tier] ?? strategy.min_capital_tier}
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-400">{strategy.description}</p>
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-slate-400 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* Enable toggle */}
        <div className="mt-5 flex items-center justify-between rounded-lg border border-surface-border bg-surface p-3">
          <span className="text-sm font-medium text-white">Enable Strategy</span>
          <button
            onClick={() => setEnabled(!enabled)}
            className={`transition-colors ${enabled ? "text-profit" : "text-slate-500"}`}
          >
            {enabled ? <ToggleRight className="h-7 w-7" /> : <ToggleLeft className="h-7 w-7" />}
          </button>
        </div>

        {/* Instrument selection */}
        <div className="mt-4">
          <label className="text-sm font-medium text-slate-300">Select Instruments</label>
          <p className="mb-2 text-[11px] text-slate-500">
            Choose which underlyings this strategy should monitor
          </p>
          <div className="flex flex-wrap gap-2">
            {AVAILABLE_INSTRUMENTS.map((inst) => {
              const isSelected = selectedInstruments.includes(inst);
              return (
                <button
                  key={inst}
                  onClick={() => toggleInstrument(inst)}
                  className={`rounded-md border px-3 py-1.5 text-xs font-medium transition-all ${
                    isSelected
                      ? "border-accent bg-accent/20 text-accent-light"
                      : "border-surface-border bg-surface text-slate-400 hover:border-slate-500 hover:text-white"
                  }`}
                >
                  {inst}
                  {isSelected && <Check className="ml-1 inline h-3 w-3" />}
                </button>
              );
            })}
          </div>
        </div>

        {/* Params */}
        {strategy.params.length > 0 && (
          <div className="mt-4">
            <label className="text-sm font-medium text-slate-300">Parameters</label>
            <div className="mt-2 grid grid-cols-2 gap-3">
              {strategy.params.map((p) => (
                <div key={p.name}>
                  <label className="block text-[11px] text-slate-500">{p.description}</label>
                  <input
                    type="number"
                    value={paramValues[p.name] ?? (typeof p.default_value === "boolean" ? 0 : p.default_value)}
                    onChange={(e) => handleParamChange(p.name, e.target.value)}
                    min={p.min}
                    max={p.max}
                    step={typeof p.default_value === "number" && p.default_value < 1 ? 0.01 : 1}
                    className="mt-0.5 w-full rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                  />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Save */}
        <div className="mt-6 flex items-center justify-end gap-3">
          <button
            onClick={onClose}
            className="rounded-lg border border-surface-border px-4 py-2 text-sm text-slate-400 hover:text-white"
          >
            Cancel
          </button>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white transition-colors ${
              saved
                ? "bg-profit"
                : "bg-accent hover:bg-accent-light"
            } disabled:opacity-50`}
          >
            {saveMutation.isPending ? (
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
            ) : saved ? (
              <>
                <Check className="h-4 w-4" />
                Saved
              </>
            ) : (
              "Save Configuration"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
