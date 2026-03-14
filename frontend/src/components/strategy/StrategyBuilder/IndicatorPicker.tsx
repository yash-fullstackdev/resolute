"use client";

import { useState } from "react";
import type { IndicatorConfig, IndicatorCategory } from "@/types/strategy";
import { INDICATOR_NAMES } from "@/lib/constants";
import { Plus, Search } from "lucide-react";

const AVAILABLE_INDICATORS: Array<{
  name: string;
  category: IndicatorCategory;
  default_params: Record<string, number>;
}> = [
  { name: "RSI", category: "MOMENTUM", default_params: { period: 14 } },
  { name: "MACD", category: "MOMENTUM", default_params: { fast: 12, slow: 26, signal: 9 } },
  { name: "SUPERTREND", category: "TREND", default_params: { period: 10, multiplier: 3 } },
  { name: "BOLLINGER", category: "VOLATILITY", default_params: { period: 20, std_dev: 2 } },
  { name: "EMA", category: "TREND", default_params: { period: 20 } },
  { name: "SMA", category: "TREND", default_params: { period: 50 } },
  { name: "ATR", category: "VOLATILITY", default_params: { period: 14 } },
  { name: "ADX", category: "TREND", default_params: { period: 14 } },
  { name: "VWAP", category: "VOLUME", default_params: {} },
  { name: "OBV", category: "VOLUME", default_params: {} },
  { name: "STOCHASTIC", category: "MOMENTUM", default_params: { k_period: 14, d_period: 3 } },
  { name: "CCI", category: "MOMENTUM", default_params: { period: 20 } },
  { name: "MFI", category: "VOLUME", default_params: { period: 14 } },
  { name: "PCR", category: "CUSTOM", default_params: {} },
  { name: "IV_PERCENTILE", category: "VOLATILITY", default_params: { period: 252 } },
  { name: "IV_RANK", category: "VOLATILITY", default_params: { period: 252 } },
];

const CATEGORIES: IndicatorCategory[] = ["TREND", "MOMENTUM", "VOLATILITY", "VOLUME", "CUSTOM"];

interface IndicatorPickerProps {
  selectedIndicators: IndicatorConfig[];
  onAdd: (indicator: IndicatorConfig) => void;
  onRemove: (indicatorName: string) => void;
}

export function IndicatorPicker({ selectedIndicators, onAdd, onRemove }: IndicatorPickerProps) {
  const [search, setSearch] = useState("");
  const [activeCategory, setActiveCategory] = useState<IndicatorCategory | "ALL">("ALL");

  const filtered = AVAILABLE_INDICATORS.filter((ind) => {
    const matchesSearch =
      search === "" ||
      ind.name.toLowerCase().includes(search.toLowerCase()) ||
      (INDICATOR_NAMES[ind.name] ?? "").toLowerCase().includes(search.toLowerCase());
    const matchesCategory = activeCategory === "ALL" || ind.category === activeCategory;
    return matchesSearch && matchesCategory;
  });

  const selectedNames = new Set(selectedIndicators.map((i) => i.name));

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-white">Indicators</h3>

      {/* Selected indicators */}
      {selectedIndicators.length > 0 && (
        <div className="space-y-1">
          {selectedIndicators.map((ind) => (
            <div
              key={ind.name}
              className="flex items-center justify-between rounded-md border border-accent/20 bg-accent/5 px-3 py-2"
            >
              <div>
                <span className="text-sm text-white">{ind.display_name}</span>
                <span className="ml-2 text-xs text-slate-400">
                  ({Object.entries(ind.params).map(([k, v]) => `${k}=${v}`).join(", ")})
                </span>
              </div>
              <button
                onClick={() => onRemove(ind.name)}
                className="text-xs text-loss hover:text-loss-light"
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-2.5 h-4 w-4 text-slate-500" />
        <input
          type="text"
          placeholder="Search indicators..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full rounded-md border border-surface-border bg-surface-dark py-2 pl-9 pr-3 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
        />
      </div>

      {/* Category tabs */}
      <div className="flex flex-wrap gap-1">
        <button
          onClick={() => setActiveCategory("ALL")}
          className={`rounded-md px-2 py-1 text-xs font-medium transition-colors ${
            activeCategory === "ALL"
              ? "bg-accent text-white"
              : "bg-surface-light text-slate-400 hover:text-white"
          }`}
        >
          All
        </button>
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => setActiveCategory(cat)}
            className={`rounded-md px-2 py-1 text-xs font-medium transition-colors ${
              activeCategory === cat
                ? "bg-accent text-white"
                : "bg-surface-light text-slate-400 hover:text-white"
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      {/* Available indicators */}
      <div className="max-h-60 space-y-1 overflow-y-auto">
        {filtered.map((ind) => {
          const isSelected = selectedNames.has(ind.name);
          return (
            <button
              key={ind.name}
              disabled={isSelected}
              onClick={() => {
                onAdd({
                  name: ind.name,
                  display_name: INDICATOR_NAMES[ind.name] ?? ind.name,
                  category: ind.category,
                  params: { ...ind.default_params },
                  default_params: { ...ind.default_params },
                  description: "",
                });
              }}
              className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm transition-colors ${
                isSelected
                  ? "cursor-not-allowed bg-surface-light/50 text-slate-500"
                  : "bg-surface-dark text-slate-300 hover:bg-surface-light hover:text-white"
              }`}
            >
              <div>
                <span className="font-medium">{INDICATOR_NAMES[ind.name] ?? ind.name}</span>
                <span className="ml-2 text-xs text-slate-500">{ind.category}</span>
              </div>
              {!isSelected && <Plus className="h-4 w-4 text-accent-light" />}
            </button>
          );
        })}
      </div>
    </div>
  );
}
