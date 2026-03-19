"use client";

import { MonthlyPnlPoint } from "@/types/backtest";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

interface MonthlyHeatmapProps {
  data: MonthlyPnlPoint[];
}

function cellColor(pnl: number, maxAbs: number): string {
  if (pnl === 0 || maxAbs === 0) return "bg-surface-light text-slate-400";
  const intensity = Math.min(Math.abs(pnl) / maxAbs, 1);
  if (pnl > 0) {
    if (intensity > 0.7) return "bg-emerald-600 text-white";
    if (intensity > 0.4) return "bg-emerald-700/80 text-white";
    return "bg-emerald-900/60 text-emerald-300";
  } else {
    if (intensity > 0.7) return "bg-red-600 text-white";
    if (intensity > 0.4) return "bg-red-700/80 text-white";
    return "bg-red-900/60 text-red-300";
  }
}

function fmtPts(v: number): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)} pts`;
}

export function MonthlyHeatmap({ data }: MonthlyHeatmapProps) {
  const byYear: Record<string, Record<number, number>> = {};
  for (const point of data) {
    const parts = point.month.split("-");
    const year = parts[0] ?? "";
    const month = parseInt(parts[1] ?? "0", 10);
    if (!byYear[year]) byYear[year] = {};
    byYear[year][month] = (byYear[year][month] ?? 0) + point.pnl;
  }

  const years = Object.keys(byYear).sort();
  if (years.length === 0) return null;

  const yearTotals: Record<string, number> = {};
  for (const year of years) {
    yearTotals[year] = Object.values(byYear[year] ?? {}).reduce((a, b) => a + b, 0);
  }

  const allPnls = data.map((d) => Math.abs(d.pnl));
  const maxAbs = Math.max(...allPnls, 1);

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr>
            <th className="px-3 py-2 text-left text-slate-500 font-medium w-16">Year</th>
            {MONTHS.map((m) => (
              <th key={m} className="px-1 py-2 text-center text-slate-500 font-medium min-w-[60px]">
                {m}
              </th>
            ))}
            <th className="px-3 py-2 text-center text-slate-500 font-medium min-w-[80px]">Total</th>
          </tr>
        </thead>
        <tbody>
          {years.map((year) => {
            const yearTotal = yearTotals[year] ?? 0;
            return (
              <tr key={year}>
                <td className="px-3 py-1 text-slate-400 font-medium">{year}</td>
                {MONTHS.map((_, mIdx) => {
                  const month = mIdx + 1;
                  const pnl = byYear[year]?.[month];
                  if (pnl === undefined) {
                    return (
                      <td key={month} className="px-1 py-1">
                        <div className="rounded px-1 py-2 text-center text-slate-600 bg-surface-light/30">
                          —
                        </div>
                      </td>
                    );
                  }
                  return (
                    <td key={month} className="px-1 py-1">
                      <div
                        className={`rounded px-1 py-1.5 text-center font-medium tabular-nums cursor-default transition-opacity hover:opacity-80 ${cellColor(pnl, maxAbs)}`}
                        title={`${year}-${String(month).padStart(2, "0")}: ${fmtPts(pnl)}`}
                      >
                        {fmtPts(pnl)}
                      </div>
                    </td>
                  );
                })}
                <td className="px-3 py-1">
                  <div
                    className={`rounded px-2 py-1.5 text-center font-bold tabular-nums ${
                      yearTotal >= 0 ? "text-profit" : "text-loss"
                    }`}
                  >
                    {fmtPts(yearTotal)}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-slate-600">Values in index points. Hover for exact amount.</p>
    </div>
  );
}
