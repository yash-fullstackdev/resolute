"use client";

import type { Signal } from "@/types/trading";
import { formatTimeIST } from "@/lib/formatters";
import { TrendingUp, TrendingDown, Zap, FileText } from "lucide-react";

interface SignalCardProps {
  signal: Signal;
}

function fmt(n: number | null | undefined): string {
  if (n == null) return "--";
  return new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(n);
}

const STRATEGY_DISPLAY: Record<string, string> = {
  ttm_squeeze: "TTM Squeeze",
  supertrend_strategy: "Supertrend",
  vwap_supertrend: "VWAP + Supertrend",
  ema_breakdown: "EMA Breakdown",
  rsi_vwap_scalp: "RSI VWAP Scalp",
  ema33_ob: "EMA 33 Pullback",
  smc_order_block: "SMC Order Block",
};

export function SignalCard({ signal }: SignalCardProps) {
  const isBuy = signal.direction === "BULLISH" || signal.direction === "BUY" || signal.direction === "BUY_CALL";
  const dirColor = isBuy ? "text-profit" : "text-loss";
  const dirBg = isBuy ? "bg-profit/10" : "bg-loss/10";
  const dirLabel = isBuy ? "BUY" : "SELL";
  const opts = signal.options;
  const hasOptions = signal.has_options_chain && opts;
  const meta = signal.metadata ?? {};
  const instanceName = (meta.instance_name as string) ?? "";
  const tradingMode = (meta.trading_mode as string) ?? "";
  const biasDir = (meta.bias_direction as string) ?? "";

  return (
    <div className={`rounded-xl border bg-surface p-4 transition-colors ${
      isBuy ? "border-profit/20" : "border-loss/20"
    }`}>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-wrap">
          {isBuy
            ? <TrendingUp className="h-4 w-4 text-profit" />
            : <TrendingDown className="h-4 w-4 text-loss" />}
          <span className={`text-sm font-bold ${dirColor}`}>{dirLabel}</span>
          <span className="text-sm font-semibold text-white">{signal.underlying}</span>
          {tradingMode === "paper" && (
            <span className="inline-flex items-center gap-0.5 rounded-full bg-amber-400/15 px-2 py-0.5 text-[10px] font-bold text-amber-400">
              <FileText className="h-2.5 w-2.5" /> PAPER
            </span>
          )}
          {tradingMode === "live" && (
            <span className="inline-flex items-center gap-0.5 rounded-full bg-profit/15 px-2 py-0.5 text-[10px] font-bold text-profit">
              <Zap className="h-2.5 w-2.5" /> LIVE
            </span>
          )}
        </div>
        <span className="text-[11px] text-slate-500 tabular-nums">{formatTimeIST(signal.created_at)}</span>
      </div>

      {/* Strategy + Instance */}
      <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded bg-surface-light px-2 py-0.5 text-slate-400">
          {STRATEGY_DISPLAY[signal.strategy_name] ?? signal.strategy_name}
        </span>
        {instanceName && instanceName !== signal.strategy_name && (
          <span className="text-slate-600">{instanceName}</span>
        )}
        {biasDir && (
          <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
            biasDir === "BUY" ? "bg-profit/10 text-profit" : "bg-loss/10 text-loss"
          }`}>
            Bias: {biasDir}
          </span>
        )}
      </div>

      {/* Live P&L + Trade Status */}
      {signal.current_price != null && signal.entry_price != null && (
        <div className="mt-2 flex items-center justify-between rounded-lg bg-surface-dark/50 px-3 py-2">
          <div className="flex items-center gap-3">
            <div>
              <p className="text-[9px] text-slate-600 uppercase">Now</p>
              <p className="text-sm font-bold text-white tabular-nums">{fmt(signal.current_price)}</p>
            </div>
            <div>
              <p className="text-[9px] text-slate-600 uppercase">P&L</p>
              <p className={`text-sm font-bold tabular-nums ${(signal.live_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"}`}>
                {(signal.live_pnl ?? 0) >= 0 ? "+" : ""}{(signal.live_pnl ?? 0).toFixed(1)} pts
              </p>
            </div>
          </div>
          {signal.trade_status && signal.trade_status !== "OPEN" && (
            <span className={`rounded-md px-2.5 py-1 text-[10px] font-bold ${
              signal.trade_status === "TARGET HIT" ? "bg-profit/20 text-profit" :
              signal.trade_status === "SL HIT" ? "bg-loss/20 text-loss" :
              "bg-amber-400/20 text-amber-400"
            }`}>
              {signal.trade_status}
            </span>
          )}
          {signal.trade_status === "OPEN" && (
            <span className="inline-flex items-center gap-1 rounded-md bg-accent/10 px-2 py-1 text-[10px] font-bold text-accent-light">
              <span className="h-1.5 w-1.5 rounded-full bg-accent-light animate-pulse" />
              LIVE
            </span>
          )}
        </div>
      )}

      {/* ── Index Signal ── */}
      <div className="mt-3">
        <div className="flex items-center gap-2 mb-1.5">
          <span className="text-[10px] font-semibold text-slate-500 uppercase">Index Signal</span>
          {signal.index_rr && signal.index_rr !== "N/A" && (
            <span className="rounded bg-surface-dark px-1.5 py-0.5 text-[10px] font-medium text-slate-400">
              RR {signal.index_rr}
            </span>
          )}
        </div>
        <div className="grid grid-cols-3 gap-2 rounded-lg border border-surface-border bg-surface-dark p-2.5">
          <div className="text-center">
            <p className="text-[10px] text-slate-500">Entry</p>
            <p className="text-xs font-bold text-white tabular-nums">{fmt(signal.entry_price)}</p>
          </div>
          <div className="text-center">
            <p className="text-[10px] text-slate-500">Stop Loss</p>
            <p className="text-xs font-bold text-loss tabular-nums">{fmt(signal.stop_loss_price)}</p>
            {signal.index_risk_pts != null && signal.index_risk_pts > 0 && (
              <p className="text-[9px] text-slate-600">-{signal.index_risk_pts} pts</p>
            )}
          </div>
          <div className="text-center">
            <p className="text-[10px] text-slate-500">Target</p>
            <p className="text-xs font-bold text-profit tabular-nums">{fmt(signal.target_price)}</p>
            {signal.index_reward_pts != null && signal.index_reward_pts > 0 && (
              <p className="text-[9px] text-slate-600">+{signal.index_reward_pts} pts</p>
            )}
          </div>
        </div>
      </div>

      {/* ── Options Suggestion (only when chain available) ── */}
      {hasOptions && (
        <div className="mt-3">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-[10px] font-semibold text-slate-500 uppercase">Options Suggestion</span>
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${dirBg} ${dirColor}`}>
              {opts.option_type} {opts.strike}
            </span>
            {opts.rr && opts.rr !== "N/A" && (
              <span className="rounded bg-surface-dark px-1.5 py-0.5 text-[10px] font-medium text-slate-400">
                RR {opts.rr}
              </span>
            )}
            {opts.expiry && (
              <span className="text-[10px] text-slate-600 ml-auto">Exp: {opts.expiry}</span>
            )}
          </div>
          <div className="grid grid-cols-4 gap-2 rounded-lg border border-accent/20 bg-accent/5 p-2.5">
            <div className="text-center">
              <p className="text-[10px] text-slate-500">LTP</p>
              <p className="text-xs font-bold text-white tabular-nums">{fmt(opts.ltp)}</p>
              {opts.delta > 0 && (
                <p className="text-[9px] text-slate-600">Delta: {opts.delta}</p>
              )}
            </div>
            <div className="text-center">
              <p className="text-[10px] text-slate-500">SL</p>
              <p className="text-xs font-bold text-loss tabular-nums">{fmt(opts.sl)}</p>
              <p className="text-[9px] text-slate-600">-{fmt(opts.risk)}</p>
            </div>
            <div className="text-center">
              <p className="text-[10px] text-slate-500">Target</p>
              <p className="text-xs font-bold text-profit tabular-nums">{fmt(opts.tp)}</p>
              <p className="text-[9px] text-slate-600">+{fmt(opts.reward)}</p>
            </div>
            <div className="text-center">
              <p className="text-[10px] text-slate-500">IV</p>
              <p className="text-xs font-bold text-white tabular-nums">
                {opts.iv != null ? `${opts.iv}%` : "--"}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* No options chain note */}
      {!hasOptions && !signal.has_options_chain && (
        <p className="mt-2 text-[10px] text-slate-600">
          Price signal only — no options chain available for this instrument
        </p>
      )}

      {signal.rationale && (
        <p className="mt-2 text-[11px] text-slate-500">{signal.rationale}</p>
      )}
    </div>
  );
}
