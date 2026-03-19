"use client";

import { useState, useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { Strategy, StrategyBiasConfig, StrategyBiasFilter } from "@/types/strategy";
import type { ApiResponse } from "@/types/api";
import { TIER_COLORS, TIER_NAMES } from "@/lib/constants";
import {
  INDICATOR_TYPES, TF_OPTIONS, getDefaultFilterParams, getDefaultBiasConfig,
} from "@/lib/bias-indicators";
import {
  X, ToggleLeft, ToggleRight, Check, Search, Plus, Trash2,
  ChevronDown, ChevronUp, Shield,
} from "lucide-react";

interface SymbolResult {
  symbol: string;
  security_id: string;
}

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
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [showDropdown, setShowDropdown] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);

  // Bias config state
  const [biasMode, setBiasMode] = useState<"bias_filtered" | "independent">(
    strategy.bias_config?.mode ?? "independent"
  );
  const [biasFilters, setBiasFilters] = useState<StrategyBiasFilter[]>(
    strategy.bias_config?.bias_filters ?? []
  );
  const [minAgreement, setMinAgreement] = useState(
    strategy.bias_config?.min_agreement ?? 2
  );
  const [biasOpen, setBiasOpen] = useState(
    (strategy.bias_config?.bias_filters?.length ?? 0) > 0
  );

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (searchRef.current && !searchRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  useEffect(() => {
    const initial: Record<string, number | string> = {};
    for (const p of strategy.params) {
      initial[p.name] = p.current_value as number;
    }
    setParamValues(initial);
  }, [strategy.params]);

  const { data: searchResults, isFetching: isSearching } = useQuery<SymbolResult[]>({
    queryKey: ["symbol-search", debouncedQuery],
    queryFn: async () => {
      if (!debouncedQuery) return [];
      const res = await apiClient.get<ApiResponse<SymbolResult[]>>(
        `/symbols/search?q=${encodeURIComponent(debouncedQuery)}&limit=15`
      );
      return res.data.data;
    },
    enabled: debouncedQuery.length > 0,
    staleTime: 30_000,
  });

  const saveMutation = useMutation({
    mutationFn: async () => {
      const biasConfig: StrategyBiasConfig = {
        bias_filters: biasFilters,
        min_agreement: minAgreement,
        mode: biasMode,
      };
      await apiClient.patch(`/strategies/${strategy.id}`, {
        enabled,
        instruments: selectedInstruments,
        params: paramValues,
        bias_config: biasConfig,
      });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    },
  });

  const addInstrument = (symbol: string) => {
    if (!selectedInstruments.includes(symbol)) {
      setSelectedInstruments((prev) => [...prev, symbol]);
    }
    setSearchQuery("");
    setDebouncedQuery("");
    setShowDropdown(false);
  };

  const removeInstrument = (symbol: string) => {
    setSelectedInstruments((prev) => prev.filter((s) => s !== symbol));
  };

  const handleParamChange = (name: string, value: string) => {
    const num = parseFloat(value);
    if (!isNaN(num)) {
      setParamValues((prev) => ({ ...prev, [name]: num }));
    }
  };

  // Bias filter helpers
  function addBiasFilter() {
    setBiasFilters([...biasFilters, {
      type: "ema_crossover", timeframe: 5,
      params: getDefaultFilterParams("ema_crossover"),
    }]);
    if (biasMode === "independent") setBiasMode("bias_filtered");
  }
  function removeBiasFilter(idx: number) {
    const next = biasFilters.filter((_, i) => i !== idx);
    setBiasFilters(next);
    if (next.length === 0) setBiasMode("independent");
  }
  function updateBiasFilter(idx: number, updates: Partial<StrategyBiasFilter>) {
    const next = [...biasFilters];
    const cur = next[idx];
    if (!cur) return;
    const f = { ...cur, ...updates };
    if (updates.type && updates.type !== cur.type) {
      f.params = getDefaultFilterParams(updates.type);
    }
    next[idx] = f;
    setBiasFilters(next);
  }
  function updateBiasFilterParam(idx: number, key: string, value: number) {
    const next = [...biasFilters];
    const cur = next[idx];
    if (!cur) return;
    next[idx] = { ...cur, params: { ...cur.params, [key]: value } };
    setBiasFilters(next);
  }

  const visibleResults = (searchResults ?? []).filter(
    (r) => !selectedInstruments.includes(r.symbol)
  );

  const biasActive = biasMode === "bias_filtered" && biasFilters.length > 0;
  const biasLabel = biasActive
    ? `${biasFilters.length} filter${biasFilters.length > 1 ? "s" : ""}, min ${minAgreement} agree`
    : "Off";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="relative w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-xl border border-surface-border bg-surface-dark p-6 shadow-2xl">
        {/* Header */}
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-bold text-white">{strategy.display_name}</h2>
              <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                TIER_COLORS[strategy.min_capital_tier] ?? "bg-slate-600 text-slate-200"
              }`}>
                {TIER_NAMES[strategy.min_capital_tier] ?? strategy.min_capital_tier}
              </span>
              {biasActive && (
                <span className="rounded-full bg-accent/20 px-2 py-0.5 text-[10px] font-semibold text-accent-light">
                  BIAS ON
                </span>
              )}
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

        {/* Instrument search */}
        <div className="mt-4">
          <label className="text-sm font-medium text-slate-300">Select Instruments</label>
          <p className="mb-2 text-[11px] text-slate-500">
            Search symbols to add. Strategy runs only on selected instruments.
          </p>
          {selectedInstruments.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {selectedInstruments.map((sym) => (
                <span key={sym}
                  className="flex items-center gap-1 rounded-md border border-accent/40 bg-accent/10 px-2 py-1 text-xs font-medium text-accent-light">
                  {sym}
                  <button onClick={() => removeInstrument(sym)}
                    className="ml-0.5 rounded text-accent-light/60 hover:text-accent-light">
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div ref={searchRef} className="relative">
            <div className="flex items-center gap-2 rounded-md border border-surface-border bg-surface px-3 py-2 focus-within:border-accent">
              <Search className="h-4 w-4 shrink-0 text-slate-500" />
              <input type="text"
                placeholder="Search symbol (e.g. NIFTY, SBIN, RELIANCE…)"
                value={searchQuery}
                onChange={(e) => { setSearchQuery(e.target.value); setShowDropdown(true); }}
                onFocus={() => searchQuery && setShowDropdown(true)}
                className="w-full bg-transparent text-sm text-white placeholder-slate-500 focus:outline-none" />
              {isSearching && (
                <div className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-accent border-t-transparent" />
              )}
            </div>
            {showDropdown && debouncedQuery.length > 0 && (
              <div className="absolute z-10 mt-1 w-full rounded-md border border-surface-border bg-surface-dark shadow-xl">
                {visibleResults.length > 0 ? (
                  <ul className="max-h-48 overflow-y-auto py-1">
                    {visibleResults.map((r) => (
                      <li key={r.symbol}>
                        <button onClick={() => addInstrument(r.symbol)}
                          className="flex w-full items-center gap-3 px-3 py-2 text-sm hover:bg-surface">
                          <span className="font-medium text-white">{r.symbol}</span>
                          <span className="text-xs text-slate-500">{r.security_id}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="px-3 py-3 text-xs text-slate-500">
                    {isSearching ? "Searching…" : "No symbols found"}
                  </p>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Strategy Params */}
        {strategy.params.length > 0 && (
          <div className="mt-4">
            <label className="text-sm font-medium text-slate-300">Strategy Parameters</label>
            <div className="mt-2 grid grid-cols-2 gap-3">
              {strategy.params.map((p) => (
                <div key={p.name}>
                  <label className="block text-[11px] text-slate-500">{p.description}</label>
                  <input type="number"
                    value={paramValues[p.name] ?? (typeof p.default_value === "boolean" ? 0 : p.default_value)}
                    onChange={(e) => handleParamChange(p.name, e.target.value)}
                    min={p.min} max={p.max}
                    step={typeof p.default_value === "number" && p.default_value < 1 ? 0.01 : 1}
                    className="mt-0.5 w-full rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none" />
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Bias Filters ── */}
        <div className="mt-4 border-t border-surface-border pt-4">
          <button onClick={() => setBiasOpen((o) => !o)}
            className="flex w-full items-center gap-2 text-sm font-medium text-white hover:text-accent-light transition-colors text-left">
            <Shield className="h-4 w-4 text-accent-light" />
            {biasOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            Bias Filters
            <span className="ml-auto text-[11px] text-slate-500">({biasLabel})</span>
          </button>

          {biasOpen && (
            <div className="mt-3 space-y-3">
              <p className="text-[11px] text-slate-500">
                Add indicator filters to confirm direction before this strategy fires.
                Signals that don't align with the bias consensus are suppressed — in both backtest and live trading.
              </p>

              {/* Mode toggle */}
              <div className="flex items-center justify-between rounded-lg border border-surface-border bg-surface p-3">
                <div>
                  <span className="text-sm font-medium text-white">Require Bias Alignment</span>
                  <p className="text-[11px] text-slate-500 mt-0.5">
                    {biasMode === "bias_filtered"
                      ? "Strategy only fires when bias indicators agree with signal direction"
                      : "Strategy fires independently — bias filters are ignored"}
                  </p>
                </div>
                <button onClick={() => setBiasMode(biasMode === "bias_filtered" ? "independent" : "bias_filtered")}
                  className={`transition-colors ${biasMode === "bias_filtered" ? "text-profit" : "text-slate-500"}`}>
                  {biasMode === "bias_filtered"
                    ? <ToggleRight className="h-7 w-7" />
                    : <ToggleLeft className="h-7 w-7" />}
                </button>
              </div>

              {biasMode === "bias_filtered" && (
                <>
                  {/* Min agreement */}
                  <div className="flex items-center gap-3">
                    <label className="text-[11px] text-slate-500 uppercase font-medium">Min Agreement</label>
                    <input type="number" value={minAgreement}
                      min={1} max={Math.max(biasFilters.length, 1)}
                      onChange={(e) => setMinAgreement(Number(e.target.value))}
                      className="w-16 rounded-md border border-surface-border bg-surface px-2 py-1 text-xs text-white focus:border-accent focus:outline-none" />
                    <span className="text-[11px] text-slate-500">
                      of {biasFilters.length} filter{biasFilters.length !== 1 ? "s" : ""} must agree
                    </span>
                  </div>

                  {/* Filter list */}
                  {biasFilters.map((filter, idx) => {
                    const typeDef = INDICATOR_TYPES[filter.type];
                    return (
                      <div key={idx} className="rounded-lg border border-surface-border/50 bg-surface/30 p-3">
                        <div className="flex items-start gap-2">
                          <div className="flex-1 grid grid-cols-2 gap-2 sm:grid-cols-4">
                            <div className="space-y-0.5">
                              <label className="text-[10px] text-slate-500 uppercase">Indicator</label>
                              <select value={filter.type}
                                onChange={(e) => updateBiasFilter(idx, { type: e.target.value })}
                                className="w-full rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none">
                                {Object.entries(INDICATOR_TYPES).map(([k, v]) => (
                                  <option key={k} value={k}>{v.label}</option>
                                ))}
                              </select>
                            </div>
                            <div className="space-y-0.5">
                              <label className="text-[10px] text-slate-500 uppercase">Timeframe</label>
                              <select value={filter.timeframe}
                                onChange={(e) => updateBiasFilter(idx, { timeframe: Number(e.target.value) })}
                                className="w-full rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none">
                                {TF_OPTIONS.map((tf) => (
                                  <option key={tf} value={tf}>{tf}m</option>
                                ))}
                              </select>
                            </div>
                            {typeDef?.params.map((p) => (
                              <div key={p.key} className="space-y-0.5">
                                <label className="text-[10px] text-slate-500 uppercase">{p.label}</label>
                                <input type="number"
                                  value={filter.params[p.key] ?? p.default}
                                  min={p.min} max={p.max} step={p.step ?? 1}
                                  onChange={(e) => updateBiasFilterParam(idx, p.key, Number(e.target.value))}
                                  className="w-full rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none" />
                              </div>
                            ))}
                          </div>
                          <button onClick={() => removeBiasFilter(idx)}
                            className="mt-4 rounded-md p-1 text-slate-500 hover:bg-loss/10 hover:text-loss transition-colors">
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </div>
                    );
                  })}

                  <button onClick={addBiasFilter}
                    className="flex items-center gap-1 rounded-lg border border-dashed border-surface-border px-3 py-1.5 text-xs text-slate-400 hover:border-accent-light hover:text-accent-light transition-colors">
                    <Plus className="h-3 w-3" /> Add Indicator Filter
                  </button>
                </>
              )}
            </div>
          )}
        </div>

        {/* Save */}
        <div className="mt-6 flex items-center justify-end gap-3">
          <button onClick={onClose}
            className="rounded-lg border border-surface-border px-4 py-2 text-sm text-slate-400 hover:text-white">
            Cancel
          </button>
          <button
            onClick={() => saveMutation.mutate()}
            disabled={saveMutation.isPending}
            className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white transition-colors ${
              saved ? "bg-profit" : "bg-accent hover:bg-accent-light"
            } disabled:opacity-50`}>
            {saveMutation.isPending ? (
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
            ) : saved ? (
              <><Check className="h-4 w-4" /> Saved</>
            ) : (
              "Save Configuration"
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
