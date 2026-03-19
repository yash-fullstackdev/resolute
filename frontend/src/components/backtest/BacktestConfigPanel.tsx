"use client";

import { useState, useEffect } from "react";
import { Plus, Trash2, ChevronDown, ChevronUp } from "lucide-react";
import type {
  BacktestInstrument,
  BacktestStrategyOption,
  MultiBacktestRequest,
  BiasConfig,
  BiasFilter,
  StrategySlot,
  ExitConfig,
} from "@/types/backtest";
import { apiClient } from "@/lib/api";

interface BacktestConfigPanelProps {
  onRun: (req: MultiBacktestRequest) => void;
  isRunning: boolean;
}

// ── Indicator type definitions ───────────────────────────────────────────────

const INDICATOR_TYPES: Record<string, { label: string; params: { key: string; label: string; default: number; min?: number; max?: number; step?: number }[] }> = {
  ema_crossover: {
    label: "EMA Crossover",
    params: [
      { key: "short", label: "Short Period", default: 9, min: 1, max: 200 },
      { key: "long", label: "Long Period", default: 21, min: 2, max: 500 },
    ],
  },
  supertrend: {
    label: "Supertrend",
    params: [
      { key: "period", label: "Period", default: 10, min: 5, max: 50 },
      { key: "multiplier", label: "Multiplier", default: 3.0, min: 0.5, max: 10, step: 0.1 },
    ],
  },
  rsi_zone: {
    label: "RSI Zone",
    params: [
      { key: "period", label: "Period", default: 14, min: 2, max: 50 },
      { key: "overbought", label: "Overbought", default: 70, min: 50, max: 95 },
      { key: "oversold", label: "Oversold", default: 30, min: 5, max: 50 },
    ],
  },
  ttm_momentum: {
    label: "TTM Squeeze Momentum",
    params: [
      { key: "period", label: "Period", default: 20, min: 5, max: 50 },
    ],
  },
  macd_signal: {
    label: "MACD Signal",
    params: [
      { key: "fast", label: "Fast EMA", default: 12, min: 2, max: 50 },
      { key: "slow", label: "Slow EMA", default: 26, min: 5, max: 100 },
      { key: "signal", label: "Signal", default: 9, min: 2, max: 30 },
    ],
  },
  ema_zone: {
    label: "EMA Zone + RSI",
    params: [
      { key: "ema_period", label: "EMA Period", default: 33, min: 5, max: 200 },
      { key: "rsi_period", label: "RSI Period", default: 14, min: 2, max: 50 },
      { key: "rsi_bull", label: "RSI Bull", default: 60, min: 50, max: 90 },
      { key: "rsi_bear", label: "RSI Bear", default: 40, min: 10, max: 50 },
    ],
  },
  price_vs_ema: {
    label: "Price vs EMA",
    params: [
      { key: "period", label: "EMA Period", default: 20, min: 2, max: 200 },
    ],
  },
  bollinger_squeeze: {
    label: "Bollinger Squeeze",
    params: [
      { key: "period", label: "Period", default: 20, min: 5, max: 50 },
      { key: "std_mult", label: "Std Dev", default: 2.0, min: 0.5, max: 4, step: 0.1 },
    ],
  },
};

const TF_OPTIONS = [1, 2, 3, 5, 10, 15, 30, 60];

const BACKTEST_STRATEGIES = [
  "ttm_squeeze", "supertrend_strategy", "vwap_supertrend",
  "ema_breakdown", "rsi_vwap_scalp", "ema33_ob", "smc_order_block",
];

const DEFAULT_BIAS_FILTERS: BiasFilter[] = [
  { type: "ema_crossover", timeframe: 5, params: { short: 2, long: 11 } },
  { type: "supertrend", timeframe: 5, params: { period: 10, multiplier: 3.0 } },
];

const DEFAULT_EXIT: ExitConfig = {
  sl_atr_mult: 0.5,
  tp_atr_mult: 1.5,
  max_hold_bars: 20,
  slippage_pts: 0.5,
};

