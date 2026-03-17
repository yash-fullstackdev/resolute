"use client";

import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { ApiResponse } from "@/types/api";
import { FileText } from "lucide-react";

interface WeeklyReport {
  id: string;
  week_start: string;
  week_end: string;
  total_trades: number;
  win_rate: number;
  pnl: number;
  discipline_score: number;
  summary: string;
}

export default function ReportsPage() {
  const { data: reports, isLoading } = useQuery<WeeklyReport[]>({
    queryKey: ["reports-weekly"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<WeeklyReport[]>>(
        "/reports/weekly"
      );
      return res.data.data;
    },
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">Reports</h1>
        <p className="mt-1 text-sm text-slate-400">
          Weekly performance reports and analytics
        </p>
      </div>

      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : reports && reports.length > 0 ? (
        <div className="space-y-4">
          {reports.map((report) => (
            <div
              key={report.id}
              className="rounded-xl border border-surface-border bg-surface p-5"
            >
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="text-sm font-semibold text-white">
                    {report.week_start} &mdash; {report.week_end}
                  </h3>
                  {report.summary && (
                    <p className="mt-1 text-xs text-slate-400">{report.summary}</p>
                  )}
                </div>
              </div>
              <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div>
                  <p className="text-xs text-slate-500">Trades</p>
                  <p className="text-sm font-semibold text-white">
                    {report.total_trades}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-500">Win Rate</p>
                  <p className="text-sm font-semibold text-white">
                    {(report.win_rate * 100).toFixed(1)}%
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-500">P&amp;L</p>
                  <p
                    className={`text-sm font-semibold ${
                      report.pnl >= 0 ? "text-profit" : "text-loss"
                    }`}
                  >
                    {report.pnl >= 0 ? "+" : ""}
                    {report.pnl.toLocaleString("en-IN", {
                      style: "currency",
                      currency: "INR",
                    })}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-slate-500">Discipline</p>
                  <p className="text-sm font-semibold text-white">
                    {report.discipline_score}/10
                  </p>
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <FileText className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">No reports available yet</p>
            <p className="text-xs text-slate-500">
              Weekly reports are generated after your first trading week
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
