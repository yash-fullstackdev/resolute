"use client";

import { useState, useEffect } from "react";
import { apiClient } from "@/lib/api";
import { STRATEGY_NAMES } from "@/lib/constants";
import type { BacktestInstrument, BiasConfig, BiasFilter, OptimizeResult } from "@/types/backtest";
import { Zap, Trophy, Rocket, Check, Plus, Trash2 } from "lucide-react";
import { DeployDialog } from "./DeployDialog";

// ── Strategy param grids ──────────────────────────────────────────────────────

const STRATEGY_PARAM_GRIDS: Record<string, { key: string; label: string; values: number[] }[]> = {
  ttm_squeeze: [
    { key: "bb_period", label: "BB Period", values: [15, 20, 25, 30] },
    { key: "bb_std", label: "BB Std", values: [1.5, 2.0, 2.5] },
    { key: "kc_mult", label: "KC Mult", values: [1.0, 1.5, 2.0] },
    { key: "max_sl_points", label: "Max SL", values: [15, 20, 30, 50] },
  ],
  supertrend_strategy: [
    { key: "period", label: "ST Period", values: [7, 10, 14, 20] },
    { key: "multiplier", label: "ST Mult", values: [2.0, 3.0, 4.0] },
    { key: "max_sl_points", label: "Max SL", values: [15, 20, 30] },
  ],
  ema_breakdown: [
    { key: "ema_short", label: "EMA Short", values: [2, 3, 5] },
    { key: "ema_long", label: "EMA Long", values: [9, 11, 15, 21] },
    { key: "rsi_period", label: "RSI Period", values: [10, 14, 20] },
    { key: "max_sl_points", label: "Max SL", values: [15, 20, 30] },
  ],
  ema33_ob: [
    { key: "ema_period", label: "EMA Period", values: [21, 33, 50] },
    { key: "rsi_bull_threshold", label: "RSI Bull", values: [55, 60, 65] },
    { key: "rsi_bear_threshold", label: "RSI Bear", values: [35, 40, 45] },
    { key: "max_sl_points", label: "Max SL", values: [15, 20, 30] },
  ],
  smc_order_block: [
    { key: "ob_length", label: "OB Length", values: [4, 6, 8, 10] },
    { key: "max_sl_points", label: "Max SL", values: [15, 20, 30, 50] },
  ],
  rsi_vwap_scalp: [
    { key: "rsi_period", label: "RSI Period", values: [10, 14, 20] },
    { key: "rsi_oversold", label: "Oversold", values: [25, 30, 35] },
    { key: "rsi_overbought", label: "Overbought", values: [65, 70, 75] },
    { key: "max_sl_points", label: "Max SL", values: [10, 15, 20] },
  ],
  vwap_supertrend: [
    { key: "st_period", label: "ST Period", values: [7, 10, 14] },
    { key: "st_multiplier", label: "ST Mult", values: [2.0, 3.0, 4.0] },
    { key: "max_sl_points", label: "Max SL", values: [15, 20, 30] },
  ],
  brahmaastra: [
    { key: "gap_threshold_pct", label: "Gap %", values: [0.2, 0.4, 0.6, 0.8] },
    { key: "wick_ratio_min", label: "Wick Ratio", values: [1.0, 1.5, 2.0, 2.5] },
    { key: "final_target_rr", label: "Final RR", values: [1.0, 1.5, 2.0] },
  ],
  ema5_mean_reversion: [
    { key: "ema_period", label: "EMA Period", values: [3, 5, 8] },
    { key: "min_distance_ema_pct", label: "Min Dist %", values: [0.001, 0.002, 0.003, 0.005] },
    { key: "rr_min", label: "Min RR", values: [2.0, 3.0, 4.0] },
    { key: "daily_loss_limit", label: "Loss Limit", values: [2, 3, 4] },
  ],
  parent_child_momentum: [
    { key: "ema_short", label: "EMA Short", values: [8, 10, 13] },
    { key: "ema_long", label: "EMA Long", values: [80, 100, 120] },
    { key: "macd_fast", label: "MACD Fast", values: [36, 48, 60] },
    { key: "profit_target_pct", label: "Profit %", values: [20, 25, 30] },
  ],
};

