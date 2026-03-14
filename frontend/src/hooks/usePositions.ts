"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { Position } from "@/types/trading";
import type { ApiResponse } from "@/types/api";

export function usePositions(status?: "OPEN" | "CLOSED") {
  return useQuery<Position[]>({
    queryKey: ["positions", status],
    queryFn: async () => {
      const params = status ? { status } : {};
      const response = await apiClient.get<ApiResponse<Position[]>>("/positions", { params });
      return response.data.data;
    },
    refetchInterval: 5000,
  });
}

export function useExitPosition() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (positionId: string) => {
      const response = await apiClient.post<ApiResponse<Position>>(
        `/positions/${positionId}/exit`
      );
      return response.data.data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["positions"] });
    },
  });
}
