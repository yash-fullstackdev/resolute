"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { wsClient, type ChainRow, type OptionChainData } from "@/lib/websocket";
import type { ApiResponse } from "@/types/api";
import { Grid3X3, Wifi, WifiOff } from "lucide-react";

interface ChainResponse {
  data: ChainRow[];
  spot_price: number;
  expiry: string | null;
  expiries: string[];
}

interface RegimeData {
  regime: string;
  description: string;
}

const UNDERLYINGS = [
  "NIFTY",
  "BANKNIFTY",
  "SENSEX",
  "FINNIFTY",
  "MIDCPNIFTY",
] as const;

export default function ChainPage() {
  const [underlying, setUnderlying] = useState<string>("NIFTY");
  const [selectedExpiry, setSelectedExpiry] = useState<string>("");
  const [chain, setChain] = useState<ChainRow[]>([]);
  const [spotPrice, setSpotPrice] = useState<number>(0);
  const [expiries, setExpiries] = useState<string[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const subscribedRef = useRef<string>("");

  // ── REST: initial load to get expiry list + first chain snapshot ──────────
  const { data: initData, isLoading } = useQuery<ChainResponse>({
    queryKey: ["chain-init", underlying],
    queryFn: async () => {
      const res = await apiClient.get(`/chain/${underlying}`);
      const d = res.data as Record<string, unknown>;
      return {
        data: (d.data as ChainRow[]) || [],
        spot_price: (d.spot_price as number) || 0,
        expiry: (d.expiry as string) || null,
        expiries: (d.expiries as string[]) || [],
      };
    },
    staleTime: Infinity, // WS takes over after initial load
  });

  // Sync initial REST data into state
  useEffect(() => {
    if (!initData) return;
    setChain(initData.data);
    setSpotPrice(initData.spot_price);
    setExpiries(initData.expiries);
    if (initData.expiry && !selectedExpiry) {
      setSelectedExpiry(initData.expiry);
    }
  }, [initData]); // eslint-disable-line react-hooks/exhaustive-deps

  const { data: regime } = useQuery<RegimeData>({
    queryKey: ["regime"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<RegimeData>>("/regime");
      return res.data.data;
    },
  });

  // ── WebSocket: subscribe to chain room for live updates ──────────────────

  const handleChainUpdate = useCallback(
    (data: OptionChainData) => {
      if (data.underlying === underlying && data.expiry === selectedExpiry) {
        setChain(data.data);
        setSpotPrice(data.spot_price);
      }
    },
    [underlying, selectedExpiry]
  );

  // Subscribe to WS room when underlying+expiry changes
  useEffect(() => {
    if (!selectedExpiry) return;

    const roomKey = `${underlying}:${selectedExpiry}`;
    if (subscribedRef.current === roomKey) return;

    wsClient.subscribeChain(underlying, selectedExpiry);
    subscribedRef.current = roomKey;
    setWsConnected(wsClient.isConnected);

    return () => {
      // Don't unsubscribe on every re-render — only on unmount or change
    };
  }, [underlying, selectedExpiry]);

  // Listen for OPTION_CHAIN events
  useEffect(() => {
    const unsub = wsClient.on("OPTION_CHAIN", handleChainUpdate);
    return unsub;
  }, [handleChainUpdate]);

  // Track WS connection status
  useEffect(() => {
    const interval = setInterval(() => {
      setWsConnected(wsClient.isConnected);
    }, 2000);
    return () => clearInterval(interval);
  }, []);

  // Cleanup WS subscription on unmount
  useEffect(() => {
    return () => {
      wsClient.unsubscribeChain();
      subscribedRef.current = "";
    };
  }, []);

  // When underlying changes, reset expiry and fetch new expiry list
  const handleUnderlyingChange = (newUnderlying: string) => {
    setUnderlying(newUnderlying);
    setSelectedExpiry("");
    setChain([]);
    setSpotPrice(0);
    setExpiries([]);
    subscribedRef.current = "";
  };

  // When expiry changes, fetch chain via REST for immediate data, then WS takes over
  const handleExpiryChange = async (newExpiry: string) => {
    setSelectedExpiry(newExpiry);
    subscribedRef.current = "";

    try {
      const res = await apiClient.get(`/chain/${underlying}/${newExpiry}`);
      const d = res.data as Record<string, unknown>;
      setChain((d.data as ChainRow[]) || []);
      setSpotPrice((d.spot_price as number) || 0);
    } catch {
      // WS will provide data shortly
    }
  };

  // Find ATM strike (nearest to spot)
  const atmStrike =
    spotPrice > 0 && chain.length > 0
      ? chain.reduce((prev, curr) =>
          Math.abs(curr.strike - spotPrice) < Math.abs(prev.strike - spotPrice)
            ? curr
            : prev
        ).strike
      : 0;

  // Filter to ±10 strikes around ATM
  const filteredChain = (() => {
    if (!atmStrike || chain.length === 0) return chain;
    const sorted = [...chain].sort((a, b) => a.strike - b.strike);
    const atmIdx = sorted.findIndex((r) => r.strike === atmStrike);
    if (atmIdx < 0) return chain;
    const start = Math.max(0, atmIdx - 10);
    const end = Math.min(sorted.length, atmIdx + 11);
    return sorted.slice(start, end);
  })();

  const formatNum = (n: number, decimals: number) => {
    if (n === 0) return "-";
    return n.toFixed(decimals);
  };

  const formatOI = (n: number) => {
    if (n === 0) return "-";
    return n.toLocaleString("en-IN");
  };

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-2xl font-bold text-white">Option Chain</h1>
            {wsConnected ? (
              <Wifi className="h-4 w-4 text-profit" />
            ) : (
              <WifiOff className="h-4 w-4 text-slate-500" />
            )}
          </div>
          {spotPrice > 0 && (
            <p className="mt-0.5 text-sm text-slate-400">
              Spot:{" "}
              <span className="font-semibold text-white">
                ₹
                {spotPrice.toLocaleString("en-IN", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </span>
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
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
            onChange={(e) => handleUnderlyingChange(e.target.value)}
            className="rounded-lg border border-surface-border bg-surface-dark px-3 py-2 text-sm text-white focus:border-accent focus:outline-none"
          >
            {UNDERLYINGS.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
          {expiries.length > 0 && (
            <select
              value={selectedExpiry}
              onChange={(e) => handleExpiryChange(e.target.value)}
              className="rounded-lg border border-surface-border bg-surface-dark px-3 py-2 text-sm text-white focus:border-accent focus:outline-none"
            >
              {expiries.map((exp) => (
                <option key={exp} value={exp}>
                  {exp}
                </option>
              ))}
            </select>
          )}
        </div>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : chain.length === 0 ? (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <Grid3X3 className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">
              No option chain data available
            </p>
            <p className="text-xs text-slate-500">
              Check if market is open and Dhan credentials are configured
            </p>
          </div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-surface-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-surface-border bg-surface-dark text-slate-400">
                <th
                  colSpan={8}
                  className="border-r border-surface-border px-2 py-2 text-center font-semibold text-profit"
                >
                  CALLS
                </th>
                <th className="bg-surface-dark px-3 py-2 font-semibold text-white">
                  Strike
                </th>
                <th
                  colSpan={8}
                  className="border-l border-surface-border px-2 py-2 text-center font-semibold text-loss"
                >
                  PUTS
                </th>
              </tr>
              <tr className="border-b border-surface-border bg-surface-dark text-slate-500">
                <th className="px-2 py-1.5 text-right">OI</th>
                <th className="px-2 py-1.5 text-right">Volume</th>
                <th className="px-2 py-1.5 text-right">IV</th>
                <th className="px-2 py-1.5 text-right">Delta</th>
                <th className="px-2 py-1.5 text-right">Gamma</th>
                <th className="px-2 py-1.5 text-right">Theta</th>
                <th className="px-2 py-1.5 text-right">Vega</th>
                <th className="border-r border-surface-border px-2 py-1.5 text-right">
                  LTP
                </th>
                <th className="bg-surface-dark px-3 py-1.5 text-center font-semibold text-white" />
                <th className="border-l border-surface-border px-2 py-1.5 text-right">
                  LTP
                </th>
                <th className="px-2 py-1.5 text-right">OI</th>
                <th className="px-2 py-1.5 text-right">Volume</th>
                <th className="px-2 py-1.5 text-right">IV</th>
                <th className="px-2 py-1.5 text-right">Delta</th>
                <th className="px-2 py-1.5 text-right">Gamma</th>
                <th className="px-2 py-1.5 text-right">Theta</th>
                <th className="px-2 py-1.5 text-right">Vega</th>
              </tr>
            </thead>
            <tbody>
              {filteredChain.map((row) => {
                const isATM = row.strike === atmStrike;
                const isITMCall = spotPrice > 0 && row.strike < spotPrice;
                const isITMPut = spotPrice > 0 && row.strike > spotPrice;

                return (
                  <tr
                    key={row.strike}
                    className={`border-b border-surface-border/50 transition-colors hover:bg-surface-light/30 ${
                      isATM ? "bg-accent/10 border-accent/30" : ""
                    }`}
                  >
                    {/* Call side */}
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatOI(row.call_oi)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatOI(row.call_volume)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.call_iv, 1)}
                      {row.call_iv > 0 ? "%" : ""}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.call_delta, 2)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.call_gamma, 4)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.call_theta, 2)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMCall
                          ? "bg-profit/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.call_vega, 2)}
                    </td>
                    <td
                      className={`border-r border-surface-border px-2 py-1.5 text-right font-medium text-profit ${
                        isITMCall ? "bg-profit/5" : ""
                      }`}
                    >
                      {formatNum(row.call_ltp, 2)}
                    </td>

                    {/* Strike */}
                    <td
                      className={`px-3 py-1.5 text-center font-semibold ${
                        isATM
                          ? "bg-accent/20 text-accent-light"
                          : "bg-surface-dark text-white"
                      }`}
                    >
                      {row.strike.toLocaleString("en-IN")}
                    </td>

                    {/* Put side */}
                    <td
                      className={`border-l border-surface-border px-2 py-1.5 text-right font-medium text-loss ${
                        isITMPut ? "bg-loss/5" : ""
                      }`}
                    >
                      {formatNum(row.put_ltp, 2)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatOI(row.put_oi)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatOI(row.put_volume)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.put_iv, 1)}
                      {row.put_iv > 0 ? "%" : ""}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.put_delta, 2)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.put_gamma, 4)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.put_theta, 2)}
                    </td>
                    <td
                      className={`px-2 py-1.5 text-right ${
                        isITMPut
                          ? "bg-loss/5 text-slate-400"
                          : "text-slate-300"
                      }`}
                    >
                      {formatNum(row.put_vega, 2)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