// Default exit grids per strategy (matches fast_strategies.py STRATEGY_EXIT_DEFAULTS)
const STRATEGY_EXIT_GRID_DEFAULTS: Record<string, { sl_atr_mult: number[]; tp_atr_mult: number[]; max_hold_bars: number[] }> = {
  brahmaastra:           { sl_atr_mult: [0.5], tp_atr_mult: [0.75], max_hold_bars: [12] },
  ema5_mean_reversion:   { sl_atr_mult: [0.5], tp_atr_mult: [1.5],  max_hold_bars: [24] },
  parent_child_momentum: { sl_atr_mult: [1.0], tp_atr_mult: [1.5],  max_hold_bars: [16] },
};
const DEFAULT_EXIT_GRID = { sl_atr_mult: [0.5], tp_atr_mult: [1.5], max_hold_bars: [20] };

const EXIT_GRID_DEFS = [
  { key: "sl_atr_mult",  label: "SL x ATR",       hint: "e.g. 0.5, 1.0" },
  { key: "tp_atr_mult",  label: "TP x ATR",        hint: "e.g. 1.0, 1.5, 2.0" },
  { key: "max_hold_bars", label: "Max Hold (bars)", hint: "e.g. 12, 20, 30" },
];

// ── Bias indicator definitions (same as BacktestConfigPanel) ─────────────────

const INDICATOR_TYPES: Record<string, { label: string; params: { key: string; label: string; default: number; min?: number; max?: number; step?: number }[] }> = {
  ema_crossover: { label: "EMA Crossover", params: [
    { key: "short", label: "Short Period", default: 9, min: 1, max: 200 },
    { key: "long",  label: "Long Period",  default: 21, min: 2, max: 500 },
  ]},
  supertrend: { label: "Supertrend", params: [
    { key: "period",     label: "Period",     default: 10, min: 5, max: 50 },
    { key: "multiplier", label: "Multiplier", default: 3.0, min: 0.5, max: 10, step: 0.1 },
  ]},
  rsi_zone: { label: "RSI Zone", params: [
    { key: "period",     label: "Period",     default: 14, min: 2, max: 50 },
    { key: "overbought", label: "Overbought", default: 70, min: 50, max: 95 },
    { key: "oversold",   label: "Oversold",   default: 30, min: 5,  max: 50 },
  ]},
  ttm_momentum: { label: "TTM Squeeze Momentum", params: [
    { key: "period", label: "Period", default: 20, min: 5, max: 50 },
  ]},
  macd_signal: { label: "MACD Signal", params: [
    { key: "fast",   label: "Fast EMA", default: 12, min: 2, max: 50 },
    { key: "slow",   label: "Slow EMA", default: 26, min: 5, max: 100 },
    { key: "signal", label: "Signal",   default: 9,  min: 2, max: 30 },
  ]},
  ema_zone: { label: "EMA Zone + RSI", params: [
    { key: "ema_period", label: "EMA Period", default: 33, min: 5, max: 200 },
    { key: "rsi_period", label: "RSI Period", default: 14, min: 2, max: 50 },
    { key: "rsi_bull",   label: "RSI Bull",   default: 60, min: 50, max: 90 },
    { key: "rsi_bear",   label: "RSI Bear",   default: 40, min: 10, max: 50 },
  ]},
  price_vs_ema: { label: "Price vs EMA", params: [
    { key: "period", label: "EMA Period", default: 20, min: 2, max: 200 },
  ]},
  bollinger_squeeze: { label: "Bollinger Squeeze", params: [
    { key: "period",   label: "Period",  default: 20, min: 5, max: 50 },
    { key: "std_mult", label: "Std Dev", default: 2.0, min: 0.5, max: 4, step: 0.1 },
  ]},
};
const TF_OPTIONS = [1, 2, 3, 5, 10, 15, 30, 60];

const BACKTEST_STRATEGIES = [
  "ttm_squeeze", "supertrend_strategy", "vwap_supertrend",
  "ema_breakdown", "rsi_vwap_scalp", "ema33_ob", "smc_order_block",
  "brahmaastra", "ema5_mean_reversion", "parent_child_momentum",
];

const OPTIMIZE_METRICS = [
  { value: "profit_factor", label: "Profit Factor" },
  { value: "sharpe", label: "Sharpe Ratio" },
  { value: "total_pnl", label: "Total P&L" },
  { value: "win_rate", label: "Win Rate" },
];

const INP = "w-full rounded-lg border border-surface-border bg-surface-light px-2.5 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-accent-light";

