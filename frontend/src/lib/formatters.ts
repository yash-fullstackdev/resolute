/**
 * Format a number in Indian numbering system with INR symbol.
 * e.g. 100000 => "₹1,00,000"
 */
export function formatINR(value: number | undefined | null, showSign = false): string {
  if (value == null) return "₹0";
  const absValue = Math.abs(value);
  const formatted = absValue.toLocaleString("en-IN", {
    maximumFractionDigits: 2,
    minimumFractionDigits: 0,
  });
  const sign = value >= 0 ? (showSign ? "+" : "") : "-";
  return `${sign}₹${formatted}`;
}

/**
 * Format percentage with sign.
 * e.g. 0.38 => "+38.00%"
 */
export function formatPercentage(value: number | undefined | null, decimals = 2): string {
  if (value == null) return "0.00%";
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(decimals)}%`;
}

/**
 * Format a date string to IST display with "IST" label.
 * e.g. "2026-03-14T10:32:00Z" => "14 Mar 2026, 04:02 PM IST"
 */
export function formatDateIST(dateStr: string): string {
  const date = new Date(dateStr);
  return (
    date.toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST"
  );
}

/**
 * Format a time string to IST HH:MM format.
 * e.g. "2026-03-14T10:32:00Z" => "04:02 PM IST"
 */
export function formatTimeIST(dateStr: string): string {
  const date = new Date(dateStr);
  return (
    date.toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST"
  );
}

/**
 * Format date only in IST.
 */
export function formatDateOnlyIST(dateStr: string): string {
  const date = new Date(dateStr);
  return date.toLocaleDateString("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}

/**
 * Get P&L color class based on value.
 */
export function pnlColorClass(value: number | undefined | null): string {
  if (value == null) return "text-slate-400";
  if (value > 0) return "text-profit";
  if (value < 0) return "text-loss";
  return "text-slate-400";
}

/**
 * Get P&L background color class based on value.
 */
export function pnlBgClass(value: number | undefined | null): string {
  if (value == null) return "bg-slate-700/50 text-slate-400";
  if (value > 0) return "bg-profit/10 text-profit";
  if (value < 0) return "bg-loss/10 text-loss";
  return "bg-slate-700/50 text-slate-400";
}
