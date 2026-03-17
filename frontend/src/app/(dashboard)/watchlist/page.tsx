"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import { UNDERLYINGS } from "@/lib/constants";
import { formatINR } from "@/lib/formatters";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { useWebSocket } from "@/hooks/useWebSocket";
import type { ApiResponse } from "@/types/api";
import { Plus, Trash2, X, Eye } from "lucide-react";

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

  const { data: watchlists, isLoading } = useQuery<Watchlist[]>({
    queryKey: ["watchlists"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<Watchlist[]>>("/watchlists");
      return res.data.data;
    },
  });

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
        <div className="space-y-4">
          {watchlists.map((wl) => (
            <div key={wl.id} className="rounded-xl border border-surface-border bg-surface p-5">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold text-white">{wl.name}</h2>
                <button
                  onClick={() => deleteMutation.mutate(wl.id)}
                  disabled={deleteMutation.isPending}
                  className="rounded-md p-1.5 text-slate-400 transition-colors hover:bg-loss/10 hover:text-loss"
                  title="Delete watchlist"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>

              {/* Current symbols */}
              {wl.symbols.length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {wl.symbols.map((sym) => {
                    const tick = ticks[sym];
                    return (
                      <span
                        key={sym}
                        className="flex items-center gap-1.5 rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-sm font-medium text-accent-light"
                      >
                        {sym}
                        {tick && (
                          <span className="ml-1 flex items-center gap-1 text-xs">
                            <span className="text-white">{formatINR(tick.last_price)}</span>
                            <span className={tick.change_pct >= 0 ? "text-profit" : "text-loss"}>
                              {tick.change_pct >= 0 ? "+" : ""}{tick.change_pct.toFixed(2)}%
                            </span>
                          </span>
                        )}
                        <button
                          onClick={() => removeSymbol(wl, sym)}
                          className="rounded-full p-0.5 transition-colors hover:bg-loss/20 hover:text-loss"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </span>
                    );
                  })}
                </div>
              ) : (
                <p className="mt-3 text-sm text-slate-500">No symbols added yet</p>
              )}

              {/* Add symbols */}
              <div className="mt-4 border-t border-surface-border pt-3">
                <p className="mb-2 text-xs text-slate-500">Add symbols:</p>
                <div className="flex flex-wrap gap-1.5">
                  {UNDERLYINGS.filter((s) => !wl.symbols.includes(s)).map((sym) => (
                    <button
                      key={sym}
                      onClick={() => addSymbol(wl, sym)}
                      className="rounded-md border border-surface-border px-2.5 py-1 text-xs text-slate-400 transition-colors hover:border-accent/50 hover:bg-accent/10 hover:text-white"
                    >
                      + {sym}
                    </button>
                  ))}
                  {UNDERLYINGS.filter((s) => !wl.symbols.includes(s)).length === 0 && (
                    <span className="text-xs text-slate-500">All symbols added</span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
