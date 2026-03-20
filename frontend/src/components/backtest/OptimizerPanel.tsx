"use client";

import { useState, useEffect } from "react";
import { apiClient } from "@/lib/api";
import type { BacktestInstrument, BacktestStrategyOption, OptimizeResult } from "@/types/backtest";
import { Zap, Trophy, TrendingUp, TrendingDown, Rocket, Check } from "lucide-react";

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
};

const OPTIMIZE_METRICS = [
  { value: "profit_factor", label: "Profit Factor" },
  { value: "sharpe", label: "Sharpe Ratio" },
  { value: "total_pnl", label: "Total P&L" },
  { value: "win_rate", label: "Win Rate" },
];

const BACKTEST_STRATEGIES = [
  "ttm_squeeze", "supertrend_strategy", "vwap_supertrend",
  "ema_breakdown", "rsi_vwap_scalp", "ema33_ob", "smc_order_block",
];

const INP = "w-full rounded-lg border border-surface-border bg-surface-light px-2.5 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-accent-light";

export function OptimizerPanel() {
  const [instruments, setInstruments] = useState<BacktestInstrument[]>([]);
  const [instrument, setInstrument] = useState("NIFTY_50");
  const [startDate, setStartDate] = useState("2025-01-01");
  const [endDate, setEndDate] = useState("2025-12-31");
  const [strategyName, setStrategyName] = useState("ttm_squeeze");
  const [optimizeFor, setOptimizeFor] = useState("profit_factor");
  const [slAtr, setSlAtr] = useState(0.5);
  const [tpAtr, setTpAtr] = useState(1.5);
  const [maxHold, setMaxHold] = useState(20);
  const [paramGrid, setParamGrid] = useState<Record<string, number[]>>({});
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<OptimizeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deployed, setDeployed] = useState(false);
  const [testBiasOnOff, setTestBiasOnOff] = useState(false);

  useEffect(() => {
    apiClient.get("/backtest/instruments").then((r) => {
      setInstruments(r.data.instruments ?? []);
    }).catch(() => {});
  }, []);

  // Load default param grid when strategy changes
  useEffect(() => {
    const grid = STRATEGY_PARAM_GRIDS[strategyName];
    if (grid) {
      const g: Record<string, number[]> = {};
      for (const p of grid) g[p.key] = [...p.values];
      setParamGrid(g);
    }
  }, [strategyName]);

  const totalCombinations = Object.values(paramGrid).reduce((acc, v) => acc * v.length, 1);

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
        optimize_for: optimizeFor,
        exit_config: { sl_atr_mult: slAtr, tp_atr_mult: tpAtr, max_hold_bars: maxHold, slippage_pts: 0.5 },
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

  const handleDeploy = async () => {
    if (!result?.best) return;
    try {
      await apiClient.post("/strategies/deploy", {
        strategy_name: strategyName,
        instance_name: `${strategyName.replace(/_/g, " ")} — optimized`,
        instruments: [instrument],
        params: result.best.params,
        session: "all",
        mode: "paper",
      });
      setDeployed(true);
    } catch { /* ignore */ }
  };

  const gridDefs = STRATEGY_PARAM_GRIDS[strategyName] ?? [];

  return (
    <div className="rounded-2xl border border-surface-border bg-surface-dark p-5 space-y-5">
      <div className="flex items-center gap-2">
        <Zap className="h-5 w-5 text-accent-light" />
        <h2 className="text-base font-semibold text-white">Parameter Optimizer</h2>
        <span className="text-xs text-slate-500 ml-2">
          Tests all parameter combinations to find the best
        </span>
      </div>

      {/* Config */}
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
            {BACKTEST_STRATEGIES.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ")}</option>)}
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

      {/* Exit config */}
      <div className="grid grid-cols-3 gap-3">
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">SL x ATR</label>
          <input type="number" value={slAtr} min={0.1} max={5} step={0.1} onChange={(e) => setSlAtr(Number(e.target.value))} className={INP} />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">TP x ATR</label>
          <input type="number" value={tpAtr} min={0.5} max={10} step={0.1} onChange={(e) => setTpAtr(Number(e.target.value))} className={INP} />
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Max Hold (bars)</label>
          <input type="number" value={maxHold} min={1} max={375} onChange={(e) => setMaxHold(Number(e.target.value))} className={INP} />
        </div>
      </div>

      {/* Bias test toggle */}
      <label className="flex items-center gap-2 cursor-pointer text-xs text-slate-300 select-none">
        <div className={`w-8 h-4 rounded-full transition-colors relative ${testBiasOnOff ? "bg-accent" : "bg-surface-border"}`}
          onClick={() => setTestBiasOnOff(!testBiasOnOff)}>
          <div className={`absolute top-0.5 w-3 h-3 rounded-full bg-white transition-transform ${testBiasOnOff ? "translate-x-4" : "translate-x-0.5"}`} />
        </div>
        Test with bias ON vs OFF (doubles combinations, finds if bias helps or hurts)
      </label>

      {/* Parameter Grid */}
      <div>
        <label className="text-[10px] font-medium text-slate-500 uppercase mb-2 block">
          Parameter Grid ({totalCombinations} combinations)
        </label>
        <div className="space-y-2">
          {gridDefs.map((p) => (
            <div key={p.key} className="flex items-center gap-3 rounded-lg border border-surface-border/50 bg-surface-light/20 p-2">
              <span className="text-xs text-slate-400 w-24 shrink-0">{p.label}</span>
              <input
                type="text"
                value={(paramGrid[p.key] ?? p.values).join(", ")}
                onChange={(e) => {
                  const vals = e.target.value.split(",").map((v) => parseFloat(v.trim())).filter((v) => !isNaN(v));
                  if (vals.length > 0) setParamGrid((prev) => ({ ...prev, [p.key]: vals }));
                }}
                className="flex-1 rounded-md border border-surface-border bg-surface px-2 py-1 text-xs text-white focus:border-accent focus:outline-none"
                placeholder="comma-separated values"
              />
            </div>
          ))}
        </div>
      </div>

      {/* Run button */}
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-500">
          {totalCombinations}{testBiasOnOff ? ` × 2 bias` : ""} = {totalCombinations * (testBiasOnOff ? 2 : 1)} runs
        </span>
        <button onClick={handleRun}
          disabled={isRunning || totalCombinations === 0}
          className="rounded-xl bg-accent px-6 py-2.5 text-sm font-semibold text-white hover:bg-accent-light transition-colors disabled:opacity-40 flex items-center gap-2">
          {isRunning ? (
            <><span className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" /> Optimizing ({totalCombinations})...</>
          ) : (
            <><Zap className="h-4 w-4" /> Run Optimization</>
          )}
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-loss/40 bg-loss/10 p-3 text-sm text-loss">{error}</div>
      )}

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {/* Best result */}
          {result.best && (
            <div className="rounded-xl border-2 border-profit/30 bg-profit/5 p-4">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <Trophy className="h-5 w-5 text-amber-400" />
                  <h3 className="text-sm font-bold text-white">Best Parameters</h3>
                </div>
                <button onClick={handleDeploy} disabled={deployed}
                  className={`flex items-center gap-1 rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                    deployed ? "bg-profit/20 text-profit" : "bg-accent hover:bg-accent-light text-white"
                  }`}>
                  {deployed ? <><Check className="h-3 w-3" /> Deployed</> : <><Rocket className="h-3 w-3" /> Deploy to Paper</>}
                </button>
              </div>
              <div className="flex flex-wrap gap-2 mb-3">
                {Object.entries(result.best.params).map(([k, v]) => (
                  <span key={k} className="rounded-full bg-surface-dark px-3 py-1 text-xs font-medium text-white">
                    {k}: {v}
                  </span>
                ))}
              </div>
              <div className="grid grid-cols-3 gap-2 sm:grid-cols-6">
                {[
                  { label: "Trades", value: String(result.best.total_trades), color: "text-white" },
                  { label: "Win Rate", value: `${result.best.win_rate}%`, color: result.best.win_rate >= 50 ? "text-profit" : "text-yellow-400" },
                  { label: "Profit Factor", value: result.best.profit_factor.toFixed(2), color: result.best.profit_factor >= 1.5 ? "text-profit" : "text-yellow-400" },
                  { label: "Sharpe", value: result.best.sharpe.toFixed(3), color: result.best.sharpe >= 1 ? "text-profit" : "text-slate-400" },
                  { label: "Net P&L", value: `${result.best.total_pnl >= 0 ? "+" : ""}${result.best.total_pnl} pts`, color: result.best.total_pnl >= 0 ? "text-profit" : "text-loss" },
                  { label: "Max DD", value: `-${result.best.max_drawdown}%`, color: result.best.max_drawdown > 20 ? "text-loss" : "text-white" },
                ].map((r) => (
                  <div key={r.label} className="text-center">
                    <p className="text-[10px] text-slate-500">{r.label}</p>
                    <p className={`text-xs font-bold tabular-nums ${r.color}`}>{r.value}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* All results table */}
          <div>
            <h3 className="text-sm font-semibold text-white mb-2">
              All Results ({result.results.length} of {result.total_combinations})
            </h3>
            <div className="overflow-x-auto rounded-lg border border-surface-border">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-surface-border bg-surface-dark text-slate-500">
                    <th className="px-3 py-2 text-left font-medium">Rank</th>
                    <th className="px-3 py-2 text-left font-medium">Parameters</th>
                    <th className="px-3 py-2 text-center font-medium">Bias</th>
                    <th className="px-3 py-2 text-right font-medium">Trades</th>
                    <th className="px-3 py-2 text-right font-medium">Win %</th>
                    <th className="px-3 py-2 text-right font-medium">PF</th>
                    <th className="px-3 py-2 text-right font-medium">Sharpe</th>
                    <th className="px-3 py-2 text-right font-medium">P&L</th>
                    <th className="px-3 py-2 text-right font-medium">Max DD</th>
                  </tr>
                </thead>
                <tbody>
                  {result.results.map((r, idx) => {
                    const isBest = result.best && JSON.stringify(r.params) === JSON.stringify(result.best.params);
                    return (
                      <tr key={idx} className={`border-b border-surface-border/30 ${isBest ? "bg-profit/5" : "hover:bg-surface-light/20"}`}>
                        <td className="px-3 py-2 tabular-nums">{idx + 1}</td>
                        <td className="px-3 py-2">
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(r.params).map(([k, v]) => (
                              <span key={k} className="rounded bg-surface-dark px-1.5 py-0.5 text-[10px] text-slate-400">
                                {k}={v}
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
    </div>
  );
}
