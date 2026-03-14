"use client";

import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
} from "recharts";
import { formatINR } from "@/lib/formatters";

interface PnLDataPoint {
  date: string;
  pnl: number;
}

interface PnLChartProps {
  data: PnLDataPoint[];
  height?: number;
}

function CustomTooltip({ active, payload, label }: {
  active?: boolean;
  payload?: Array<{ value: number }>;
  label?: string;
}) {
  if (!active || !payload?.[0]) return null;

  const value = payload[0].value;
  return (
    <div className="rounded-lg border border-surface-border bg-surface-dark p-3 shadow-xl">
      <p className="text-xs text-slate-400">{label}</p>
      <p className={`text-sm font-bold tabular-nums ${value >= 0 ? "text-profit" : "text-loss"}`}>
        {formatINR(value, true)}
      </p>
    </div>
  );
}

export function PnLChart({ data, height = 300 }: PnLChartProps) {
  const hasProfit = data.some((d) => d.pnl > 0);
  const hasLoss = data.some((d) => d.pnl < 0);

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#3a3a4e" />
          <XAxis
            dataKey="date"
            tick={{ fill: "#94a3b8", fontSize: 12 }}
            axisLine={{ stroke: "#3a3a4e" }}
          />
          <YAxis
            tick={{ fill: "#94a3b8", fontSize: 12 }}
            axisLine={{ stroke: "#3a3a4e" }}
            tickFormatter={(val: number) => formatINR(val)}
          />
          <Tooltip content={<CustomTooltip />} />
          <ReferenceLine y={0} stroke="#64748b" strokeDasharray="3 3" />
          <defs>
            <linearGradient id="pnlGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={hasProfit ? "#10b981" : "#ef4444"} stopOpacity={0.3} />
              <stop offset="100%" stopColor={hasLoss ? "#ef4444" : "#10b981"} stopOpacity={0.05} />
            </linearGradient>
          </defs>
          <Line
            type="monotone"
            dataKey="pnl"
            stroke="#6366f1"
            strokeWidth={2}
            dot={{ r: 3, fill: "#6366f1" }}
            activeDot={{ r: 5, fill: "#818cf8" }}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
