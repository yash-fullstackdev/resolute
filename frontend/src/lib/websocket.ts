import { io, type Socket } from "socket.io-client";
import { getAccessToken } from "./auth";
import { WS_URL } from "./constants";
import type { Signal, Position } from "@/types/trading";

export interface ChainRow {
  strike: number;
  call_ltp: number;
  call_oi: number;
  call_volume: number;
  call_iv: number;
  call_delta: number;
  call_gamma: number;
  call_theta: number;
  call_vega: number;
  call_bid: number;
  call_ask: number;
  put_ltp: number;
  put_oi: number;
  put_volume: number;
  put_iv: number;
  put_delta: number;
  put_gamma: number;
  put_theta: number;
  put_vega: number;
  put_bid: number;
  put_ask: number;
}

export interface OptionChainData {
  underlying: string;
  expiry: string;
  data: ChainRow[];
  spot_price: number;
}

export type WSEvent =
  | { type: "TICK"; data: { symbol: string; last_price: number; change_pct: number } }
  | { type: "SIGNAL"; data: Signal }
  | { type: "FILL"; data: { order_id: string; fill_price: number; status: string } }
  | { type: "POSITION_UPDATE"; data: Position }
  | { type: "CIRCUIT_BREAKER"; data: { status: "HALTED" | "ACTIVE"; reason: string } }
  | { type: "PLAN_LOCKED"; data: { plan_hash: string; locked_at: string } }
  | { type: "OVERRIDE_COOLDOWN_EXPIRED"; data: { override_id: string } }
  | { type: "DISCIPLINE_SCORE_UPDATE"; data: { score: number } }
  | { type: "WORKER_STATUS"; data: { status: "RUNNING" | "STOPPED" | "ERROR" } }
  | { type: "ALERT"; data: { severity: string; message: string } }
  | { type: "OPTION_CHAIN"; data: OptionChainData };

export type WSEventType = WSEvent["type"];

export type EventHandler<T extends WSEventType> = (
  data: Extract<WSEvent, { type: T }>["data"]
) => void;

class WebSocketClient {
  private socket: Socket | null = null;
  private handlers = new Map<string, Set<(data: unknown) => void>>();
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 10;

  connect(): void {
    const token = getAccessToken();
    if (!token) return;

    this.socket = io(WS_URL, {
      auth: { token },
      transports: ["polling", "websocket"],
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 30000,
      reconnectionAttempts: this.maxReconnectAttempts,
    });

    this.socket.on("connect", () => {
      this.reconnectAttempts = 0;
    });

    this.socket.on("event", (event: WSEvent) => {
      const eventHandlers = this.handlers.get(event.type);
      if (eventHandlers) {
        for (const handler of eventHandlers) {
          handler(event.data);
        }
      }
    });

    this.socket.on("disconnect", () => {
      this.reconnectAttempts++;
    });

    this.socket.on("connect_error", () => {
      this.reconnectAttempts++;
    });
  }

  disconnect(): void {
    if (this.socket) {
      this.socket.disconnect();
      this.socket = null;
    }
    this.handlers.clear();
    this.reconnectAttempts = 0;
  }

  on<T extends WSEventType>(eventType: T, handler: EventHandler<T>): () => void {
    if (!this.handlers.has(eventType)) {
      this.handlers.set(eventType, new Set());
    }
    const handlerSet = this.handlers.get(eventType);
    const wrappedHandler = handler as (data: unknown) => void;
    handlerSet?.add(wrappedHandler);

    return () => {
      handlerSet?.delete(wrappedHandler);
    };
  }

  subscribeChain(underlying: string, expiry: string): void {
    if (this.socket?.connected) {
      this.socket.emit("subscribe_chain", { underlying, expiry });
    }
  }

  unsubscribeChain(): void {
    if (this.socket?.connected) {
      this.socket.emit("unsubscribe_chain");
    }
  }

  get isConnected(): boolean {
    return this.socket?.connected ?? false;
  }
}

export const wsClient = new WebSocketClient();
