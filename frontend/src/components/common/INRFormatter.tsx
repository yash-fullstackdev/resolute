"use client";

import { formatINR, pnlColorClass } from "@/lib/formatters";

interface INRFormatterProps {
  value: number;
  showSign?: boolean;
  colorCode?: boolean;
  className?: string;
}

export function INRFormatter({ value, showSign = false, colorCode = false, className = "" }: INRFormatterProps) {
  const colorClass = colorCode ? pnlColorClass(value) : "";

  return (
    <span className={`tabular-nums ${colorClass} ${className}`}>
      {formatINR(value, showSign)}
    </span>
  );
}
