"use client";

import { useEffect, useRef } from "react";
import { wsClient, type WSEventType } from "@/lib/websocket";
import { useLiveDataStore } from "@/stores/liveDataStore";
import { useUIStore } from "@/stores/uiStore";
import type { Position, Signal } from "@/types/trading";

export function useWebSocket() {
  const connectedRef = useRef(false);

  const updatePosition = useLiveDataStore((s) => s.updatePosition);
  const addSignal = useLiveDataStore((s) => s.addSignal);
  const updateTick = useLiveDataStore((s) => s.updateTick);
  const setDisciplineScore = useLiveDataStore((s) => s.setDisciplineScore);
  const setWorkerStatus = useLiveDataStore((s) => s.setWorkerStatus);
  const addNotification = useUIStore((s) => s.addNotification);

  useEffect(() => {
    if (connectedRef.current) return;
    connectedRef.current = true;

    wsClient.connect();

    const unsubTick = wsClient.on("TICK", (data) => {
      updateTick({
        symbol: data.symbol,
        last_price: data.last_price,
        change_pct: data.change_pct,
        timestamp: new Date().toISOString(),
      });
    });

    const unsubSignal = wsClient.on("SIGNAL", (data: Signal) => {
      addSignal(data);
      addNotification({
        type: "info",
        title: "New Signal",
        message: `${data.direction} ${data.underlying} (${data.strategy_name})`,
      });
    });

    const unsubPosition = wsClient.on("POSITION_UPDATE", (data: Position) => {
      updatePosition(data);
    });

    const unsubFill = wsClient.on("FILL", (data) => {
      addNotification({
        type: "success",
        title: "Order Filled",
        message: `Order ${data.order_id} filled at ${data.fill_price}`,
      });
    });

    const unsubCircuit = wsClient.on("CIRCUIT_BREAKER", (data) => {
      if (data.status === "HALTED") {
        addNotification({
          type: "error",
          title: "Circuit Breaker HALTED",
          message: data.reason,
        });
      }
    });

    const unsubDiscipline = wsClient.on("DISCIPLINE_SCORE_UPDATE", (data) => {
      setDisciplineScore(data.score);
    });

    const unsubWorker = wsClient.on("WORKER_STATUS", (data) => {
      setWorkerStatus(data.status);
    });

    const unsubAlert = wsClient.on("ALERT", (data) => {
      addNotification({
        type: data.severity === "error" ? "error" : data.severity === "warning" ? "warning" : "info",
        title: "Alert",
        message: data.message,
      });
    });

    return () => {
      unsubTick();
      unsubSignal();
      unsubPosition();
      unsubFill();
      unsubCircuit();
      unsubDiscipline();
      unsubWorker();
      unsubAlert();
      wsClient.disconnect();
      connectedRef.current = false;
    };
  }, [updatePosition, addSignal, updateTick, setDisciplineScore, setWorkerStatus, addNotification]);
}

export function useWSEvent<T extends WSEventType>(
  eventType: T,
  handler: (data: Extract<import("@/lib/websocket").WSEvent, { type: T }>["data"]) => void
) {
  useEffect(() => {
    const unsub = wsClient.on(eventType, handler);
    return unsub;
  }, [eventType, handler]);
}
