import { io, type Socket } from "socket.io-client";
import { getAccessToken } from "./auth";
import { WS_URL } from "./constants";
import type { Signal, Position } from "@/types/trading";

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
  | { type: "ALERT"; data: { severity: string; message: string } };

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

  get isConnected(): boolean {
    return this.socket?.connected ?? false;
  }
}

export const wsClient = new WebSocketClient();
