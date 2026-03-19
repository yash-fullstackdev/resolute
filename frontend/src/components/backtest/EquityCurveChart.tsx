"use client";

import {
  ResponsiveContainer,
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ReferenceLine,
} from "recharts";
import { EquityPoint } from "@/types/backtest";

const STRATEGY_COLORS = [
  "#6366f1", "#10b981", "#f59e0b", "#ec4899", "#3b82f6", "#8b5cf6",
];

interface EquityCurveChartProps {
  equityCurve: EquityPoint[];
  perStrategyEquity?: Record<string, EquityPoint[]>;
  height?: number;
}

interface TooltipPayloadItem {
  dataKey: string;
  value: number;
  color: string;
  name: string;
}

function fmtPts(v: number): string {
  return `${v >= 0 ? "+" : ""}${v.toFixed(1)} pts`;
}

function CustomTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: TooltipPayloadItem[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-surface-border bg-surface-dark p-3 shadow-xl min-w-[180px]">
      <p className="text-xs text-slate-400 mb-2">{label}</p>
      {payload.map((p) => (
        <div key={p.dataKey} className="flex items-center justify-between gap-4 text-xs">
          <span style={{ color: p.color }}>{p.name}</span>
          <span className={`font-bold tabular-nums ${p.value >= 0 ? "text-profit" : "text-loss"}`}>
            {fmtPts(p.value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function sampleArray<T>(arr: T[], maxPoints: number): T[] {
  if (arr.length <= maxPoints) return arr;
  const step = Math.ceil(arr.length / maxPoints);
  return arr.filter((_, i) => i % step === 0 || i === arr.length - 1);
}

export function EquityCurveChart({
  equityCurve,
  perStrategyEquity,
  height = 360,
}: EquityCurveChartProps) {
  const strategyNames = perStrategyEquity ? Object.keys(perStrategyEquity) : [];
  const showPerStrategy = strategyNames.length > 1;

  // Get baseline equity (first point) to compute cumulative P&L in points
  const baseline = equityCurve[0]?.equity ?? 0;
  // Determine lot_size from equity scale: if values are in 100k+ range, it's INR
  // We derive lot_size by comparing equity changes to expected point ranges
  // For now, use the equity values directly as "cumulative P&L" relative to baseline
  const sampled = sampleArray(equityCurve, 600);
  const chartData = sampled.map((point) => {
    const cumPnl = point.equity - baseline;
    const row: Record<string, number | string> = {
      date: point.date.slice(0, 10),
      combined: Math.round(cumPnl * 100) / 100,
    };
    if (showPerStrategy) {
      for (const name of strategyNames) {
        const sEq = perStrategyEquity![name];
        if (sEq) {
          const sBaseline = sEq[0]?.equity ?? 0;
          const closest = sEq.find((p) => p.timestamp >= point.timestamp) ?? sEq.at(-1);
          if (closest) row[name] = Math.round((closest.equity - sBaseline) * 100) / 100;
        }
      }
    }
    return row;
  });

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#2d2d3e" />
          <XAxis
            dataKey="date"
            tick={{ fill: "#64748b", fontSize: 11 }}
            axisLine={{ stroke: "#2d2d3e" }}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fill: "#64748b", fontSize: 11 }}
            axisLine={{ stroke: "#2d2d3e" }}
            tickFormatter={(v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(0)}`}
            width={65}
            label={{ value: "Cumulative P&L (pts)", angle: -90, position: "insideLeft", style: { fill: "#64748b", fontSize: 10 }, offset: -5 }}
          />
          <Tooltip content={<CustomTooltip />} />
          {showPerStrategy && <Legend wrapperStyle={{ fontSize: 12, color: "#94a3b8" }} />}
          <ReferenceLine y={0} stroke="#374151" strokeDasharray="4 4" />

          <defs>
            <linearGradient id="eqGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#6366f1" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#6366f1" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="combined"
            name="Cumulative P&L"
            stroke="#6366f1"
            strokeWidth={2}
            fill="url(#eqGradient)"
            dot={false}
            activeDot={{ r: 4, fill: "#6366f1" }}
          />

          {showPerStrategy &&
            strategyNames.map((name, idx) => (
              <Line
                key={name}
                type="monotone"
                dataKey={name}
                stroke={STRATEGY_COLORS[(idx + 1) % STRATEGY_COLORS.length]}
                strokeWidth={1.5}
                dot={false}
                strokeDasharray="4 2"
                activeDot={{ r: 3 }}
              />
            ))}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
