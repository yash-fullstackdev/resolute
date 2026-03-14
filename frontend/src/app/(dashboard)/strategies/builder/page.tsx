"use client";

import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { IndicatorPicker } from "@/components/strategy/StrategyBuilder/IndicatorPicker";
import { ConditionBuilder } from "@/components/strategy/StrategyBuilder/ConditionBuilder";
import { AIChat } from "@/components/strategy/StrategyBuilder/AIChat";
import type { IndicatorConfig, Condition, OptionConfig, CustomStrategyDefinition } from "@/types/strategy";
import type { ApiResponse } from "@/types/api";
import { UNDERLYINGS } from "@/lib/constants";
import { Wand2, Code2, Save, Play, Bot, Check } from "lucide-react";

type BuilderMode = "visual" | "ai";

const DEFAULT_OPTION_CONFIG: OptionConfig = {
  action: "BUY_CALL",
  strike_selection: "ATM",
  min_dte: 7,
  max_dte: 14,
  stop_loss_pct: 35,
  target_pct: 80,
  time_stop: "EOD",
};

export default function StrategyBuilderPage() {
  const [mode, setMode] = useState<BuilderMode>("visual");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [indicators, setIndicators] = useState<IndicatorConfig[]>([]);
  const [entryConditions, setEntryConditions] = useState<Condition[]>([]);
  const [exitConditions, setExitConditions] = useState<Condition[]>([]);
  const [selectedSymbols, setSelectedSymbols] = useState<string[]>(["NIFTY", "BANKNIFTY"]);
  const [optionConfig, setOptionConfig] = useState<OptionConfig>(DEFAULT_OPTION_CONFIG);

  const availableOperands = indicators.flatMap((ind) => {
    const paramKeys = Object.keys(ind.params);
    const suffix = paramKeys.length > 0 ? `_${paramKeys.map((k) => ind.params[k]).join("_")}` : "";
    return [`${ind.name}${suffix}`];
  });

  const allOperands = [...availableOperands, "Price", "Volume", "VWAP", "0", "20", "30", "50", "70", "80", "100"];

  const saveMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        name,
        description,
        indicators,
        entry_conditions: entryConditions,
        exit_conditions: exitConditions,
        symbols: selectedSymbols,
        option_config: optionConfig,
      };
      const res = await apiClient.post<ApiResponse<CustomStrategyDefinition>>(
        "/strategies/custom",
        payload
      );
      return res.data.data;
    },
  });

  const backtestMutation = useMutation({
    mutationFn: async () => {
      const payload = {
        name,
        indicators,
        entry_conditions: entryConditions,
        exit_conditions: exitConditions,
        symbols: selectedSymbols,
        option_config: optionConfig,
      };
      const res = await apiClient.post<ApiResponse<Record<string, unknown>>>(
        "/strategies/custom/backtest",
        payload
      );
      return res.data.data;
    },
  });

  const handleAIGenerated = (strategy: Record<string, unknown>) => {
    if (strategy.name) setName(strategy.name as string);
    if (strategy.description) setDescription(strategy.description as string);
    if (strategy.indicators) setIndicators(strategy.indicators as IndicatorConfig[]);
    if (strategy.entry_conditions) setEntryConditions(strategy.entry_conditions as Condition[]);
    if (strategy.exit_conditions) setExitConditions(strategy.exit_conditions as Condition[]);
    if (strategy.symbols) setSelectedSymbols(strategy.symbols as string[]);
    if (strategy.option_config) setOptionConfig(strategy.option_config as OptionConfig);
    setMode("visual");
  };

  const toggleSymbol = (symbol: string) => {
    setSelectedSymbols((prev) =>
      prev.includes(symbol) ? prev.filter((s) => s !== symbol) : [...prev, symbol]
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">AI Strategy Builder</h1>
          <p className="mt-1 text-sm text-slate-400">Build custom strategies visually or with AI</p>
        </div>

        {/* Mode toggle */}
        <div className="flex rounded-lg border border-surface-border">
          <button
            onClick={() => setMode("visual")}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-l-lg transition-colors ${
              mode === "visual"
                ? "bg-accent text-white"
                : "text-slate-400 hover:text-white"
            }`}
          >
            <Code2 className="h-4 w-4" />
            Visual Builder
          </button>
          <button
            onClick={() => setMode("ai")}
            className={`flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-r-lg transition-colors ${
              mode === "ai"
                ? "bg-accent text-white"
                : "text-slate-400 hover:text-white"
            }`}
          >
            <Bot className="h-4 w-4" />
            AI Chat
          </button>
        </div>
      </div>

      {mode === "ai" ? (
        <div className="h-[600px]">
          <AIChat onStrategyGenerated={handleAIGenerated} />
        </div>
      ) : (
        <div className="space-y-6">
          {/* Name & Description */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-300">Strategy Name</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="My Custom Strategy"
                className="w-full rounded-lg border border-surface-border bg-surface-dark px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium text-slate-300">Description</label>
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Brief description of strategy logic"
                className="w-full rounded-lg border border-surface-border bg-surface-dark px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* Left column: Indicators + Symbols */}
            <div className="space-y-6">
              <div className="rounded-xl border border-surface-border bg-surface p-4">
                <IndicatorPicker
                  selectedIndicators={indicators}
                  onAdd={(ind) => setIndicators((prev) => [...prev, ind])}
                  onRemove={(name) => setIndicators((prev) => prev.filter((i) => i.name !== name))}
                />
              </div>

              {/* Target Symbols */}
              <div className="rounded-xl border border-surface-border bg-surface p-4">
                <h3 className="mb-3 text-sm font-semibold text-white">Target Symbols</h3>
                <div className="flex flex-wrap gap-2">
                  {UNDERLYINGS.map((sym) => (
                    <button
                      key={sym}
                      onClick={() => toggleSymbol(sym)}
                      className={`rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors ${
                        selectedSymbols.includes(sym)
                          ? "border-accent/30 bg-accent/5 text-accent-light"
                          : "border-surface-border text-slate-400 hover:text-white"
                      }`}
                    >
                      {selectedSymbols.includes(sym) && <Check className="mr-1 inline h-3 w-3" />}
                      {sym}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Right column: Conditions */}
            <div className="space-y-6">
              <div className="rounded-xl border border-surface-border bg-surface p-4">
                <ConditionBuilder
                  conditions={entryConditions}
                  onChange={setEntryConditions}
                  label="Entry Conditions"
                  availableOperands={allOperands}
                />
              </div>

              <div className="rounded-xl border border-surface-border bg-surface p-4">
                <ConditionBuilder
                  conditions={exitConditions}
                  onChange={setExitConditions}
                  label="Exit Conditions (ANY)"
                  availableOperands={allOperands}
                />
              </div>
            </div>
          </div>

          {/* Option Config */}
          <div className="rounded-xl border border-surface-border bg-surface p-4">
            <h3 className="mb-3 text-sm font-semibold text-white">Option Configuration</h3>
            <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
              <div>
                <label className="mb-1 block text-xs text-slate-400">Action</label>
                <select
                  value={optionConfig.action}
                  onChange={(e) =>
                    setOptionConfig({ ...optionConfig, action: e.target.value as OptionConfig["action"] })
                  }
                  className="w-full rounded-md border border-surface-border bg-surface-dark px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                >
                  <option value="BUY_CALL">BUY CALL</option>
                  <option value="BUY_PUT">BUY PUT</option>
                  <option value="SELL_CALL">SELL CALL</option>
                  <option value="SELL_PUT">SELL PUT</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-400">Strike</label>
                <select
                  value={optionConfig.strike_selection}
                  onChange={(e) =>
                    setOptionConfig({
                      ...optionConfig,
                      strike_selection: e.target.value as OptionConfig["strike_selection"],
                    })
                  }
                  className="w-full rounded-md border border-surface-border bg-surface-dark px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                >
                  <option value="ATM">ATM</option>
                  <option value="ITM_1">ITM 1</option>
                  <option value="ITM_2">ITM 2</option>
                  <option value="OTM_1">OTM 1</option>
                  <option value="OTM_2">OTM 2</option>
                  <option value="OTM_3">OTM 3</option>
                </select>
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-400">Min DTE</label>
                <input
                  type="number"
                  value={optionConfig.min_dte}
                  onChange={(e) =>
                    setOptionConfig({ ...optionConfig, min_dte: Number(e.target.value) })
                  }
                  className="w-full rounded-md border border-surface-border bg-surface-dark px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-400">Max DTE</label>
                <input
                  type="number"
                  value={optionConfig.max_dte}
                  onChange={(e) =>
                    setOptionConfig({ ...optionConfig, max_dte: Number(e.target.value) })
                  }
                  className="w-full rounded-md border border-surface-border bg-surface-dark px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-400">Stop Loss %</label>
                <input
                  type="number"
                  value={optionConfig.stop_loss_pct}
                  onChange={(e) =>
                    setOptionConfig({ ...optionConfig, stop_loss_pct: Number(e.target.value) })
                  }
                  className="w-full rounded-md border border-surface-border bg-surface-dark px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-400">Target %</label>
                <input
                  type="number"
                  value={optionConfig.target_pct}
                  onChange={(e) =>
                    setOptionConfig({ ...optionConfig, target_pct: Number(e.target.value) })
                  }
                  className="w-full rounded-md border border-surface-border bg-surface-dark px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
                />
              </div>
            </div>
          </div>

          {/* Actions */}
          <div className="flex flex-wrap gap-3">
            <button
              onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending || !name}
              className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-accent-light disabled:opacity-50"
            >
              <Save className="h-4 w-4" />
              {saveMutation.isPending ? "Saving..." : "Save Draft"}
            </button>
            <button
              onClick={() => backtestMutation.mutate()}
              disabled={backtestMutation.isPending || indicators.length === 0}
              className="flex items-center gap-2 rounded-lg border border-surface-border bg-surface px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-surface-light disabled:opacity-50"
            >
              <Play className="h-4 w-4" />
              {backtestMutation.isPending ? "Running..." : "Run Backtest"}
            </button>
            <button className="flex items-center gap-2 rounded-lg border border-surface-border bg-surface px-4 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-surface-light">
              <Wand2 className="h-4 w-4" />
              AI Review
            </button>
          </div>

          {/* Backtest results */}
          {backtestMutation.isSuccess && backtestMutation.data && (
            <div className="rounded-xl border border-profit/20 bg-profit/5 p-4">
              <h3 className="mb-2 text-sm font-semibold text-white">Backtest Results</h3>
              <pre className="text-xs text-slate-300">
                {JSON.stringify(backtestMutation.data, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