// Per-strategy configurable parameters
const STRATEGY_PARAMS: Record<string, { key: string; label: string; default: number; min?: number; max?: number; step?: number }[]> = {
  ttm_squeeze: [
    { key: "bb_period", label: "BB Period", default: 20, min: 5, max: 50 },
    { key: "bb_std", label: "BB Std Dev", default: 2.0, min: 0.5, max: 4, step: 0.1 },
    { key: "kc_period", label: "KC Period", default: 20, min: 5, max: 50 },
    { key: "kc_atr_period", label: "KC ATR", default: 10, min: 5, max: 30 },
    { key: "kc_mult", label: "KC Mult", default: 1.5, min: 0.5, max: 5, step: 0.1 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 50, min: 1, max: 500 },
  ],
  supertrend_strategy: [
    { key: "period", label: "ST Period", default: 10, min: 5, max: 50 },
    { key: "multiplier", label: "ST Mult", default: 3.0, min: 0.5, max: 10, step: 0.1 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 20, min: 1, max: 200 },
  ],
  vwap_supertrend: [
    { key: "st_period", label: "ST Period", default: 10, min: 5, max: 50 },
    { key: "st_multiplier", label: "ST Mult", default: 3.0, min: 0.5, max: 10, step: 0.1 },
    { key: "vwap_proximity_pct", label: "VWAP Prox %", default: 0.0015, min: 0.0005, max: 0.01, step: 0.0005 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 20, min: 1, max: 200 },
  ],
  ema_breakdown: [
    { key: "ema_short", label: "EMA Short", default: 2, min: 1, max: 50 },
    { key: "ema_long", label: "EMA Long", default: 11, min: 2, max: 100 },
    { key: "rsi_period", label: "RSI Period", default: 14, min: 2, max: 50 },
    { key: "breakaway_pct", label: "Breakaway %", default: 0.0008, min: 0.0001, max: 0.005, step: 0.0001 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 20, min: 1, max: 200 },
  ],
  rsi_vwap_scalp: [
    { key: "rsi_period", label: "RSI Period", default: 14, min: 2, max: 50 },
    { key: "rsi_oversold", label: "Oversold", default: 30, min: 5, max: 50 },
    { key: "rsi_overbought", label: "Overbought", default: 70, min: 50, max: 95 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 15, min: 1, max: 200 },
  ],
  ema33_ob: [
    { key: "ema_period", label: "EMA Period", default: 33, min: 5, max: 200 },
    { key: "rsi_period", label: "RSI Period", default: 14, min: 2, max: 50 },
    { key: "rsi_bull_threshold", label: "RSI Bull", default: 60, min: 50, max: 90 },
    { key: "rsi_bear_threshold", label: "RSI Bear", default: 40, min: 10, max: 50 },
    { key: "pullback_atr_mult", label: "Pullback ATR", default: 0.5, min: 0.1, max: 3, step: 0.1 },
    { key: "rejection_body_pct", label: "Reject Body %", default: 0.0004, min: 0.0001, max: 0.005, step: 0.0001 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 20, min: 1, max: 200 },
  ],
  smc_order_block: [
    { key: "ob_length", label: "OB Length", default: 6, min: 3, max: 20 },
    { key: "fvg_threshold", label: "FVG Thresh", default: 0.0005, min: 0.0001, max: 0.005, step: 0.0001 },
    { key: "max_sl_points", label: "Max SL (pts)", default: 20, min: 1, max: 200 },
  ],
};

const DEFAULT_SLOT: StrategySlot = {
  name: "",
  session: "all",
  mode: "independent",
  concurrent: true,
  max_fires_per_day: 5,
  time_stop_bars: 20,
  params: {},
};

// Will be populated from API data
let _dbStrategyParams: Record<string, Record<string, number>> = {};

