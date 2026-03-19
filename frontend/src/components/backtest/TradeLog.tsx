"use client";

import { useState, useMemo } from "react";
import { TradeRecord } from "@/types/backtest";
import { ChevronUp, ChevronDown, ChevronsUpDown } from "lucide-react";

const EXIT_REASON_LABELS: Record<string, string> = {
  stop_loss: "SL_HIT",
  target: "TP_HIT",
  square_off: "SQUARE_OFF",
  time_stop: "TIME_EXIT",
  end_of_backtest: "EOD_CLOSE",
  drawdown_kill: "DD_KILL",
  signal: "SIGNAL",
};

type SortKey = "date" | "strategy_name" | "direction_label" | "entry_price" | "exit_price" | "pnl_pts" | "exit_reason";
type SortDir = "asc" | "desc";

interface TradeLogProps {
  trades: TradeRecord[];
  strategyNames: string[];
  instrument?: string;
}

function formatPts(v: number): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
}

function shortInstrument(name: string): string {
  if (name.includes("BANK")) return "BNIFTY";
  if (name.includes("NIFTY")) return "NIFTY";
  return name.slice(0, 8);
}

export function TradeLog({ trades, strategyNames, instrument }: TradeLogProps) {
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [filterStrategy, setFilterStrategy] = useState<string>("all");
  const [filterDir, setFilterDir] = useState<string>("all");
  const [page, setPage] = useState(0);
  const PAGE_SIZE = 50;

  function handleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
    setPage(0);
  }

  // Compute SL/TP/RR/hold for each trade
  const enrichedTrades = useMemo(() => {
    return trades.map((t) => {
      const sl_pts = t.sl_pts ?? (t.stop_loss
        ? (t.direction === 1 ? t.entry_price - t.stop_loss : t.stop_loss - t.entry_price)
        : 0);
      const tp_pts = t.tp_pts ?? (t.target
        ? (t.direction === 1 ? t.target - t.entry_price : t.entry_price - t.target)
        : 0);
      const rr = t.rr_ratio ?? (sl_pts > 0 ? (tp_pts / sl_pts).toFixed(1) : "N/A");
      const pnl_pts = t.pnl_pts ?? (
        t.direction === 1
          ? t.exit_price - t.entry_price
          : t.entry_price - t.exit_price
      );
      const hold = t.hold_candles ?? 0;
      return { ...t, sl_pts, tp_pts, rr_ratio: rr, pnl_pts, hold_candles: hold };
    });
  }, [trades]);

  const filtered = useMemo(() => {
    let result = [...enrichedTrades];
    if (filterStrategy !== "all") result = result.filter((t) => t.strategy_name === filterStrategy);
    if (filterDir !== "all") result = result.filter((t) => t.direction_label === filterDir);
    result.sort((a, b) => {
      const av = a[sortKey as keyof typeof a];
      const bv = b[sortKey as keyof typeof b];
      if (typeof av === "number" && typeof bv === "number") {
        return sortDir === "asc" ? av - bv : bv - av;
      }
      const as = String(av ?? "");
      const bs = String(bv ?? "");
      return sortDir === "asc" ? as.localeCompare(bs) : bs.localeCompare(as);
    });
    return result;
  }, [enrichedTrades, filterStrategy, filterDir, sortKey, sortDir]);

  const paginated = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);

  const SortIcon = ({ k }: { k: SortKey }) => {
    if (sortKey !== k) return <ChevronsUpDown className="h-3 w-3 inline ml-1 opacity-40" />;
    return sortDir === "asc"
      ? <ChevronUp className="h-3 w-3 inline ml-1 text-accent-light" />
      : <ChevronDown className="h-3 w-3 inline ml-1 text-accent-light" />;
  };

  const winners = trades.filter((t) => t.pnl > 0).length;
  const losers = trades.filter((t) => t.pnl < 0).length;
  const totalPnlPts = enrichedTrades.reduce((s, t) => s + (t.pnl_pts ?? 0), 0);
  const instLabel = instrument ? shortInstrument(instrument) : "";

  return (
    <div className="space-y-3">
      {/* Filters + Summary */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <span className="text-profit">{winners} wins</span>
          <span className="text-slate-600">|</span>
          <span className="text-loss">{losers} losses</span>
          <span className="text-slate-600">|</span>
          <span>{filtered.length} trades</span>
          <span className="text-slate-600">|</span>
          <span className={totalPnlPts >= 0 ? "text-profit" : "text-loss"}>
            Total: {formatPts(totalPnlPts)} pts
          </span>
        </div>

        {strategyNames.length > 1 && (
          <select
            value={filterStrategy}
            onChange={(e) => { setFilterStrategy(e.target.value); setPage(0); }}
            className="rounded border border-surface-border bg-surface-light px-2 py-1 text-xs text-white"
          >
            <option value="all">All strategies</option>
            {strategyNames.map((n) => (
              <option key={n} value={n}>{n.replace(/_/g, " ")}</option>
            ))}
          </select>
        )}

        <select
          value={filterDir}
          onChange={(e) => { setFilterDir(e.target.value); setPage(0); }}
          className="rounded border border-surface-border bg-surface-light px-2 py-1 text-xs text-white"
        >
          <option value="all">All directions</option>
          <option value="BUY">BUY only</option>
          <option value="SELL">SELL only</option>
        </select>
      </div>

      {/* Table — matches backtest_ref.py format */}
      <div className="overflow-x-auto rounded-xl border border-surface-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-surface-border bg-surface-light/50">
              <th className="px-2 py-2.5 text-right font-medium text-slate-400 w-8">#</th>
              {([
                ["date", "Date"],
                ["", "Time"],
                ...(instLabel ? [] : [["", "Inst"]]) as [string, string][],
                ["direction_label", "Dir"],
                ["strategy_name", "Strategy"],
                ["entry_price", "Entry"],
                ["exit_price", "Exit"],
                ["", "SL"],
                ["", "TP"],
                ["", "RR"],
                ["pnl_pts", "P&L (pts)"],
                ["exit_reason", "Reason"],
                ["", "Hold"],
              ] as [SortKey | "", string][]).map(([key, label], ci) => (
                <th
                  key={ci}
                  onClick={() => key ? handleSort(key as SortKey) : undefined}
                  className={`px-2 py-2.5 text-left font-medium text-slate-400 ${key ? "cursor-pointer hover:text-white" : ""} transition-colors whitespace-nowrap`}
                >
                  {label}
                  {key && <SortIcon k={key as SortKey} />}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {paginated.map((trade, idx) => (
              <tr
                key={idx}
                className="border-b border-surface-border/40 hover:bg-surface-light/30 transition-colors"
              >
                <td className="px-2 py-1.5 text-right text-slate-600 tabular-nums">
                  {page * PAGE_SIZE + idx + 1}
                </td>
                <td className="px-2 py-1.5 text-slate-400 whitespace-nowrap tabular-nums">
                  {trade.date}
                </td>
                <td className="px-2 py-1.5 text-slate-500 tabular-nums">
                  {trade.time || "—"}
                </td>
                {!instLabel && (
                  <td className="px-2 py-1.5 text-slate-400 text-[10px]">
                    {shortInstrument(trade.strategy_name)}
                  </td>
                )}
                <td className="px-2 py-1.5">
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
                      trade.direction_label === "BUY"
                        ? "bg-profit/15 text-profit"
                        : "bg-loss/15 text-loss"
                    }`}
                  >
                    {trade.direction_label}
                  </span>
                </td>
                <td className="px-2 py-1.5 text-slate-300 text-[11px]">
                  {trade.strategy_name.replace(/_/g, " ")}
                </td>
                <td className="px-2 py-1.5 tabular-nums text-slate-300">
                  {trade.entry_price.toFixed(2)}
                </td>
                <td className="px-2 py-1.5 tabular-nums text-slate-300">
                  {trade.exit_price.toFixed(2)}
                </td>
                <td className="px-2 py-1.5 tabular-nums text-slate-500">
                  {trade.sl_pts > 0 ? trade.sl_pts.toFixed(1) : "—"}
                </td>
                <td className="px-2 py-1.5 tabular-nums text-slate-500">
                  {trade.tp_pts > 0 ? trade.tp_pts.toFixed(1) : "—"}
                </td>
                <td className="px-2 py-1.5 tabular-nums text-slate-500">
                  {trade.rr_ratio}
                </td>
                <td
                  className={`px-2 py-1.5 font-bold tabular-nums ${
                    (trade.pnl_pts ?? 0) >= 0 ? "text-profit" : "text-loss"
                  }`}
                >
                  {formatPts(trade.pnl_pts ?? 0)}
                </td>
                <td className="px-2 py-1.5 text-slate-500 text-[10px]">
                  {EXIT_REASON_LABELS[trade.exit_reason] ?? trade.exit_reason}
                </td>
                <td className="px-2 py-1.5 tabular-nums text-slate-500">
                  {trade.hold_candles ? `${trade.hold_candles}m` : "—"}
                </td>
              </tr>
            ))}
            {paginated.length === 0 && (
              <tr>
                <td colSpan={14} className="px-3 py-8 text-center text-slate-500">
                  No trades match the current filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-xs text-slate-400">
          <span>
            Page {page + 1} of {totalPages} ({filtered.length} trades)
          </span>
          <div className="flex gap-2">
            <button
              disabled={page === 0}
              onClick={() => setPage((p) => p - 1)}
              className="rounded border border-surface-border px-3 py-1 hover:bg-surface-light disabled:opacity-30 disabled:cursor-not-allowed"
            >
              Prev
            </button>
            <button
              disabled={page >= totalPages - 1}
              onClick={() => setPage((p) => p + 1)}
              className="rounded border border-surface-border px-3 py-1 hover:bg-surface-light disabled:opacity-30 disabled:cursor-not-allowed"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
