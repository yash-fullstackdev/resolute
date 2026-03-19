"use client";

import React, { useState, useCallback } from "react";
import { BacktestConfigPanel } from "@/components/backtest/BacktestConfigPanel";
import { MetricsGrid } from "@/components/backtest/MetricsGrid";
import { EquityCurveChart } from "@/components/backtest/EquityCurveChart";
import { MonthlyHeatmap } from "@/components/backtest/MonthlyHeatmap";
import { TradeLog } from "@/components/backtest/TradeLog";
import type { MultiBacktestRequest, BacktestResult } from "@/types/backtest";
import { apiClient } from "@/lib/api";
import { AlertCircle, TrendingUp, BarChart2, Calendar, List, Layers } from "lucide-react";

type ResultTab = "overview" | "equity" | "heatmap" | "trades" | "strategies";

function StrategyBreakdown({ result }: { result: BacktestResult }) {
  const strategyNames = Object.keys(result.per_strategy_metrics ?? {});
  if (strategyNames.length === 0) return null;

  return (
    <div className="space-y-4">
      {strategyNames.map((name) => {
        const m = result.per_strategy_metrics[name];
        if (!m) return null;
        const equity = result.per_strategy_equity?.[name] ?? [];
        const returnColor = m.total_return_pct >= 0 ? "text-profit" : "text-loss";
        const rows = [
          { label: "Total Return", value: `${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct.toFixed(2)}%`, color: returnColor },
          { label: "CAGR", value: `${m.cagr_pct.toFixed(2)}%`, color: m.cagr_pct >= 15 ? "text-profit" : "text-yellow-400" },
          { label: "Max DD", value: `-${m.max_drawdown_pct.toFixed(2)}%`, color: m.max_drawdown_pct > 15 ? "text-loss" : "text-white" },
          { label: "Sharpe", value: m.sharpe_ratio.toFixed(3), color: m.sharpe_ratio >= 1.5 ? "text-profit" : "text-slate-400" },
          { label: "Win Rate", value: `${m.win_rate_pct.toFixed(1)}%`, color: m.win_rate_pct >= 55 ? "text-profit" : "text-yellow-400" },
          { label: "Trades", value: String(m.total_trades), color: "text-white" },
          { label: "Profit Factor", value: m.profit_factor >= 9999 ? "∞" : m.profit_factor.toFixed(2), color: m.profit_factor >= 1.5 ? "text-profit" : "text-yellow-400" },
          { label: "Net P&L", value: `${m.total_return_inr >= 0 ? "+" : ""}${m.total_return_inr.toFixed(1)} pts`, color: returnColor },
        ];

        return (
          <div key={name} className="rounded-xl border border-surface-border bg-surface-light/20 p-4">
            <h4 className="mb-3 text-sm font-semibold text-white">{name.replace(/_/g, " ")}</h4>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
              {rows.map((r) => (
                <div key={r.label} className="rounded-lg border border-surface-border/50 bg-surface-dark p-3">
                  <p className="text-[10px] font-medium uppercase text-slate-500">{r.label}</p>
                  <p className={`mt-1 text-sm font-bold tabular-nums ${r.color}`}>{r.value}</p>
                </div>
              ))}
            </div>

            {equity.length > 0 && (
              <div className="mt-3">
                <EquityCurveChart
                  equityCurve={equity}
                                    height={180}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function BacktestPage() {
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<ResultTab>("overview");
  const [lastConfig, setLastConfig] = useState<MultiBacktestRequest | null>(null);

  const handleRun = useCallback(async (req: MultiBacktestRequest) => {
    setIsRunning(true);
    setError(null);
    setResult(null);
    setLastConfig(req);
    setActiveTab("overview");

    try {
      const res = await apiClient.post<BacktestResult>("/backtest/run", req);
      setResult(res.data);
    } catch (err: unknown) {
      const e = err as { response?: { data?: { detail?: string; error?: { message?: string } } }; message?: string };
      const msg =
        e?.response?.data?.detail ??
        e?.response?.data?.error?.message ??
        e?.message ??
        "Backtest failed. Check the server logs.";
      setError(msg);
    } finally {
      setIsRunning(false);
    }
  }, []);

  const tabs: { id: ResultTab; label: string; icon: (props: { className?: string }) => React.ReactNode }[] = [
    { id: "overview", label: "Overview", icon: TrendingUp },
    { id: "equity", label: "Equity Curve", icon: BarChart2 },
    { id: "heatmap", label: "Monthly P&L", icon: Calendar },
    { id: "trades", label: "Trade Log", icon: List },
    { id: "strategies", label: "Per Strategy", icon: Layers },
  ];

  const strategyNames = result
    ? [...new Set(result.trades.map((t) => t.strategy_name))]
    : [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-bold text-white">Backtesting</h1>
        <p className="text-sm text-slate-400">
          Test strategies against historical data (2021–2026, 1-min OHLCV)
        </p>
      </div>

      {/* Config Panel */}
      <BacktestConfigPanel onRun={handleRun} isRunning={isRunning} />

      {/* Error */}
      {error && (
        <div className="flex items-start gap-3 rounded-xl border border-loss/40 bg-loss/10 p-4 text-sm text-loss">
          <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
          <div>
            <p className="font-semibold">Backtest Error</p>
            <p className="mt-0.5 text-loss/80">{error}</p>
          </div>
        </div>
      )}

      {/* Results */}
      {result && (
        <div className="space-y-4">
          {/* Summary bar */}
          <div className="flex flex-wrap items-center gap-4 rounded-xl border border-surface-border bg-surface-light/20 px-4 py-3">
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span className="text-slate-500">Instrument:</span>
              <span className="font-medium text-white">
                {lastConfig?.instrument?.replace("_", " ")}
              </span>
            </div>
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span className="text-slate-500">Period:</span>
              <span className="font-medium text-white">
                {lastConfig?.start_date} → {lastConfig?.end_date}
              </span>
            </div>
            <div className="flex items-center gap-2 text-xs text-slate-400">
              <span className="text-slate-500">Strategies:</span>
              <span className="font-medium text-white">
                {lastConfig?.strategies.map((s) => s.name.replace(/_/g, " ")).join(", ")}
              </span>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="text-slate-500">Total P&L:</span>
              <span
                className={`font-bold tabular-nums ${
                  result.metrics.total_return_inr >= 0 ? "text-profit" : "text-loss"
                }`}
              >
                {result.metrics.total_return_inr >= 0 ? "+" : ""}{result.metrics.total_return_inr.toFixed(2)} pts
              </span>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="text-slate-500">Trades:</span>
              <span className="font-bold tabular-nums text-white">
                {result.metrics.total_trades}
              </span>
            </div>
            <div className="flex items-center gap-2 text-xs">
              <span className="text-slate-500">Win Rate:</span>
              <span
                className={`font-bold tabular-nums ${
                  result.metrics.win_rate_pct >= 50 ? "text-profit" : "text-loss"
                }`}
              >
                {result.metrics.win_rate_pct.toFixed(1)}%
              </span>
            </div>
          </div>

          {/* Tab nav */}
          <div className="flex gap-1 overflow-x-auto border-b border-surface-border pb-0">
            {tabs.map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`flex items-center gap-1.5 whitespace-nowrap px-4 py-2.5 text-sm font-medium transition-colors border-b-2 -mb-px ${
                    activeTab === tab.id
                      ? "border-accent-light text-accent-light"
                      : "border-transparent text-slate-400 hover:text-white"
                  }`}
                >
                  <Icon className="h-3.5 w-3.5" />
                  {tab.label}
                </button>
              );
            })}
          </div>

          {/* Tab content */}
          <div>
            {activeTab === "overview" && (
              <div className="space-y-5">
                <MetricsGrid metrics={result.metrics} />
                <div className="rounded-2xl border border-surface-border bg-surface-dark p-4">
                  <h3 className="mb-3 text-sm font-semibold text-white">Portfolio Equity Curve</h3>
                  <EquityCurveChart
                    equityCurve={result.equity_curve}
                    perStrategyEquity={result.per_strategy_equity}
                                        height={320}
                  />
                </div>
              </div>
            )}

            {activeTab === "equity" && (
              <div className="rounded-2xl border border-surface-border bg-surface-dark p-4">
                <h3 className="mb-3 text-sm font-semibold text-white">Portfolio Equity Curve</h3>
                <EquityCurveChart
                  equityCurve={result.equity_curve}
                  perStrategyEquity={result.per_strategy_equity}
                                    height={480}
                />
              </div>
            )}

            {activeTab === "heatmap" && (
              <div className="rounded-2xl border border-surface-border bg-surface-dark p-4">
                <h3 className="mb-3 text-sm font-semibold text-white">Monthly P&L Heatmap</h3>
                <MonthlyHeatmap data={result.monthly_pnl} />
              </div>
            )}

            {activeTab === "trades" && (
              <div className="rounded-2xl border border-surface-border bg-surface-dark p-4">
                <h3 className="mb-3 text-sm font-semibold text-white">
                  Trade Log ({result.trades.length} trades)
                </h3>
                <TradeLog trades={result.trades} strategyNames={strategyNames} />
              </div>
            )}

            {activeTab === "strategies" && (
              <div className="space-y-4">
                <h3 className="text-sm font-semibold text-white">Per-Strategy Breakdown</h3>
                <StrategyBreakdown result={result} />
              </div>
            )}
          </div>
        </div>
      )}

      {/* Empty state */}
      {!result && !isRunning && !error && (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-surface-border py-16 text-center">
          <BarChart2 className="mb-3 h-10 w-10 text-slate-600" />
          <p className="text-sm font-medium text-slate-400">No results yet</p>
          <p className="mt-1 text-xs text-slate-600">
            Configure strategies above and click Run Backtest
          </p>
        </div>
      )}
    </div>
  );
}
