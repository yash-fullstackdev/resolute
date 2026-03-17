"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { ApiResponse } from "@/types/api";
import { Grid3X3 } from "lucide-react";

interface ChainRow {
  strike: number;
  call_ltp: number;
  call_oi: number;
  call_iv: number;
  call_delta: number;
  call_gamma: number;
  call_theta: number;
  call_vega: number;
  put_ltp: number;
  put_oi: number;
  put_iv: number;
  put_delta: number;
  put_gamma: number;
  put_theta: number;
  put_vega: number;
}

interface RegimeData {
  regime: string;
  description: string;
}

const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY"] as const;

export default function ChainPage() {
  const [underlying, setUnderlying] = useState<string>("NIFTY");

  const {
    data: chain,
    isLoading,
    isError,
  } = useQuery<ChainRow[]>({
    queryKey: ["chain", underlying],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<ChainRow[]>>(
        `/chain/${underlying}`
      );
      return res.data.data;
    },
  });

  const { data: regime } = useQuery<RegimeData>({
    queryKey: ["regime"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<RegimeData>>("/regime");
      return res.data.data;
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Option Chain</h1>
          <p className="mt-1 text-sm text-slate-400">
            Live strikes, OI, IV, and Greeks
          </p>
        </div>
        <div className="flex items-center gap-4">
          {regime && (
            <div className="rounded-lg border border-surface-border bg-surface px-3 py-1.5 text-xs text-slate-300">
              Regime:{" "}
              <span className="font-semibold text-accent-light">
                {regime.regime}
              </span>
            </div>
          )}
          <select
            value={underlying}
            onChange={(e) => setUnderlying(e.target.value)}
            className="rounded-lg border border-surface-border bg-surface-dark px-3 py-2 text-sm text-white focus:border-accent focus:outline-none"
          >
            {UNDERLYINGS.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
        </div>
      </div>

      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : isError || !chain || chain.length === 0 ? (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <Grid3X3 className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">
              No option chain data available
            </p>
            <p className="text-xs text-slate-500">
              Chain data is populated during market hours when the signal engine is
              running
            </p>
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-surface-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-border bg-surface-dark text-slate-400">
                <th colSpan={7} className="border-r border-surface-border px-2 py-2 text-center font-semibold text-profit">
                  CALLS
                </th>
                <th className="bg-surface-dark px-3 py-2 font-semibold text-white">
                  Strike
                </th>
                <th colSpan={7} className="border-l border-surface-border px-2 py-2 text-center font-semibold text-loss">
                  PUTS
                </th>
              </tr>
              <tr className="border-b border-surface-border bg-surface-dark text-slate-500">
                <th className="px-2 py-1.5 text-right">Delta</th>
                <th className="px-2 py-1.5 text-right">Gamma</th>
                <th className="px-2 py-1.5 text-right">Theta</th>
                <th className="px-2 py-1.5 text-right">Vega</th>
                <th className="px-2 py-1.5 text-right">IV</th>
                <th className="px-2 py-1.5 text-right">OI</th>
                <th className="border-r border-surface-border px-2 py-1.5 text-right">LTP</th>
                <th className="bg-surface-dark px-3 py-1.5 text-center font-semibold text-white" />
                <th className="border-l border-surface-border px-2 py-1.5 text-right">LTP</th>
                <th className="px-2 py-1.5 text-right">OI</th>
                <th className="px-2 py-1.5 text-right">IV</th>
                <th className="px-2 py-1.5 text-right">Delta</th>
                <th className="px-2 py-1.5 text-right">Gamma</th>
                <th className="px-2 py-1.5 text-right">Theta</th>
                <th className="px-2 py-1.5 text-right">Vega</th>
              </tr>
            </thead>
            <tbody>
              {chain.map((row) => (
                <tr
                  key={row.strike}
                  className="border-b border-surface-border/50 transition-colors hover:bg-surface-light/30"
                >
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.call_delta.toFixed(2)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.call_gamma.toFixed(4)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.call_theta.toFixed(2)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.call_vega.toFixed(2)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.call_iv.toFixed(1)}%
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.call_oi.toLocaleString("en-IN")}
                  </td>
                  <td className="border-r border-surface-border px-2 py-1.5 text-right font-medium text-profit">
                    {row.call_ltp.toFixed(2)}
                  </td>
                  <td className="bg-surface-dark px-3 py-1.5 text-center font-semibold text-white">
                    {row.strike.toLocaleString("en-IN")}
                  </td>
                  <td className="border-l border-surface-border px-2 py-1.5 text-right font-medium text-loss">
                    {row.put_ltp.toFixed(2)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.put_oi.toLocaleString("en-IN")}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.put_iv.toFixed(1)}%
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.put_delta.toFixed(2)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.put_gamma.toFixed(4)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.put_theta.toFixed(2)}
                  </td>
                  <td className="px-2 py-1.5 text-right text-slate-300">
                    {row.put_vega.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