function getDefaultParams(stratName: string): Record<string, number> {
  const defs = STRATEGY_PARAMS[stratName];
  if (!defs) return {};
  const p: Record<string, number> = {};
  for (const d of defs) p[d.key] = d.default;
  // Override with user's saved settings from DB (fetched via API)
  const dbVals = _dbStrategyParams[stratName];
  if (dbVals) {
    for (const [k, v] of Object.entries(dbVals)) {
      if (k in p) p[k] = v;
    }
  }
  return p;
}

export function BacktestConfigPanel({ onRun, isRunning }: BacktestConfigPanelProps) {
  const [instruments, setInstruments] = useState<BacktestInstrument[]>([]);
  const [strategyOptions, setStrategyOptions] = useState<BacktestStrategyOption[]>([]);
  const [selectedInstrument, setSelectedInstrument] = useState("NIFTY_50");
  const [startDate, setStartDate] = useState("2025-01-01");
  const [endDate, setEndDate] = useState("2025-12-31");

  const [biasFilters, setBiasFilters] = useState<BiasFilter[]>([...DEFAULT_BIAS_FILTERS]);
  const [minAgreement, setMinAgreement] = useState(2);
  const [cooldownBars, setCooldownBars] = useState(10);
  const [strategies, setStrategies] = useState<StrategySlot[]>([{ ...DEFAULT_SLOT }]);
  const [exitConfig, setExitConfig] = useState<ExitConfig>({ ...DEFAULT_EXIT });

  const [biasOpen, setBiasOpen] = useState(true);
  const [exitOpen, setExitOpen] = useState(false);

  useEffect(() => {
    apiClient.get("/backtest/instruments").then((r) => {
      setInstruments(r.data.instruments ?? []);
      if (r.data.instruments?.[0]) {
        const inst = r.data.instruments[0];
        setSelectedInstrument(inst.name);
        const endYear = parseInt(inst.end_date.split("-")[0]);
        setStartDate(`${endYear - 1}-01-01`);
        setEndDate(inst.end_date);
      }
    }).catch(() => {});
    apiClient.get("/strategies").then((r) => {
      const all = r.data.data ?? [];
      // Filter to backtest-eligible strategies
      setStrategyOptions(all.filter(
        (s: BacktestStrategyOption) => BACKTEST_STRATEGIES.includes(s.id ?? s.name)
      ).map((s: Record<string, unknown>) => ({
        name: s.id ?? s.name,
        display_name: s.display_name ?? s.name,
        category: s.category,
        min_capital_tier: s.min_capital_tier,
        complexity: "",
        description: s.description,
      })));
      // Extract saved params (current_value) from DB for each strategy
      const dbParams: Record<string, Record<string, number>> = {};
      for (const s of all) {
        const id = (s as Record<string, unknown>).id as string;
        const params = (s as Record<string, unknown>).params as { name: string; current_value: number }[] | undefined;
        if (id && params) {
          const vals: Record<string, number> = {};
          for (const p of params) {
            if (typeof p.current_value === "number") vals[p.name] = p.current_value;
          }
          if (Object.keys(vals).length > 0) dbParams[id] = vals;
        }
      }
      _dbStrategyParams = dbParams;
    }).catch(() => {});
  }, []);

  // ── Bias filter helpers ──────────────────────────────────────────────
  function addBiasFilter() {
    setBiasFilters([...biasFilters, { type: "ema_crossover", timeframe: 5, params: { short: 9, long: 21 } }]);
  }
  function removeBiasFilter(idx: number) {
    setBiasFilters(biasFilters.filter((_, i) => i !== idx));
  }
  function updateBiasFilter(idx: number, updates: Partial<BiasFilter>) {
    const next = [...biasFilters];
    const f = { ...next[idx], ...updates };
    // When type changes, reset params to defaults
    if (updates.type && updates.type !== next[idx]?.type) {
      const def = INDICATOR_TYPES[updates.type];
      if (def) {
        const newParams: Record<string, number> = {};
        for (const p of def.params) newParams[p.key] = p.default;
        f.params = newParams;
      }
    }
    next[idx] = f as BiasFilter;
    setBiasFilters(next);
  }
  function updateBiasFilterParam(idx: number, key: string, value: number) {
    const next = [...biasFilters];
    const cur = next[idx];
    if (!cur) return;
    next[idx] = { ...cur, params: { ...cur.params, [key]: value } };
    setBiasFilters(next);
  }

  // ── Strategy helpers ─────────────────────────────────────────────────
  function addStrategy() {
    setStrategies([...strategies, { ...DEFAULT_SLOT }]);
  }
  function removeStrategy(idx: number) {
    setStrategies(strategies.filter((_, i) => i !== idx));
  }
  function updateSlot(idx: number, field: keyof StrategySlot, value: string | number | boolean) {
    const next = [...strategies];
    const updated = { ...next[idx], [field]: value } as StrategySlot;
    // Reset params to defaults when strategy changes
    if (field === "name" && typeof value === "string") {
      updated.params = getDefaultParams(value);
    }
    next[idx] = updated;
    setStrategies(next);
  }
  function updateSlotParam(idx: number, key: string, value: number) {
    const next = [...strategies];
    const cur = next[idx];
    if (!cur) return;
    next[idx] = { ...cur, params: { ...cur.params, [key]: value } };
    setStrategies(next);
  }

  function handleRun() {
    const biasConfig: BiasConfig = {
      bias_filters: biasFilters,
      min_agreement: minAgreement,
      cooldown_bars: cooldownBars,
    };
    const req: MultiBacktestRequest = {
      instrument: selectedInstrument,
      start_date: startDate,
      end_date: endDate,
      bias_config: biasConfig,
      strategies,
      exit_config: exitConfig,
    };
    onRun(req);
  }

  const biasLabel = biasFilters.length > 0
    ? `${biasFilters.map(f => `${INDICATOR_TYPES[f.type]?.label ?? f.type}@${f.timeframe}m`).join(" + ")} — min ${minAgreement} agree`
    : "No filters";

  return (
    <div className="rounded-2xl border border-surface-border bg-surface-dark p-5 space-y-5">
      <h2 className="text-base font-semibold text-white">Multi-Strategy Backtest</h2>

      {/* ── Instrument + Dates ── */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <Field label="Instrument">
          <select value={selectedInstrument} onChange={(e) => setSelectedInstrument(e.target.value)} className={SEL}>
            {instruments.map((i) => (
              <option key={i.name} value={i.name}>{i.display_name} ({i.start_date} → {i.end_date})</option>
            ))}
          </select>
        </Field>
        <Field label="Start Date">
          <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className={INP} />
        </Field>
        <Field label="End Date">
          <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} className={INP} />
        </Field>
      </div>

      {/* ── Dynamic Bias Engine ── */}
      <div className="border-t border-surface-border pt-3">
        <button onClick={() => setBiasOpen((o) => !o)}
          className="flex items-center gap-2 text-sm font-medium text-white hover:text-accent-light transition-colors w-full text-left">
          {biasOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          Bias Engine
          <span className="text-xs text-slate-500 ml-2 truncate max-w-[400px]">({biasLabel})</span>
        </button>
        {biasOpen && (
          <div className="mt-3 space-y-3">
            {/* Consensus settings */}
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Field label="Min Agreement">
                <input type="number" value={minAgreement} min={1} max={biasFilters.length || 1}
                  onChange={(e) => setMinAgreement(Number(e.target.value))} className={INP} />
              </Field>
              <Field label="Cooldown (5m bars)">
                <input type="number" value={cooldownBars} min={0} max={50}
                  onChange={(e) => setCooldownBars(Number(e.target.value))} className={INP} />
              </Field>
            </div>

            {/* Filter list */}
            <div className="space-y-2">
              {biasFilters.map((filter, idx) => {
                const typeDef = INDICATOR_TYPES[filter.type];
                return (
                  <div key={idx} className="rounded-lg border border-surface-border/50 bg-surface-light/20 p-3">
                    <div className="flex items-start gap-2">
                      <div className="flex-1 grid grid-cols-2 gap-2 sm:grid-cols-[2fr_1fr_repeat(4,1fr)]">
                        {/* Indicator type */}
                        <Field label="Indicator">
                          <select value={filter.type}
                            onChange={(e) => updateBiasFilter(idx, { type: e.target.value })}
                            className={SEL}>
                            {Object.entries(INDICATOR_TYPES).map(([k, v]) => (
                              <option key={k} value={k}>{v.label}</option>
                            ))}
                          </select>
                        </Field>
                        {/* Timeframe */}
                        <Field label="TF (min)">
                          <select value={filter.timeframe}
                            onChange={(e) => updateBiasFilter(idx, { timeframe: Number(e.target.value) })}
                            className={SEL}>
                            {TF_OPTIONS.map((tf) => (
                              <option key={tf} value={tf}>{tf}m</option>
                            ))}
                          </select>
                        </Field>
                        {/* Dynamic params */}
                        {typeDef?.params.map((p) => (
                          <Field key={p.key} label={p.label}>
                            <input type="number"
                              value={filter.params[p.key] ?? p.default}
                              min={p.min} max={p.max} step={p.step ?? 1}
                              onChange={(e) => updateBiasFilterParam(idx, p.key, Number(e.target.value))}
                              className={INP} />
                          </Field>
                        ))}
                      </div>
                      <button onClick={() => removeBiasFilter(idx)}
                        className="mt-5 rounded-lg p-1.5 text-slate-500 hover:bg-loss/10 hover:text-loss transition-colors">
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
            </div>
          </div>
        )}
      </div>

      {/* ── Strategy Slots ── */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-white">Strategy Slots</h3>
          <button onClick={addStrategy}
            className="flex items-center gap-1 rounded-lg border border-surface-border px-2.5 py-1 text-xs text-slate-300 hover:bg-surface-light hover:text-white transition-colors">
            <Plus className="h-3 w-3" /> Add Strategy
          </button>
        </div>

        {strategies.map((slot, idx) => (
          <div key={idx} className="rounded-xl border border-surface-border/60 bg-surface-light/30 p-4 space-y-3">
            <div className="flex items-start justify-between gap-2">
              <div className="flex-1 grid grid-cols-2 gap-3 sm:grid-cols-6">
                <div className="col-span-2 space-y-1">
                  <label className="text-[10px] font-medium text-slate-500 uppercase">Strategy</label>
                  <select value={slot.name} onChange={(e) => updateSlot(idx, "name", e.target.value)} className={SEL}>
                    <option value="">Select strategy...</option>
                    {strategyOptions.map((s) => (
                      <option key={s.name} value={s.name}>{s.display_name}</option>
                    ))}
                  </select>
                </div>
                <Field label="Session">
                  <select value={slot.session} onChange={(e) => updateSlot(idx, "session", e.target.value)} className={SEL}>
                    <option value="all">All Day</option>
                    <option value="morning">Morning (9:20-11:30)</option>
                    <option value="afternoon">Afternoon (13:00-14:30)</option>
                  </select>
                </Field>
                <Field label="Bias Mode">
                  <select value={slot.mode} onChange={(e) => updateSlot(idx, "mode", e.target.value)} className={SEL}>
                    <option value="independent">Independent</option>
                    <option value="bias_filtered">Bias Filtered</option>
                  </select>
                </Field>
                <Field label="Max Fires/Day">
                  <input type="number" value={slot.max_fires_per_day} min={1} max={20}
                    onChange={(e) => updateSlot(idx, "max_fires_per_day", Number(e.target.value))} className={INP} />
                </Field>
                <Field label="Time Stop">
                  <input type="number" value={slot.time_stop_bars} min={1} max={375}
                    onChange={(e) => updateSlot(idx, "time_stop_bars", Number(e.target.value))} className={INP} />
                </Field>
              </div>
              {strategies.length > 1 && (
                <button onClick={() => removeStrategy(idx)}
                  className="mt-5 rounded-lg p-1.5 text-slate-500 hover:bg-loss/10 hover:text-loss transition-colors">
                  <Trash2 className="h-4 w-4" />
                </button>
              )}
            </div>
            <Toggle label="Concurrent (can hold multiple open trades)" checked={slot.concurrent}
              onChange={(v) => updateSlot(idx, "concurrent", v)} />
            {/* Per-strategy parameters */}
            {slot.name && STRATEGY_PARAMS[slot.name] && (
              <div className="border-t border-surface-border/40 pt-2 mt-2">
                <p className="text-[10px] font-medium text-slate-500 uppercase mb-2">Strategy Parameters</p>
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-7">
                  {STRATEGY_PARAMS[slot.name]?.map((p) => (
                    <Field key={p.key} label={p.label}>
                      <input type="number"
                        value={slot.params[p.key] ?? p.default}
                        min={p.min} max={p.max} step={p.step ?? 1}
                        onChange={(e) => updateSlotParam(idx, p.key, Number(e.target.value))}
                        className={INP} />
                    </Field>
                  ))}
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* ── Exit Config ── */}
      <div className="border-t border-surface-border pt-3">
        <button onClick={() => setExitOpen((o) => !o)}
          className="flex items-center gap-2 text-sm font-medium text-white hover:text-accent-light transition-colors">
          {exitOpen ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          Exit Config
          <span className="text-xs text-slate-500 ml-2">
            (SL={exitConfig.sl_atr_mult}x ATR, TP={exitConfig.tp_atr_mult}x ATR, Hold={exitConfig.max_hold_bars}m)
          </span>
        </button>
        {exitOpen && (
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Field label="SL x ATR">
              <input type="number" value={exitConfig.sl_atr_mult} min={0.1} max={5} step={0.1}
                onChange={(e) => setExitConfig({ ...exitConfig, sl_atr_mult: Number(e.target.value) })} className={INP} />
            </Field>
            <Field label="TP x ATR">
              <input type="number" value={exitConfig.tp_atr_mult} min={0.5} max={10} step={0.1}
                onChange={(e) => setExitConfig({ ...exitConfig, tp_atr_mult: Number(e.target.value) })} className={INP} />
            </Field>
            <Field label="Max Hold (bars)">
              <input type="number" value={exitConfig.max_hold_bars} min={1} max={375}
                onChange={(e) => setExitConfig({ ...exitConfig, max_hold_bars: Number(e.target.value) })} className={INP} />
            </Field>
            <Field label="Slippage (pts)">
              <input type="number" value={exitConfig.slippage_pts} min={0} max={5} step={0.1}
                onChange={(e) => setExitConfig({ ...exitConfig, slippage_pts: Number(e.target.value) })} className={INP} />
            </Field>
          </div>
        )}
      </div>

      {/* ── Run Button ── */}
      <div className="flex items-center justify-end pt-1">
        <button onClick={handleRun}
          disabled={isRunning || strategies.some((s) => !s.name)}
          className="rounded-xl bg-accent px-6 py-2.5 text-sm font-semibold text-white hover:bg-accent-light transition-colors disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-2">
          {isRunning ? (
            <>
              <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
              Running...
            </>
          ) : (
            "Run Backtest"
          )}
        </button>
      </div>
    </div>
  );
}

// ── Tiny helpers ──────────────────────────────────────────────────────────────

const INP = "w-full rounded-lg border border-surface-border bg-surface-light px-2.5 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-accent-light";
const SEL = INP;

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-[10px] font-medium text-slate-500 uppercase">{label}</label>
      {children}
    </div>
  );
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer text-xs text-slate-300 select-none">
      <div className={`w-8 h-4 rounded-full transition-colors relative ${checked ? "bg-accent" : "bg-surface-border"}`}
        onClick={() => onChange(!checked)}>
        <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-transform ${checked ? "translate-x-4" : "translate-x-0.5"}`} />
      </div>
      {label}
    </label>
  );
}