// ── Helpers ───────────────────────────────────────────────────────────────────

function parseNums(raw: string): number[] {
  return raw.split(",").map((v) => parseFloat(v.trim())).filter((v) => !isNaN(v));
}

function SweepRow({ label, hint, rawVal, parsed, onChange, onBlur }: {
  label: string; hint: string; rawVal: string; parsed: number[];
  onChange: (v: string) => void; onBlur: (v: string) => void;
}) {
  const isValid = parsed.length > 0;
  return (
    <div className="rounded-lg border border-surface-border/50 bg-surface-light/20 p-2.5 space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-slate-300">{label}</span>
        <div className="flex gap-1 items-center">
          {parsed.map((v, i) => (
            <span key={i} className="rounded bg-accent/20 px-1.5 py-0.5 text-[10px] font-mono text-accent-light">{v}</span>
          ))}
          <span className="text-[10px] text-slate-600 ml-1">{parsed.length} val{parsed.length !== 1 ? "s" : ""}</span>
        </div>
      </div>
      <input
        type="text" value={rawVal}
        onChange={(e) => onChange(e.target.value)}
        onBlur={(e) => onBlur(e.target.value)}
        placeholder={hint}
        className={`w-full rounded-md border px-2.5 py-1.5 text-xs text-white bg-surface focus:outline-none focus:ring-1 ${
          isValid ? "border-surface-border focus:ring-accent focus:border-accent" : "border-loss/60 focus:ring-loss"
        }`}
      />
      {!isValid && <p className="text-[10px] text-loss">Enter comma-separated numbers</p>}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function OptimizerPanel() {
  const [instruments, setInstruments] = useState<BacktestInstrument[]>([]);
  const [instrument, setInstrument] = useState("NIFTY_50");
  const [startDate, setStartDate] = useState("2025-01-01");
  const [endDate, setEndDate] = useState("2025-12-31");
  const [strategyName, setStrategyName] = useState("ttm_squeeze");
  const [optimizeFor, setOptimizeFor] = useState("profit_factor");

  // Strategy param grid
  const [paramGrid, setParamGrid] = useState<Record<string, number[]>>({});
  const [paramRaw, setParamRaw] = useState<Record<string, string>>({});

  // Exit param grid (sweepable)
  const [exitGrid, setExitGrid] = useState<Record<string, number[]>>(DEFAULT_EXIT_GRID);
  const [exitRaw, setExitRaw] = useState<Record<string, string>>({
    sl_atr_mult: DEFAULT_EXIT_GRID.sl_atr_mult.join(", "),
    tp_atr_mult: DEFAULT_EXIT_GRID.tp_atr_mult.join(", "),
    max_hold_bars: DEFAULT_EXIT_GRID.max_hold_bars.join(", "),
  });

  // Bias
  const [biasMode, setBiasMode] = useState<"off" | "filtered">("off");
  const [biasFilters, setBiasFilters] = useState<BiasFilter[]>([]);
  const [minAgreement, setMinAgreement] = useState(1);
  const [testBiasOnOff, setTestBiasOnOff] = useState(false);

  // Run/result
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<OptimizeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Deploy dialog
  const [showDeploy, setShowDeploy] = useState(false);
  const [deployed, setDeployed] = useState(false);

  useEffect(() => {
    apiClient.get("/backtest/instruments").then((r) => {
      setInstruments(r.data.instruments ?? []);
    }).catch(() => {});
  }, []);

  // Load defaults when strategy changes
  useEffect(() => {
    const grid = STRATEGY_PARAM_GRIDS[strategyName];
    if (grid) {
      const g: Record<string, number[]> = {};
      const raw: Record<string, string> = {};
      for (const p of grid) { g[p.key] = [...p.values]; raw[p.key] = p.values.join(", "); }
      setParamGrid(g);
      setParamRaw(raw);
    }
    const exitDef = STRATEGY_EXIT_GRID_DEFAULTS[strategyName] ?? DEFAULT_EXIT_GRID;
    setExitGrid({ ...exitDef });
    setExitRaw({
      sl_atr_mult:   exitDef.sl_atr_mult.join(", "),
      tp_atr_mult:   exitDef.tp_atr_mult.join(", "),
      max_hold_bars: exitDef.max_hold_bars.join(", "),
    });
    setDeployed(false);
    setResult(null);
  }, [strategyName]);

  const paramCombos = Object.values(paramGrid).reduce((acc, v) => acc * v.length, 1);
  const exitCombos  = Object.values(exitGrid).reduce((acc, v) => acc * v.length, 1);
  const biasMult    = testBiasOnOff && biasMode === "filtered" ? 2 : 1;
  const totalCombos = paramCombos * exitCombos * biasMult;

  const biasConfig: BiasConfig | null = biasMode === "filtered" && biasFilters.length > 0
    ? { bias_filters: biasFilters, min_agreement: minAgreement, mode: "bias_filtered" }
    : null;

  const handleRun = async () => {
    setIsRunning(true);
    setError(null);
    setResult(null);
    setDeployed(false);
    try {
      const res = await apiClient.post<OptimizeResult>("/backtest/optimize", {
        instrument,
        start_date: startDate,
        end_date: endDate,
        strategy_name: strategyName,
        param_grid: paramGrid,
        exit_grid: exitGrid,
        optimize_for: optimizeFor,
        bias_config: biasConfig ?? { bias_filters: [], min_agreement: 1 },
        test_bias_on_off: testBiasOnOff,
      });
      setResult(res.data);
    } catch (err: unknown) {
      const e = err as { response?: { data?: { error?: { message?: string } } }; message?: string };
      setError(e?.response?.data?.error?.message ?? e?.message ?? "Optimization failed");
    } finally {
      setIsRunning(false);
    }
  };

  const handleDeployConfirm = async (instanceName: string) => {
    if (!result?.best) return;
    setShowDeploy(false);
    try {
      await apiClient.post("/strategies/deploy", {
        strategy_name: strategyName,
        instance_name: instanceName,
        instruments: [instrument],
        params: result.best.params,
        session: "all",
        mode: "paper",
        bias_config: biasConfig,
        exit_config: {
          sl_atr_mult:   result.best.params.sl_atr_mult  ?? (exitGrid.sl_atr_mult?.[0]  ?? 0.5),
          tp_atr_mult:   result.best.params.tp_atr_mult  ?? (exitGrid.tp_atr_mult?.[0]  ?? 1.5),
          max_hold_bars: result.best.params.max_hold_bars ?? (exitGrid.max_hold_bars?.[0] ?? 20),
          slippage_pts: 0.5,
        },
      });
      setDeployed(true);
    } catch { /* ignore */ }
  };

  const gridDefs = STRATEGY_PARAM_GRIDS[strategyName] ?? [];
  const defaultDeployName = `${STRATEGY_NAMES[strategyName] ?? strategyName} — optimized`;

  // ── Bias helpers ──────────────────────────────────────────────────
  function updateFilter(idx: number, updates: Partial<BiasFilter>) {
    setBiasFilters((prev) => prev.map((f, i) => i === idx ? { ...f, ...updates } : f));
  }
  function removeFilter(idx: number) {
    const next = biasFilters.filter((_, i) => i !== idx);
    setBiasFilters(next);
    if (next.length === 0) setBiasMode("off");
  }
  function addFilter() {
    setBiasFilters((prev) => [...prev, { type: "ema_crossover", timeframe: 5, params: { short: 9, long: 21 } }]);
    setBiasMode("filtered");
  }

  return (
    <div className="rounded-2xl border border-surface-border bg-surface-dark p-5 space-y-5">
      <div className="flex items-center gap-2">
        <Zap className="h-5 w-5 text-accent-light" />
        <h2 className="text-base font-semibold text-white">Parameter Optimizer</h2>
        <span className="text-xs text-slate-500 ml-2">Tests all parameter combinations to find the best</span>
      </div>

      {/* Config row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Instrument</label>
          <select value={instrument} onChange={(e) => setInstrument(e.target.value)} className={INP}>
            {instruments.map((i) => <option key={i.name} value={i.name}>{i.display_name}</option>)}
          </select>
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Strategy</label>
          <select value={strategyName} onChange={(e) => setStrategyName(e.target.value)} className={INP}>
            {BACKTEST_STRATEGIES.map((s) => <option key={s} value={s}>{STRATEGY_NAMES[s] ?? s.replace(/_/g, " ")}</option>)}
          </select>
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Start Date</label>
          <input type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} className={INP} />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">End Date</label>
          <input type="date" value={endDate} onChange={(e) => setEndDate(e.target.value)} className={INP} />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Optimize For</label>
          <select value={optimizeFor} onChange={(e) => setOptimizeFor(e.target.value)} className={INP}>
            {OPTIMIZE_METRICS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </div>
      </div>

      {/* Strategy Parameter Grid */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Strategy Parameters</label>
          <span className="text-[10px] font-semibold text-accent-light">{paramCombos} combo{paramCombos !== 1 ? "s" : ""}</span>
        </div>
        <div className="space-y-2">
          {gridDefs.map((p) => {
            const parsed = paramGrid[p.key] ?? p.values;
            const rawVal = paramRaw[p.key] ?? parsed.join(", ");
            return (
              <SweepRow key={p.key} label={p.label} hint={`e.g. ${p.values.join(", ")}`} rawVal={rawVal} parsed={parsed}
                onChange={(v) => setParamRaw((prev) => ({ ...prev, [p.key]: v }))}
                onBlur={(v) => {
                  const vals = parseNums(v);
                  if (vals.length > 0) {
                    setParamGrid((prev) => ({ ...prev, [p.key]: vals }));
                    setParamRaw((prev) => ({ ...prev, [p.key]: vals.join(", ") }));
                  } else {
                    setParamRaw((prev) => ({ ...prev, [p.key]: parsed.join(", ") }));
                  }
                }}
              />
            );
          })}
        </div>
      </div>

      {/* Exit Rules Grid */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Exit Rules</label>
          <span className="text-[10px] font-semibold text-accent-light">{exitCombos} combo{exitCombos !== 1 ? "s" : ""}</span>
        </div>
        <div className="space-y-2">
          {EXIT_GRID_DEFS.map((p) => {
            const parsed = exitGrid[p.key] ?? [0];
            const rawVal = exitRaw[p.key] ?? parsed.join(", ");
            return (
              <SweepRow key={p.key} label={p.label} hint={p.hint} rawVal={rawVal} parsed={parsed}
                onChange={(v) => setExitRaw((prev) => ({ ...prev, [p.key]: v }))}
                onBlur={(v) => {
                  const vals = parseNums(v);
                  if (vals.length > 0) {
                    setExitGrid((prev) => ({ ...prev, [p.key]: vals }));
                    setExitRaw((prev) => ({ ...prev, [p.key]: vals.join(", ") }));
                  } else {
                    setExitRaw((prev) => ({ ...prev, [p.key]: parsed.join(", ") }));
                  }
                }}
              />
            );
          })}
          {strategyName === "brahmaastra" && (
            <p className="text-[10px] text-amber-400 px-1">Kill switch at 10:30 — entries 9:15–10:15 give 15–75 bars max</p>
          )}
        </div>
      </div>

      {/* Bias */}
      <div className="rounded-lg border border-surface-border/50 p-3 space-y-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-medium text-slate-500 uppercase">Bias</span>
          <div className="flex gap-1">
            <button onClick={() => setBiasMode("off")}
              className={`rounded px-2 py-0.5 text-[10px] font-medium transition-colors ${biasMode === "off" ? "bg-surface-light text-white" : "text-slate-600 hover:text-slate-400"}`}>
              Off
            </button>
            <button onClick={() => { setBiasMode("filtered"); if (biasFilters.length === 0) addFilter(); }}
              className={`rounded px-2 py-0.5 text-[10px] font-medium transition-colors ${biasMode === "filtered" ? "bg-accent/20 text-accent-light" : "text-slate-600 hover:text-slate-400"}`}>
              Filtered
            </button>
          </div>
          {biasMode === "filtered" && biasFilters.length > 0 && (
            <div className="flex items-center gap-1 ml-auto">
              <span className="text-[9px] text-slate-600">Min agree:</span>
              <input type="number" value={minAgreement} min={1} max={Math.max(biasFilters.length, 1)}
                onChange={(e) => setMinAgreement(Number(e.target.value))}
                className="w-10 rounded border border-surface-border bg-surface-light px-1 py-0.5 text-[10px] text-white focus:outline-none" />
            </div>
          )}
        </div>

        {biasMode === "filtered" && (
          <div className="space-y-1.5">
            {biasFilters.map((f, fi) => (
              <div key={fi} className="flex items-center gap-1.5 rounded-lg bg-surface-dark/30 p-1.5">
                <select value={f.type} onChange={(e) => {
                  const def = INDICATOR_TYPES[e.target.value];
                  const newParams: Record<string, number> = {};
                  if (def) for (const p of def.params) newParams[p.key] = p.default;
                  updateFilter(fi, { type: e.target.value, params: newParams });
                }} className="rounded border border-surface-border bg-surface-light px-1.5 py-1 text-[10px] text-white focus:outline-none min-w-[110px]">
                  {Object.entries(INDICATOR_TYPES).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
                </select>
                <select value={f.timeframe} onChange={(e) => updateFilter(fi, { timeframe: Number(e.target.value) })}
                  className="rounded border border-surface-border bg-surface-light px-1 py-1 text-[10px] text-white focus:outline-none w-14">
                  {TF_OPTIONS.map((tf) => <option key={tf} value={tf}>{tf}m</option>)}
                </select>
                {INDICATOR_TYPES[f.type]?.params.map((p) => (
                  <input key={p.key} type="number" value={f.params[p.key] ?? p.default}
                    min={p.min} max={p.max} step={p.step ?? 1} title={p.label}
                    onChange={(e) => updateFilter(fi, { params: { ...f.params, [p.key]: Number(e.target.value) } })}
                    className="rounded border border-surface-border bg-surface-light px-1 py-1 text-[10px] text-white focus:outline-none w-12" />
                ))}
                <button onClick={() => removeFilter(fi)} className="rounded p-0.5 text-slate-600 hover:text-loss transition-colors">
                  <Trash2 className="h-3 w-3" />
                </button>
              </div>
            ))}
            <button onClick={addFilter} className="flex items-center gap-1 text-[10px] text-slate-600 hover:text-accent-light transition-colors">
              <Plus className="h-2.5 w-2.5" /> Add filter
            </button>
          </div>
        )}

        {biasMode === "filtered" && biasFilters.length > 0 && (
          <label className="flex items-center gap-2 cursor-pointer text-[10px] text-slate-400 select-none">
            <div className={`w-7 h-3.5 rounded-full transition-colors relative cursor-pointer ${testBiasOnOff ? "bg-accent" : "bg-surface-border"}`}
              onClick={() => setTestBiasOnOff(!testBiasOnOff)}>
              <div className={`absolute top-0.5 w-2.5 h-2.5 rounded-full bg-white transition-transform ${testBiasOnOff ? "translate-x-3.5" : "translate-x-0.5"}`} />
            </div>
            Test bias ON vs OFF (doubles combinations)
          </label>
        )}
      </div>

      {/* Total combinations summary */}
      <div className="flex items-center justify-between">
        <div className="text-[10px] text-slate-500">
          {paramCombos} strategy × {exitCombos} exit{biasMult > 1 ? ` × ${biasMult} bias` : ""} = <span className="font-bold text-accent-light">{totalCombos} total runs</span>
        </div>
        <button onClick={handleRun}
          disabled={isRunning || totalCombos === 0}
          className="rounded-xl bg-accent px-6 py-2.5 text-sm font-semibold text-white hover:bg-accent-light transition-colors disabled:opacity-40 flex items-center gap-2">
          {isRunning
            ? <><span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" /> Optimizing ({totalCombos})...</>
            : <><Zap className="h-4 w-4" /> Run Optimization</>}
        </button>
      </div>

      {/* Error */}
      {error && <div className="rounded-lg border border-loss/40 bg-loss/10 p-3 text-sm text-loss">{error}</div>}

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {result.best && (
            <div className="rounded-xl border-2 border-profit/30 bg-profit/5 p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <Trophy className="h-5 w-5 text-amber-400" />
                  <h3 className="text-sm font-bold text-white">Best Parameters</h3>
                </div>
                <button onClick={() => setShowDeploy(true)} disabled={deployed}
                  className={`flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                    deployed ? "bg-profit/20 text-profit" : "bg-accent hover:bg-accent-light text-white"
                  }`}>
                  {deployed ? <><Check className="h-3 w-3" /> Deployed</> : <><Rocket className="h-3 w-3" /> Deploy to Paper</>}
                </button>
              </div>
              <div className="flex flex-wrap gap-2 mb-3">
                {Object.entries(result.best.params).map(([k, v]) => (
                  <span key={k} className="rounded-full bg-surface-dark px-3 py-1 text-xs font-medium text-white">
                    {k}: {typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : v}
                  </span>
                ))}
              </div>
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                {[
                  { label: "Trades",    value: String(result.best.total_trades), color: "text-white" },
                  { label: "Win Rate",  value: `${result.best.win_rate}%`, color: result.best.win_rate >= 50 ? "text-profit" : "text-yellow-400" },
                  { label: "Prof. Factor", value: result.best.profit_factor.toFixed(2), color: result.best.profit_factor >= 1.5 ? "text-profit" : "text-yellow-400" },
                  { label: "Sharpe",    value: result.best.sharpe.toFixed(3), color: result.best.sharpe >= 1 ? "text-profit" : "text-slate-400" },
                  { label: "Net P&L",  value: `${result.best.total_pnl >= 0 ? "+" : ""}${result.best.total_pnl} pts`, color: result.best.total_pnl >= 0 ? "text-profit" : "text-loss" },
                  { label: "Max DD",   value: `-${result.best.max_drawdown}%`, color: result.best.max_drawdown > 20 ? "text-loss" : "text-white" },
                ].map((r) => (
                  <div key={r.label} className="text-center">
                    <p className="text-[10px] text-slate-500">{r.label}</p>
                    <p className={`text-xs font-bold tabular-nums ${r.color}`}>{r.value}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <h3 className="text-sm font-semibold text-white mb-2">
              All Results ({result.results.length} of {result.total_combinations})
            </h3>
            <div className="overflow-x-auto rounded-lg border border-surface-border">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-surface-border bg-surface-dark text-slate-500">
                    <th className="px-3 py-2 text-left font-medium">#</th>
                    <th className="px-3 py-2 text-left font-medium">Parameters</th>
                    <th className="px-3 py-2 text-center font-medium">Bias</th>
                    <th className="px-3 py-2 text-right font-medium">Trades</th>
                    <th className="px-3 py-2 text-right font-medium">Win %</th>
                    <th className="px-3 py-2 text-right font-medium">PF</th>
                    <th className="px-3 py-2 text-right font-medium">Sharpe</th>
                    <th className="px-3 py-2 text-right font-medium">P&L</th>
                    <th className="px-3 py-2 text-right font-medium">DD%</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.map((r, idx) => {
                    const isBest = result.best && JSON.stringify(r.params) === JSON.stringify(result.best.params);
                    return (
                      <tr key={idx} className={`border-b border-surface-border/30 ${isBest ? "bg-profit/5" : "hover:bg-surface-light/20"}`}>
                        <td className="px-3 py-2 tabular-nums text-slate-500">{idx + 1}</td>
                        <td className="px-3 py-2">
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(r.params).map(([k, v]) => (
                              <span key={k} className="rounded bg-surface-dark px-1.5 py-0.5 text-[10px] text-slate-400">
                                {k}={typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(3)) : v}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-center">
                          <span className={`rounded px-1.5 py-0.5 text-[9px] font-bold ${
                            (r as Record<string, unknown>).bias === "bias_on" ? "bg-accent/20 text-accent-light" : "bg-slate-700 text-slate-400"
                          }`}>{(r as Record<string, unknown>).bias === "bias_on" ? "ON" : "OFF"}</span>
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-white">{r.total_trades}</td>
                        <td className={`px-3 py-2 text-right tabular-nums ${r.win_rate >= 50 ? "text-profit" : "text-slate-400"}`}>{r.win_rate}%</td>
                        <td className={`px-3 py-2 text-right tabular-nums ${r.profit_factor >= 1.5 ? "text-profit" : "text-slate-400"}`}>{r.profit_factor}</td>
                        <td className={`px-3 py-2 text-right tabular-nums ${r.sharpe >= 1 ? "text-profit" : "text-slate-400"}`}>{r.sharpe}</td>
                        <td className={`px-3 py-2 text-right tabular-nums font-medium ${r.total_pnl >= 0 ? "text-profit" : "text-loss"}`}>
                          {r.total_pnl >= 0 ? "+" : ""}{r.total_pnl}
                        </td>
                        <td className="px-3 py-2 text-right tabular-nums text-slate-400">-{r.max_drawdown}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* Deploy dialog */}
      {showDeploy && (
        <DeployDialog
          defaultName={defaultDeployName}
          onConfirm={handleDeployConfirm}
          onCancel={() => setShowDeploy(false)}
        />
      )}
    </div>
  );
}
