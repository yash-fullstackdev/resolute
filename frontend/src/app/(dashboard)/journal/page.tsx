"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiClient } from "@/lib/api";
import type { ApiResponse } from "@/types/api";
import { BookOpen, ChevronDown, ChevronUp } from "lucide-react";

interface JournalEntry {
  id: string;
  date: string;
  entry_type: "TRADE" | "OBSERVATION" | "LESSON" | "REVIEW";
  title: string;
  content: string;
  mood: string;
  discipline_score: number;
  tags: string[];
}

const ENTRY_TYPE_COLORS: Record<string, string> = {
  TRADE: "bg-accent/10 text-accent-light",
  OBSERVATION: "bg-blue-500/10 text-blue-400",
  LESSON: "bg-amber-500/10 text-amber-400",
  REVIEW: "bg-purple-500/10 text-purple-400",
};

export default function JournalPage() {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const { data: entries, isLoading } = useQuery<JournalEntry[]>({
    queryKey: ["journal"],
    queryFn: async () => {
      const res = await apiClient.get<ApiResponse<JournalEntry[]>>("/journal");
      return res.data.data;
    },
  });

  const toggle = (id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-white">Trade Journal</h1>
        <p className="mt-1 text-sm text-slate-400">
          Record trades, observations, lessons, and reviews
        </p>
      </div>

      {isLoading ? (
        <div className="flex h-64 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
        </div>
      ) : entries && entries.length > 0 ? (
        <div className="space-y-4">
          {entries.map((entry) => {
            const isExpanded = expandedId === entry.id;
            const typeClass =
              ENTRY_TYPE_COLORS[entry.entry_type] ?? "bg-slate-600/10 text-slate-400";

            return (
              <div
                key={entry.id}
                className="rounded-xl border border-surface-border bg-surface transition-colors hover:border-surface-border/80"
              >
                <button
                  onClick={() => toggle(entry.id)}
                  className="flex w-full items-center justify-between p-4 text-left"
                >
                  <div className="flex-1 space-y-1">
                    <div className="flex items-center gap-3">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-medium ${typeClass}`}
                      >
                        {entry.entry_type}
                      </span>
                      <span className="text-xs text-slate-500">{entry.date}</span>
                      {entry.mood && (
                        <span className="text-xs text-slate-500">
                          Mood: {entry.mood}
                        </span>
                      )}
                      {entry.discipline_score > 0 && (
                        <span className="text-xs text-slate-500">
                          Discipline: {entry.discipline_score}/10
                        </span>
                      )}
                    </div>
                    <h3 className="text-sm font-semibold text-white">
                      {entry.title}
                    </h3>
                    {!isExpanded && (
                      <p className="line-clamp-1 text-xs text-slate-400">
                        {entry.content}
                      </p>
                    )}
                  </div>
                  {isExpanded ? (
                    <ChevronUp className="ml-3 h-4 w-4 flex-shrink-0 text-slate-400" />
                  ) : (
                    <ChevronDown className="ml-3 h-4 w-4 flex-shrink-0 text-slate-400" />
                  )}
                </button>

                {isExpanded && (
                  <div className="border-t border-surface-border px-4 pb-4 pt-3">
                    <p className="whitespace-pre-wrap text-sm text-slate-300">
                      {entry.content}
                    </p>
                    {entry.tags.length > 0 && (
                      <div className="mt-3 flex flex-wrap gap-2">
                        {entry.tags.map((tag) => (
                          <span
                            key={tag}
                            className="rounded-full bg-surface-dark px-2 py-0.5 text-xs text-slate-400"
                          >
                            #{tag}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-surface-border">
          <div className="text-center">
            <BookOpen className="mx-auto h-8 w-8 text-slate-500" />
            <p className="mt-2 text-sm text-slate-400">No journal entries yet</p>
            <p className="text-xs text-slate-500">
              Start recording your trading journey
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
