"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { formatINR } from "@/lib/formatters";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { ApiResponse } from "@/types/api";
import { Plus, Trash2, X, Eye, ArrowLeft, TrendingUp, TrendingDown, Minus, Search, Loader2 } from "lucide-react";

interface SymbolResult {
  symbol: string;
  security_id: number;
}

function useSymbolSearch() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SymbolResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const search = useCallback((q: string) => {
    setQuery(q);
    if (timerRef.current) clearTimeout(timerRef.current);

    const trimmed = q.trim();
    if (!trimmed) {
      setResults([]);
      setIsSearching(false);
      return;
    }

    setIsSearching(true);
    timerRef.current = setTimeout(async () => {
      try {
        const res = await apiClient.get<ApiResponse<SymbolResult[]>>(
          `/symbols/search?q=${encodeURIComponent(trimmed)}&limit=15`
        );
        setResults(res.data.data);
      } catch {
        setResults([]);
      } finally {
        setIsSearching(false);
      }
    }, 300);
  }, []);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  return { query, search, results, isSearching };
}

interface Watchlist {
  id: string;
  name: string;
  symbols: string[];
  created_at: string;
  updated_at: string;
}

export default function WatchlistPage() {
  useWebSocket();
  const ticks = useLiveDataStore((s) => s.ticks);
  const queryClient = useQueryClient();
  const [newName, setNewName] = useState("");
  const [selectedWatchlist, setSelectedWatchlist] = useState<Watchlist | null>(null);
  const { query: symbolSearch, search: setSymbolSearch, results: symbolResults, isSearching } = useSymbolSearch();

  const { data: watchlists, isLoading } = useQuery<Watchlist[]>({
    queryKey: ["watchlists"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Watchlist[]>>("/watchlists");
      return res.data.data;
    },
  });

  // Keep selectedWatchlist in sync with fetched data
  const activeWatchlist = selectedWatchlist
    ? watchlists?.find((wl) => wl.id === selectedWatchlist.id) ?? selectedWatchlist
    : null;

  const createMutation = useMutation({
    mutationFn: async (name: string) => {
      await apiClient.post("/watchlists", { name, symbols: [] });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["watchlists"] });
      setNewName("");
    },
  });

  const updateMutation = useMutation({
    mutationFn: async ({ id, name, symbols }: { id: string; name: string; symbols: string[] }) => {
      await apiClient.put(`/watchlists/${id}`, { name, symbols });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["watchlists"] });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: async (id: string) => {
      await apiClient.delete(`/watchlists/${id}`);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["watchlists"] });
      setSelectedWatchlist(null);
    },
  });

  const addSymbol = (watchlist: Watchlist, symbol: string) => {
    if (!watchlist.symbols.includes(symbol)) {
      updateMutation.mutate({
        id: watchlist.id,
        name: watchlist.name,
        symbols: [...watchlist.symbols, symbol],
      });
    }
  };

  const removeSymbol = (watchlist: Watchlist, symbol: string) => {
    updateMutation.mutate({
      id: watchlist.id,
      name: watchlist.name,
      symbols: watchlist.symbols.filter((s) => s !== symbol),
    });
  };

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
      </div>
    );
  }

  // ── Detail View ──
  if (activeWatchlist) {
    return (
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <button
            onClick={() => setSelectedWatchlist(null)}
            className="rounded-lg p-2 text-slate-400 transition-colors hover:bg-surface-light hover:text-white"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <h1 className="text-2xl font-bold text-white">{activeWatchlist.name}</h1>
          <span className="rounded-full bg-surface-light px-2.5 py-0.5 text-xs text-slate-400">
            {activeWatchlist.symbols.length} symbols
          </span>
        </div>

        {/* Live Market Table */}
        {activeWatchlist.symbols.length > 0 ? (
          <div className="overflow-hidden rounded-xl border border-surface-border">
            <table className="w-full">
              <thead>
                <tr className="border-b border-surface-border bg-surface-dark">
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-slate-400">Symbol</th>
                  <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-slate-400">LTP</th>
                  <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-slate-400">Change %</th>
                  <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-slate-400">Trend</th>
                  <th className="px-4 py-3 text-center text-xs font-medium uppercase tracking-wider text-slate-400">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-border">
                {activeWatchlist.symbols.map((sym) => {
                  const tick = ticks[sym];
                  const price = tick?.last_price;
                  const changePct = tick?.change_pct ?? 0;
                  const isUp = changePct > 0;
                  const isDown = changePct < 0;

                  return (
                    <tr key={sym} className="bg-surface transition-colors hover:bg-surface-light">
                      <td className="px-4 py-4">
                        <span className="text-sm font-semibold text-white">{sym}</span>
                      </td>
                      <td className="px-4 py-4 text-right">
                        {price != null ? (
                          <span className="text-sm font-bold tabular-nums text-white">
                            {formatINR(price)}
                          </span>
                        ) : (
                          <span className="text-sm text-slate-500">--</span>
                        )}
                      </td>
                      <td className="px-4 py-4 text-right">
                        {tick ? (
                          <span
                            className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-bold tabular-nums ${
                              isUp
                                ? "bg-profit/10 text-profit"
                                : isDown
                                  ? "bg-loss/10 text-loss"
                                  : "bg-slate-700 text-slate-400"
                            }`}
                          >
                            {isUp ? "+" : ""}{changePct.toFixed(2)}%
                          </span>
                        ) : (
                          <span className="text-sm text-slate-500">--</span>
                        )}
                      </td>
                      <td className="px-4 py-4 text-right">
                        {tick ? (
                          isUp ? (
                            <TrendingUp className="ml-auto h-4 w-4 text-profit" />
                          ) : isDown ? (
                            <TrendingDown className="ml-auto h-4 w-4 text-loss" />
                          ) : (
                            <Minus className="ml-auto h-4 w-4 text-slate-500" />
                          )
                        ) : null}
                      </td>
                      <td className="px-4 py-4 text-center">
                        <button
                          onClick={() => removeSymbol(activeWatchlist, sym)}
                          className="rounded-md p-1 text-slate-500 transition-colors hover:bg-loss/10 hover:text-loss"
                          title="Remove from watchlist"
                        >
                          <X className="h-4 w-4" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="flex h-32 items-center justify-center rounded-xl border border-dashed border-surface-border">
            <p className="text-sm text-slate-500">No symbols in this watchlist</p>
          </div>
        )}

        {/* Add symbols section */}
        <div className="rounded-xl border border-surface-border bg-surface p-5">
          <p className="mb-3 text-sm font-medium text-slate-300">Add symbols</p>
          <div className="relative mb-4">
            <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
            <input
              type="text"
              value={symbolSearch}
              onChange={(e) => setSymbolSearch(e.target.value.toUpperCase())}
              placeholder="Type to search NSE symbols..."
              className="w-full rounded-lg border border-surface-border bg-surface-dark pl-10 pr-3 py-2.5 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
            />
            {isSearching && (
              <Loader2 className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-slate-500" />
            )}
          </div>
          {symbolSearch.trim() ? (
            (() => {
              const filtered = symbolResults.filter(
                (s) => !activeWatchlist.symbols.includes(s.symbol)
              );
              if (isSearching) return null;
              if (filtered.length === 0) {
                return (
                  <p className="text-sm text-slate-500">No symbols found</p>
                );
              }
              return (
                <div className="flex flex-wrap gap-1.5">
                  {filtered.map((s) => (
                    <button
                      key={s.symbol}
                      onClick={() => addSymbol(activeWatchlist, s.symbol)}
                      className="flex items-center gap-1 rounded-lg border border-surface-border px-3 py-1.5 text-sm text-slate-400 transition-colors hover:border-accent/50 hover:bg-accent/10 hover:text-white"
                    >
                      <Plus className="h-3 w-3" />
                      {s.symbol}
                    </button>
                  ))}
                </div>
              );
            })()
          ) : (
            <p className="text-sm text-slate-500">Type to search NSE symbols</p>
          )}
        </div>
      </div>
    );
  }

  // ── List View ──
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Watchlists</h1>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Watchlist name..."
            className="rounded-lg border border-surface-border bg-surface px-3 py-2 text-sm text-white placeholder-slate-500 focus:border-accent focus:outline-none"
            onKeyDown={(e) => {
              if (e.key === "Enter" && newName.trim()) {
                createMutation.mutate(newName.trim());
              }
            }}
          />
          <button
            onClick={() => createMutation.mutate(newName.trim() || "My Watchlist")}
            disabled={createMutation.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-accent/80 disabled:opacity-50"
          >
            <Plus className="h-4 w-4" />
            Add
          </button>
        </div>
      </div>

      {(!watchlists || watchlists.length === 0) ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-surface-border py-16">
          <Eye className="mb-3 h-10 w-10 text-slate-500" />
          <p className="text-sm text-slate-400">No watchlists yet</p>
          <button
            onClick={() => createMutation.mutate("My Watchlist")}
            className="mt-4 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent/80"
          >
            Create your first watchlist
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {watchlists.map((wl) => (
            <div
              key={wl.id}
              className="group cursor-pointer rounded-xl border border-surface-border bg-surface p-5 transition-all hover:border-accent/30 hover:bg-surface-light"
              onClick={() => setSelectedWatchlist(wl)}
            >
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold text-white">{wl.name}</h2>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteMutation.mutate(wl.id);
                  }}
                  disabled={deleteMutation.isPending}
                  className="rounded-md p-1.5 text-slate-500 opacity-0 transition-all hover:bg-loss/10 hover:text-loss group-hover:opacity-100"
                  title="Delete watchlist"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>

              <p className="mt-1 text-xs text-slate-500">{wl.symbols.length} symbols</p>

              {wl.symbols.length > 0 ? (
                <div className="mt-3 space-y-1.5">
                  {wl.symbols.slice(0, 5).map((sym) => {
                    const tick = ticks[sym];
                    return (
                      <div key={sym} className="flex items-center justify-between">
                        <span className="text-sm text-slate-300">{sym}</span>
                        {tick ? (
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-medium tabular-nums text-white">
                              {formatINR(tick.last_price)}
                            </span>
                            <span
                              className={`text-xs font-medium tabular-nums ${
                                tick.change_pct >= 0 ? "text-profit" : "text-loss"
                              }`}
                            >
                              {tick.change_pct >= 0 ? "+" : ""}{tick.change_pct.toFixed(2)}%
                            </span>
                          </div>
                        ) : (
                          <span className="text-xs text-slate-600">--</span>
                        )}
                      </div>
                    );
                  })}
                  {wl.symbols.length > 5 && (
                    <p className="text-xs text-slate-500">+{wl.symbols.length - 5} more</p>
                  )}
                </div>
              ) : (
                <p className="mt-3 text-sm text-slate-500">Empty watchlist</p>
              )}

              <div className="mt-3 flex items-center gap-1 text-xs text-accent opacity-0 transition-opacity group-hover:opacity-100">
                <Eye className="h-3 w-3" />
                View details
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
