"use client";

import { useState, useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { Strategy, StrategyInstance, StrategyBiasConfig, StrategyBiasFilter } from "@/types/strategy";
import type { ApiResponse } from "@/types/api";
import { TIER_COLORS, TIER_NAMES } from "@/lib/constants";
import {
  INDICATOR_TYPES, TF_OPTIONS, getDefaultFilterParams,
} from "@/lib/bias-indicators";
import {
  X, Check, Search, Plus, Trash2,
  ChevronDown, ChevronUp, Shield, Zap, FileText, Power,
} from "lucide-react";

interface SymbolResult { symbol: string; security_id: string; }

interface StrategyConfigModalProps {
  strategy: Strategy;
  instance?: StrategyInstance;  // undefined = create new
  onClose: () => void;
}

const SESSION_OPTIONS = [
  { value: "all", label: "All Day" },
  { value: "morning", label: "Morning (9:20–11:30)" },
  { value: "afternoon", label: "Afternoon (13:00–14:30)" },
];

const MODE_OPTIONS = [
  { value: "disabled", label: "Disabled", icon: Power, color: "text-slate-500" },
  { value: "paper", label: "Paper Trading", icon: FileText, color: "text-amber-400" },
  { value: "live", label: "Live Trading", icon: Zap, color: "text-profit" },
];

export function StrategyConfigModal({ strategy, instance, onClose }: StrategyConfigModalProps) {
  const queryClient = useQueryClient();
  const isNew = !instance;

  const [instanceName, setInstanceName] = useState(
    instance?.instance_name ?? `${strategy.display_name} — New`
  );
  const [mode, setMode] = useState<"live" | "paper" | "disabled">(instance?.mode ?? "paper");
  const [session, setSession] = useState(instance?.session ?? "all");
  const [maxDailyLoss, setMaxDailyLoss] = useState<number | "">(instance?.max_daily_loss_pts ?? "");
  const [slAtr, setSlAtr] = useState(instance?.exit_config?.sl_atr_mult ?? 0.5);
  const [tpAtr, setTpAtr] = useState(instance?.exit_config?.tp_atr_mult ?? 1.5);
  const [maxHold, setMaxHold] = useState(instance?.exit_config?.max_hold_bars ?? 20);
  const [slippage, setSlippage] = useState(instance?.exit_config?.slippage_pts ?? 0.5);
  const [selectedInstruments, setSelectedInstruments] = useState<string[]>(
    instance?.instruments ?? []
  );
  const [paramValues, setParamValues] = useState<Record<string, number | string>>({});
  const [saved, setSaved] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [showDropdown, setShowDropdown] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);

  // Bias config state
  const [biasMode, setBiasMode] = useState<"bias_filtered" | "independent">(
    instance?.bias_config?.mode ?? "independent"
  );
  const [biasFilters, setBiasFilters] = useState<StrategyBiasFilter[]>(
    instance?.bias_config?.bias_filters ?? []
  );
  const [minAgreement, setMinAgreement] = useState(
    instance?.bias_config?.min_agreement ?? 2
  );
  const [biasOpen, setBiasOpen] = useState(
    (instance?.bias_config?.bias_filters?.length ?? 0) > 0
  );

  useEffect(() => {
    const t = setTimeout(() => setDebouncedQuery(searchQuery.trim()), 300);
    return () => clearTimeout(t);
  }, [searchQuery]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (searchRef.current && !searchRef.current.contains(e.target as Node)) setShowDropdown(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Initialize params from strategy defaults + instance overrides
  useEffect(() => {
    const initial: Record<string, number | string> = {};
    for (const p of strategy.params) {
      initial[p.name] = p.current_value as number;
    }
    // Override with instance-specific params
    if (instance?.params) {
      for (const [k, v] of Object.entries(instance.params)) {
        if (k in initial) initial[k] = v;
      }
    }
    setParamValues(initial);
  }, [strategy.params, instance]);

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
      const exitConfig = { sl_atr_mult: slAtr, tp_atr_mult: tpAtr, max_hold_bars: maxHold, slippage_pts: slippage };

      if (isNew) {
        await apiClient.post("/strategies/instances", {
          strategy_name: strategy.id,
          instance_name: instanceName,
          instruments: selectedInstruments,
          params: { ...paramValues, exit_config: exitConfig },
          bias_config: biasConfig,
          session,
          mode,
          max_daily_loss_pts: maxDailyLoss || null,
        });
      } else {
        await apiClient.patch(`/strategies/instances/${instance.instance_id}`, {
          instance_name: instanceName,
          enabled: mode !== "disabled",
          instruments: selectedInstruments,
          params: { ...paramValues, exit_config: exitConfig },
          bias_config: biasConfig,
          session,
          mode,
          max_daily_loss_pts: maxDailyLoss || null,
        });
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["strategies"] });
      setSaved(true);
      setTimeout(() => { setSaved(false); onClose(); }, 800);
    },
  });

  // Instrument helpers
  const addInstrument = (symbol: string) => {
    if (!selectedInstruments.includes(symbol)) setSelectedInstruments((p) => [...p, symbol]);
    setSearchQuery(""); setDebouncedQuery(""); setShowDropdown(false);
  };
  const removeInstrument = (symbol: string) => {
    setSelectedInstruments((p) => p.filter((s) => s !== symbol));
  };

  // Bias filter helpers
  function addBiasFilter() {
    setBiasFilters([...biasFilters, { type: "ema_crossover", timeframe: 5, params: getDefaultFilterParams("ema_crossover") }]);
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
    if (updates.type && updates.type !== cur.type) f.params = getDefaultFilterParams(updates.type);
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

  const visibleResults = (searchResults ?? []).filter((r) => !selectedInstruments.includes(r.symbol));

  const INP = "w-full rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="relative w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-xl border border-surface-border bg-surface-dark p-6 shadow-2xl">
        {/* Header */}
        <div className="flex items-start justify-between mb-5">
          <div>
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-bold text-white">
                {isNew ? `New ${strategy.display_name} Instance` : "Edit Instance"}
              </h2>
              <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                TIER_COLORS[strategy.min_capital_tier] ?? "bg-slate-600 text-slate-200"
              }`}>
                {TIER_NAMES[strategy.min_capital_tier] ?? strategy.min_capital_tier}
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-500">{strategy.description}</p>
          </div>
          <button onClick={onClose} className="rounded-md p-1 text-slate-400 hover:text-white">
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="space-y-4">
          {/* Instance Name + Mode */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Instance Name</label>
              <input type="text" value={instanceName}
                onChange={(e) => setInstanceName(e.target.value)}
                placeholder="e.g. TTM Nifty Aggressive"
                className={INP} />
            </div>
            <div>
              <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Trading Mode</label>
              <div className="flex gap-1">
                {MODE_OPTIONS.map((m) => {
                  const Icon = m.icon;
                  const active = mode === m.value;
                  return (
                    <button key={m.value}
                      onClick={() => setMode(m.value as typeof mode)}
                      className={`flex-1 flex items-center justify-center gap-1 rounded-md py-1.5 text-[11px] font-bold transition-all ${
                        active ? `${m.color} bg-surface ring-1 ring-current` : "text-slate-600 hover:text-slate-400"
                      }`}>
                      <Icon className="h-3 w-3" />
                      {m.label.split(" ")[0]}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Session + Daily Loss */}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Session</label>
              <select value={session} onChange={(e) => setSession(e.target.value as typeof session)} className={INP}>
                {SESSION_OPTIONS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Max Daily Loss (pts)</label>
              <input type="number" value={maxDailyLoss}
                onChange={(e) => setMaxDailyLoss(e.target.value ? Number(e.target.value) : "")}
                placeholder="No limit" min={1} max={500}
                className={INP} />
            </div>
          </div>

          {/* Exit Config */}
          <div>
            <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Exit Rules</label>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <div>
                <label className="block text-[10px] text-slate-600">SL x ATR</label>
                <input type="number" value={slAtr} min={0.1} max={5} step={0.1}
                  onChange={(e) => setSlAtr(Number(e.target.value))} className={INP} />
              </div>
              <div>
                <label className="block text-[10px] text-slate-600">TP x ATR</label>
                <input type="number" value={tpAtr} min={0.5} max={10} step={0.1}
                  onChange={(e) => setTpAtr(Number(e.target.value))} className={INP} />
              </div>
              <div>
                <label className="block text-[10px] text-slate-600">Max Hold (bars)</label>
                <input type="number" value={maxHold} min={1} max={375}
                  onChange={(e) => setMaxHold(Number(e.target.value))} className={INP} />
              </div>
              <div>
                <label className="block text-[10px] text-slate-600">Slippage (pts)</label>
                <input type="number" value={slippage} min={0} max={5} step={0.1}
                  onChange={(e) => setSlippage(Number(e.target.value))} className={INP} />
              </div>
            </div>
          </div>

          {/* Instruments */}
          <div>
            <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Instruments</label>
            {selectedInstruments.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1">
                {selectedInstruments.map((sym) => (
                  <span key={sym} className="flex items-center gap-1 rounded-md border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] font-medium text-accent-light">
                    {sym}
                    <button onClick={() => removeInstrument(sym)} className="text-accent-light/60 hover:text-accent-light"><X className="h-2.5 w-2.5" /></button>
                  </span>
                ))}
              </div>
            )}
            <div ref={searchRef} className="relative">
              <div className="flex items-center gap-2 rounded-md border border-surface-border bg-surface px-3 py-1.5 focus-within:border-accent">
                <Search className="h-3.5 w-3.5 shrink-0 text-slate-500" />
                <input type="text" placeholder="Search symbol…" value={searchQuery}
                  onChange={(e) => { setSearchQuery(e.target.value); setShowDropdown(true); }}
                  onFocus={() => searchQuery && setShowDropdown(true)}
                  className="w-full bg-transparent text-xs text-white placeholder-slate-500 focus:outline-none" />
                {isSearching && <div className="h-3 w-3 shrink-0 animate-spin rounded-full border-2 border-accent border-t-transparent" />}
              </div>
              {showDropdown && debouncedQuery.length > 0 && (
                <div className="absolute z-10 mt-1 w-full rounded-md border border-surface-border bg-surface-dark shadow-xl">
                  {visibleResults.length > 0 ? (
                    <ul className="max-h-36 overflow-y-auto py-1">
                      {visibleResults.map((r) => (
                        <li key={r.symbol}>
                          <button onClick={() => addInstrument(r.symbol)}
                            className="flex w-full items-center gap-3 px-3 py-1.5 text-xs hover:bg-surface">
                            <span className="font-medium text-white">{r.symbol}</span>
                            <span className="text-[10px] text-slate-500">{r.security_id}</span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="px-3 py-2 text-[11px] text-slate-500">{isSearching ? "Searching…" : "No symbols found"}</p>
                  )}
                </div>
              )}
            </div>
          </div>

          {/* Strategy Parameters */}
          {strategy.params.length > 0 && (
            <div>
              <label className="block text-[11px] text-slate-500 font-medium uppercase mb-1">Strategy Parameters</label>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {strategy.params.map((p) => (
                  <div key={p.name}>
                    <label className="block text-[10px] text-slate-600">{p.description}</label>
                    <input type="number"
                      value={paramValues[p.name] ?? (typeof p.default_value === "boolean" ? 0 : p.default_value)}
                      onChange={(e) => { const n = parseFloat(e.target.value); if (!isNaN(n)) setParamValues((prev) => ({ ...prev, [p.name]: n })); }}
                      min={p.min} max={p.max}
                      step={typeof p.default_value === "number" && p.default_value < 1 ? 0.01 : 1}
                      className={INP} />
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Bias Filters */}
          <div className="border-t border-surface-border pt-3">
            <button onClick={() => setBiasOpen((o) => !o)}
              className="flex w-full items-center gap-2 text-[11px] font-medium text-white hover:text-accent-light transition-colors text-left uppercase">
              <Shield className="h-3.5 w-3.5 text-accent-light" />
              {biasOpen ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
              Bias Filters
              <span className="ml-auto text-slate-500 normal-case font-normal">
                {biasMode === "bias_filtered" && biasFilters.length > 0
                  ? `${biasFilters.length} filter${biasFilters.length > 1 ? "s" : ""}, min ${minAgreement} agree`
                  : "Off"}
              </span>
            </button>

            {biasOpen && (
              <div className="mt-2 space-y-2">
                <div className="flex items-center gap-3 text-xs">
                  <label className="text-slate-500">Mode:</label>
                  <button onClick={() => setBiasMode("independent")}
                    className={`rounded px-2 py-1 text-[11px] font-medium ${biasMode === "independent" ? "bg-surface-light text-white" : "text-slate-600"}`}>
                    Independent
                  </button>
                  <button onClick={() => setBiasMode("bias_filtered")}
                    className={`rounded px-2 py-1 text-[11px] font-medium ${biasMode === "bias_filtered" ? "bg-accent/20 text-accent-light" : "text-slate-600"}`}>
                    Bias Filtered
                  </button>
                  {biasMode === "bias_filtered" && (
                    <div className="flex items-center gap-1 ml-auto">
                      <label className="text-[10px] text-slate-500">Min agree:</label>
                      <input type="number" value={minAgreement} min={1} max={Math.max(biasFilters.length, 1)}
                        onChange={(e) => setMinAgreement(Number(e.target.value))}
                        className="w-12 rounded border border-surface-border bg-surface px-1.5 py-0.5 text-[11px] text-white focus:border-accent focus:outline-none" />
                    </div>
                  )}
                </div>

                {biasMode === "bias_filtered" && (
                  <>
                    {biasFilters.map((filter, idx) => {
                      const typeDef = INDICATOR_TYPES[filter.type];
                      return (
                        <div key={idx} className="rounded-lg border border-surface-border/40 bg-surface/20 p-2">
                          <div className="flex items-start gap-1">
                            <div className="flex-1 grid grid-cols-2 gap-1.5 sm:grid-cols-4">
                              <div>
                                <label className="text-[9px] text-slate-600 uppercase">Indicator</label>
                                <select value={filter.type}
                                  onChange={(e) => updateBiasFilter(idx, { type: e.target.value })}
                                  className={INP}>
                                  {Object.entries(INDICATOR_TYPES).map(([k, v]) => (
                                    <option key={k} value={k}>{v.label}</option>
                                  ))}
                                </select>
                              </div>
                              <div>
                                <label className="text-[9px] text-slate-600 uppercase">TF</label>
                                <select value={filter.timeframe}
                                  onChange={(e) => updateBiasFilter(idx, { timeframe: Number(e.target.value) })}
                                  className={INP}>
                                  {TF_OPTIONS.map((tf) => <option key={tf} value={tf}>{tf}m</option>)}
                                </select>
                              </div>
                              {typeDef?.params.map((p) => (
                                <div key={p.key}>
                                  <label className="text-[9px] text-slate-600 uppercase">{p.label}</label>
                                  <input type="number" value={filter.params[p.key] ?? p.default}
                                    min={p.min} max={p.max} step={p.step ?? 1}
                                    onChange={(e) => updateBiasFilterParam(idx, p.key, Number(e.target.value))}
                                    className={INP} />
                                </div>
                              ))}
                            </div>
                            <button onClick={() => removeBiasFilter(idx)}
                              className="mt-3 rounded p-1 text-slate-600 hover:bg-loss/10 hover:text-loss transition-colors">
                              <Trash2 className="h-3 w-3" />
                            </button>
                          </div>
                        </div>
                      );
                    })}
                    <button onClick={addBiasFilter}
                      className="flex items-center gap-1 rounded border border-dashed border-surface-border px-2 py-1 text-[10px] text-slate-500 hover:border-accent-light hover:text-accent-light transition-colors">
                      <Plus className="h-2.5 w-2.5" /> Add Filter
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="mt-5 flex items-center justify-between border-t border-surface-border pt-4">
          {mode === "live" && (
            <p className="text-[10px] text-loss font-medium">
              Live mode places real orders with real money
            </p>
          )}
          {mode !== "live" && <div />}
          <div className="flex items-center gap-2">
            <button onClick={onClose}
              className="rounded-lg border border-surface-border px-4 py-2 text-xs text-slate-400 hover:text-white">
              Cancel
            </button>
            <button onClick={() => saveMutation.mutate()}
              disabled={saveMutation.isPending || !instanceName.trim()}
              className={`flex items-center gap-2 rounded-lg px-4 py-2 text-xs font-semibold text-white transition-colors ${
                saved ? "bg-profit" : "bg-accent hover:bg-accent-light"
              } disabled:opacity-50`}>
              {saveMutation.isPending ? (
                <div className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" />
              ) : saved ? (
                <><Check className="h-3.5 w-3.5" /> Saved</>
              ) : isNew ? (
                "Create Instance"
              ) : (
                "Save Changes"
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
