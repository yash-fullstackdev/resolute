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
  strategy_instance: {
    label: "Strategy Instance",
    params: [], // no numeric params — uses strategy_name dropdown
  },
};

const TF_OPTIONS = [1, 2, 3, 5, 10, 15, 30, 60];

const BACKTEST_STRATEGIES = [
  "ttm_squeeze", "supertrend_strategy", "vwap_supertrend",
  "ema_breakdown", "rsi_vwap_scalp", "ema33_ob", "smc_order_block",
  "brahmaastra", "ema5_mean_reversion", "parent_child_momentum",
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
  brahmaastra: [
    { key: "gap_threshold_pct", label: "Gap %", default: 0.4, min: 0.1, max: 2.0, step: 0.1 },
    { key: "wick_ratio_min", label: "Wick Ratio", default: 1.5, min: 0.5, max: 5.0, step: 0.1 },
    { key: "partial_book_rr", label: "Partial RR", default: 1.0, min: 0.5, max: 2.0, step: 0.1 },
    { key: "final_target_rr", label: "Final RR", default: 1.5, min: 1.0, max: 5.0, step: 0.1 },
  ],
  ema5_mean_reversion: [
    { key: "ema_period", label: "EMA Period", default: 5, min: 3, max: 20 },
    { key: "min_distance_ema_pct", label: "Min Dist %", default: 0.002, min: 0.0005, max: 0.01, step: 0.0005 },
    { key: "rr_min", label: "Min RR", default: 3.0, min: 1.5, max: 5.0, step: 0.5 },
    { key: "daily_loss_limit", label: "Daily Loss Limit", default: 3, min: 1, max: 5 },
  ],
  parent_child_momentum: [
    { key: "ema_short", label: "EMA Short (1H)", default: 10, min: 3, max: 30 },
    { key: "ema_mid", label: "EMA Mid (1H)", default: 30, min: 10, max: 60 },
    { key: "ema_long", label: "EMA Long (1H)", default: 100, min: 50, max: 200 },
    { key: "macd_fast", label: "MACD Fast", default: 48, min: 12, max: 100 },
    { key: "macd_slow", label: "MACD Slow", default: 104, min: 26, max: 200 },
    { key: "macd_signal", label: "MACD Signal", default: 36, min: 9, max: 72 },
    { key: "profit_target_pct", label: "Profit Target %", default: 25, min: 10, max: 100 },
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

// Populated from API — strategy params + bias configs from DB
let _dbStrategyParams: Record<string, Record<string, number>> = {};
let _dbStrategyBias: Record<string, BiasConfig> = {};

function getDefaultParams(stratName: string): Record<string, number> {
  const defs = STRATEGY_PARAMS[stratName];
  if (!defs) return {};
  const p: Record<string, number> = {};
  for (const d of defs) p[d.key] = d.default;
  const dbVals = _dbStrategyParams[stratName];
  if (dbVals) {
    for (const [k, v] of Object.entries(dbVals)) {
      if (k in p) p[k] = v;
    }
  }
  return p;
}

function getDefaultBiasForStrategy(stratName: string): BiasConfig | undefined {
  return _dbStrategyBias[stratName];
}

export function BacktestConfigPanel({ onRun, isRunning }: BacktestConfigPanelProps) {
  const [instruments, setInstruments] = useState<BacktestInstrument[]>([]);
  const [strategyOptions, setStrategyOptions] = useState<BacktestStrategyOption[]>([]);
  const [availableInstances, setAvailableInstances] = useState<Record<string, unknown>[]>([]);
  const [selectedInstrument, setSelectedInstrument] = useState("NIFTY_50");
  const [startDate, setStartDate] = useState("2025-01-01");
  const [endDate, setEndDate] = useState("2025-12-31");

  const [strategies, setStrategies] = useState<StrategySlot[]>([{ ...DEFAULT_SLOT }]);


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
      // Extract saved params + bias + exit configs + instances from DB
      const dbParams: Record<string, Record<string, number>> = {};
      const dbBias: Record<string, BiasConfig> = {};
      const allInstances: Record<string, unknown>[] = [];
      for (const s of all) {
        const rec = s as Record<string, unknown>;
        const id = rec.id as string;
        const params = rec.params as { name: string; current_value: number }[] | undefined;
        if (id && params) {
          const vals: Record<string, number> = {};
          for (const p of params) {
            if (typeof p.current_value === "number") vals[p.name] = p.current_value;
          }
          if (Object.keys(vals).length > 0) dbParams[id] = vals;
        }
        const bc = rec.bias_config as BiasConfig | undefined;
        if (id && bc && bc.bias_filters) {
          dbBias[id] = bc;
        }
        // Collect instances
        const instances = (rec.instances ?? []) as Record<string, unknown>[];
        for (const inst of instances) {
          allInstances.push({ ...inst, strategy_id: id, strategy_display: rec.display_name });
        }
      }
      _dbStrategyParams = dbParams;
      _dbStrategyBias = dbBias;
      setAvailableInstances(allInstances);
    }).catch(() => {});
  }, []);

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
    if (field === "name" && typeof value === "string") {
      updated.params = getDefaultParams(value);
      const dbBias = getDefaultBiasForStrategy(value);
      if (dbBias) {
        updated.bias_config = dbBias;
        updated.mode = dbBias.mode ?? "bias_filtered";
      } else {
        updated.bias_config = undefined;
        updated.mode = "independent";
      }
    }
    next[idx] = updated;
    setStrategies(next);
  }

  function loadInstance(idx: number, instanceId: string) {
    const inst = availableInstances.find((i) => (i as Record<string, unknown>).instance_id === instanceId) as Record<string, unknown> | undefined;
    if (!inst) return;
    const next = [...strategies];
    const stratId = inst.strategy_id as string;
    const instParams = (inst.params ?? {}) as Record<string, number>;
    const instBias = inst.bias_config as BiasConfig | undefined;
    const instExit = inst.exit_config as ExitConfig | undefined;
    next[idx] = {
      name: stratId,
      session: ((inst.session as string) ?? "all") as "morning" | "afternoon" | "all",
      mode: instBias?.mode ?? "independent",
      concurrent: true,
      max_fires_per_day: 5,
      time_stop_bars: instExit?.max_hold_bars ?? 20,
      params: { ...getDefaultParams(stratId), ...instParams, max_sl_points: instParams.max_sl_points ?? 20 },
      bias_config: instBias,
      sl_atr_mult: instExit?.sl_atr_mult ?? 0.5,
      tp_atr_mult: instExit?.tp_atr_mult ?? 1.5,
      max_hold_bars: instExit?.max_hold_bars ?? 20,
      slippage_pts: instExit?.slippage_pts ?? 0.5,
    };
    setStrategies(next);
  }
  function updateSlotBias(idx: number, updater: (slot: StrategySlot) => Partial<StrategySlot>) {
    const next = [...strategies];
    const cur = next[idx];
    if (!cur) return;
    next[idx] = { ...cur, ...updater(cur) } as StrategySlot;
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
    // Use per-strategy exit config if set, otherwise defaults
    const firstSlot = strategies[0];
    const exitCfg = {
      sl_atr_mult: firstSlot?.sl_atr_mult ?? 0.5,
      tp_atr_mult: firstSlot?.tp_atr_mult ?? 1.5,
      max_hold_bars: firstSlot?.max_hold_bars ?? 20,
      slippage_pts: firstSlot?.slippage_pts ?? 0.5,
    };
    const req: MultiBacktestRequest = {
      instrument: selectedInstrument,
      start_date: startDate,
      end_date: endDate,
      bias_config: { bias_filters: [], min_agreement: 1, cooldown_bars: 0 },
      // Sync time_stop_bars from max_hold_bars so multi_runner reads the correct value
      strategies: strategies.map((s) => ({
        ...s,
        time_stop_bars: s.max_hold_bars ?? s.time_stop_bars,
      })),
      exit_config: exitCfg,
    };
    onRun(req);
  }


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
            {/* Instance selector */}
            {availableInstances.length > 0 && (
              <div className="space-y-1">
                <label className="text-[10px] font-medium text-slate-500 uppercase">Load from Instance</label>
                <select
                  value=""
                  onChange={(e) => { if (e.target.value) loadInstance(idx, e.target.value); }}
                  className={`${SEL} text-accent-light`}>
                  <option value="">Select saved instance to auto-fill...</option>
                  {availableInstances.map((inst) => {
                    const r = inst as Record<string, unknown>;
                    return (
                      <option key={r.instance_id as string} value={r.instance_id as string}>
                        {String(r.strategy_display ?? r.strategy_id)} — {String(r.instance_name)} ({String(r.mode)})
                      </option>
                    );
                  })}
                </select>
              </div>
            )}
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
            {/* Per-strategy exit rules */}
            {slot.name && (
              <div className="border-t border-surface-border/40 pt-2 mt-2">
                <p className="text-[10px] font-medium text-slate-500 uppercase mb-2">Exit Rules</p>
                <div className="grid grid-cols-4 gap-2">
                  <Field label="SL x ATR">
                    <input type="number" value={slot.sl_atr_mult ?? 0.5} min={0.1} max={5} step={0.1}
                      onChange={(e) => updateSlot(idx, "sl_atr_mult", Number(e.target.value))} className={INP} />
                  </Field>
                  <Field label="TP x ATR">
                    <input type="number" value={slot.tp_atr_mult ?? 1.5} min={0.5} max={10} step={0.1}
                      onChange={(e) => updateSlot(idx, "tp_atr_mult", Number(e.target.value))} className={INP} />
                  </Field>
                  <Field label="Max Hold">
                    <input type="number" value={slot.max_hold_bars ?? 20} min={1} max={375}
                      onChange={(e) => updateSlot(idx, "max_hold_bars", Number(e.target.value))} className={INP} />
                  </Field>
                  <Field label="Slippage">
                    <input type="number" value={slot.slippage_pts ?? 0.5} min={0} max={5} step={0.1}
                      onChange={(e) => updateSlot(idx, "slippage_pts", Number(e.target.value))} className={INP} />
                  </Field>
                </div>
              </div>
            )}
            {/* Per-strategy bias editor */}
            {slot.name && (
              <div className="border-t border-surface-border/40 pt-2 mt-2">
                <div className="flex items-center gap-2 mb-2">
                  <span className="text-[10px] font-medium text-slate-500 uppercase">Bias</span>
                  <div className="flex gap-1">
                    <button onClick={() => updateSlotBias(idx, () => ({ mode: "independent" }))}
                      className={`rounded px-2 py-0.5 text-[10px] font-medium transition-colors ${
                        slot.mode === "independent" ? "bg-surface-light text-white" : "text-slate-600 hover:text-slate-400"
                      }`}>Off</button>
                    <button onClick={() => updateSlotBias(idx, (s) => {
                      const existing = s.bias_config;
                      return {
                        mode: "bias_filtered",
                        bias_config: existing?.bias_filters?.length
                          ? { ...existing, mode: "bias_filtered" as const }
                          : { bias_filters: [{ type: "ema_crossover", timeframe: 5, params: { short: 2, long: 11 } }], min_agreement: 1, mode: "bias_filtered" as const },
                      };
                    })}
                      className={`rounded px-2 py-0.5 text-[10px] font-medium transition-colors ${
                        slot.mode === "bias_filtered" ? "bg-accent/20 text-accent-light" : "text-slate-600 hover:text-slate-400"
                      }`}>Filtered</button>
                  </div>
                  {slot.mode === "bias_filtered" && slot.bias_config && (
                    <div className="flex items-center gap-1 ml-auto">
                      <span className="text-[9px] text-slate-600">Min agree:</span>
                      <input type="number" value={slot.bias_config.min_agreement ?? 1} min={1}
                        max={Math.max(slot.bias_config.bias_filters?.length ?? 1, 1)}
                        onChange={(e) => updateSlotBias(idx, (s) => ({
                          bias_config: { ...(s.bias_config ?? { bias_filters: [], min_agreement: 1, mode: "bias_filtered" as const }), min_agreement: Number(e.target.value) },
                        }))}
                        className="w-10 rounded border border-surface-border bg-surface-light px-1 py-0.5 text-[10px] text-white focus:outline-none" />
                    </div>
                  )}
                </div>
                {slot.mode === "bias_filtered" && slot.bias_config && (
                  <div className="space-y-1.5">
                    {(slot.bias_config.bias_filters ?? []).map((f, fi) => {
                      const updateFilter = (updates: Partial<BiasFilter>) => {
                        updateSlotBias(idx, (s) => {
                          const bc = s.bias_config ?? { bias_filters: [], min_agreement: 1, mode: "bias_filtered" as const };
                          const filters = [...bc.bias_filters];
                          filters[fi] = { ...filters[fi], ...updates } as BiasFilter;
                          return { bias_config: { ...bc, bias_filters: filters } };
                        });
                      };
                      return (
                        <div key={fi} className="flex items-center gap-1.5 rounded-lg bg-surface-dark/30 p-1.5">
                          <select value={f.type} onChange={(e) => {
                            const newType = e.target.value;
                            const def = INDICATOR_TYPES[newType];
                            const newParams: Record<string, number | string> = {};
                            if (newType === "strategy_instance") {
                              const first = availableInstances[0] as Record<string, unknown> | undefined;
                              newParams.instance_name = String(first?.instance_name ?? "");
                              newParams.strategy_name = String(first?.strategy_id ?? "");
                            } else if (def) {
                              for (const p of def.params) newParams[p.key] = p.default;
                            }
                            updateFilter({ type: newType, params: newParams });
                          }} className="rounded border border-surface-border bg-surface-light px-1.5 py-1 text-[10px] text-white focus:outline-none min-w-[100px]">
                            {Object.entries(INDICATOR_TYPES).map(([k, v]) => (
                              <option key={k} value={k}>{v.label}</option>
                            ))}
                          </select>
                          {f.type !== "strategy_instance" && (
                            <select value={f.timeframe} onChange={(e) => updateFilter({ timeframe: Number(e.target.value) })}
                              className="rounded border border-surface-border bg-surface-light px-1 py-1 text-[10px] text-white focus:outline-none w-14">
                              {TF_OPTIONS.map((tf) => <option key={tf} value={tf}>{tf}m</option>)}
                            </select>
                          )}
                          {f.type === "strategy_instance" ? (
                            <select
                              value={String(f.params.instance_name ?? "")}
                              onChange={(e) => {
                                const selected = availableInstances.find(
                                  (i) => (i as Record<string, unknown>).instance_name === e.target.value
                                ) as Record<string, unknown> | undefined;
                                updateFilter({
                                  params: {
                                    ...f.params,
                                    instance_name: e.target.value,
                                    strategy_name: String(selected?.strategy_id ?? ""),
                                  },
                                });
                              }}
                              className="rounded border border-surface-border bg-surface-light px-1.5 py-1 text-[10px] text-white focus:outline-none min-w-[150px]"
                            >
                              {availableInstances.length === 0 && (
                                <option value="">No saved instances</option>
                              )}
                              {availableInstances.map((inst) => {
                                const i = inst as Record<string, unknown>;
                                const key = String(i.instance_id ?? i.instance_name);
                                const label = `${i.strategy_display} — ${i.instance_name}`;
                                return <option key={key} value={String(i.instance_name)}>{label}</option>;
                              })}
                            </select>
                          ) : (
                            INDICATOR_TYPES[f.type]?.params.map((p) => (
                              <input key={p.key} type="number" value={Number(f.params[p.key]) || p.default}
                                min={p.min} max={p.max} step={p.step ?? 1} title={p.label}
                                onChange={(e) => updateFilter({ params: { ...f.params, [p.key]: Number(e.target.value) } })}
                                className="rounded border border-surface-border bg-surface-light px-1 py-1 text-[10px] text-white focus:outline-none w-12" />
                            ))
                          )}
                          <button onClick={() => updateSlotBias(idx, (s) => {
                            const bc = s.bias_config ?? { bias_filters: [], min_agreement: 1, mode: "bias_filtered" as const };
                            const filters = bc.bias_filters.filter((_, i) => i !== fi);
                            return { bias_config: { ...bc, bias_filters: filters }, mode: filters.length === 0 ? "independent" : "bias_filtered" };
                          })} className="rounded p-0.5 text-slate-600 hover:text-loss transition-colors">
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                      );
                    })}
                    <button onClick={() => updateSlotBias(idx, (s) => {
                      const bc = s.bias_config ?? { bias_filters: [], min_agreement: 1, mode: "bias_filtered" as const };
                      return { bias_config: { ...bc, bias_filters: [...bc.bias_filters, { type: "ema_crossover", timeframe: 5, params: { short: 9, long: 21 } }] } };
                    })} className="flex items-center gap-1 text-[10px] text-slate-600 hover:text-accent-light transition-colors">
                      <Plus className="h-2.5 w-2.5" /> Add filter
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
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
