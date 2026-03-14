import { create } from "zustand";
import type { Position, Signal, Tick } from "@/types/trading";
import type { CircuitBreakerState } from "@/types/discipline";

interface LiveDataState {
  positions: Position[];
  signals: Signal[];
  ticks: Record<string, Tick>;
  circuitBreaker: CircuitBreakerState | null;
  disciplineScore: number;
  workerStatus: "RUNNING" | "STOPPED" | "ERROR";

  setPositions: (positions: Position[]) => void;
  updatePosition: (position: Position) => void;
  removePosition: (positionId: string) => void;

  addSignal: (signal: Signal) => void;
  setSignals: (signals: Signal[]) => void;

  updateTick: (tick: Tick) => void;

  setCircuitBreaker: (state: CircuitBreakerState) => void;
  setDisciplineScore: (score: number) => void;
  setWorkerStatus: (status: "RUNNING" | "STOPPED" | "ERROR") => void;
}

export const useLiveDataStore = create<LiveDataState>((set) => ({
  positions: [],
  signals: [],
  ticks: {},
  circuitBreaker: null,
  disciplineScore: 0,
  workerStatus: "STOPPED",

  setPositions: (positions) => set({ positions }),

  updatePosition: (position) =>
    set((state) => {
      const index = state.positions.findIndex((p) => p.id === position.id);
      if (index === -1) {
        return { positions: [...state.positions, position] };
      }
      const updated = [...state.positions];
      updated[index] = position;
      return { positions: updated };
    }),

  removePosition: (positionId) =>
    set((state) => ({
      positions: state.positions.filter((p) => p.id !== positionId),
    })),

  addSignal: (signal) =>
    set((state) => ({
      signals: [signal, ...state.signals].slice(0, 100),
    })),

  setSignals: (signals) => set({ signals }),

  updateTick: (tick) =>
    set((state) => ({
      ticks: { ...state.ticks, [tick.symbol]: tick },
    })),

  setCircuitBreaker: (circuitBreaker) => set({ circuitBreaker }),
  setDisciplineScore: (disciplineScore) => set({ disciplineScore }),
  setWorkerStatus: (workerStatus) => set({ workerStatus }),
}));
