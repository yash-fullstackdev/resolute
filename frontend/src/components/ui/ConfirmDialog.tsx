"use client";

import { useEffect, useRef } from "react";
import { AlertTriangle, Trash2, Zap, X } from "lucide-react";

export type ConfirmVariant = "danger" | "warning" | "info";

interface ConfirmDialogProps {
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: ConfirmVariant;
  loading?: boolean;
}

const VARIANT_STYLES: Record<ConfirmVariant, {
  icon: typeof AlertTriangle;
  iconBg: string;
  iconColor: string;
  buttonBg: string;
  buttonHover: string;
}> = {
  danger: {
    icon: Trash2,
    iconBg: "bg-loss/10",
    iconColor: "text-loss",
    buttonBg: "bg-loss",
    buttonHover: "hover:bg-loss/80",
  },
  warning: {
    icon: AlertTriangle,
    iconBg: "bg-amber-400/10",
    iconColor: "text-amber-400",
    buttonBg: "bg-amber-500",
    buttonHover: "hover:bg-amber-400",
  },
  info: {
    icon: Zap,
    iconBg: "bg-accent/10",
    iconColor: "text-accent-light",
    buttonBg: "bg-accent",
    buttonHover: "hover:bg-accent-light",
  },
};

export function ConfirmDialog({
  open,
  onConfirm,
  onCancel,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "danger",
  loading = false,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onCancel]);

  // Close on backdrop click
  const handleBackdropClick = (e: React.MouseEvent) => {
    if (dialogRef.current && !dialogRef.current.contains(e.target as Node)) {
      onCancel();
    }
  };

  if (!open) return null;

  const styles = VARIANT_STYLES[variant];
  const Icon = styles.icon;

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={handleBackdropClick}
    >
      <div
        ref={dialogRef}
        className="w-full max-w-md rounded-2xl border border-surface-border bg-surface-dark p-6 shadow-2xl animate-in fade-in zoom-in-95 duration-200"
      >
        {/* Icon + Close */}
        <div className="flex items-start justify-between">
          <div className={`rounded-xl p-3 ${styles.iconBg}`}>
            <Icon className={`h-6 w-6 ${styles.iconColor}`} />
          </div>
          <button
            onClick={onCancel}
            className="rounded-lg p-1 text-slate-500 hover:bg-surface-light hover:text-white transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Content */}
        <div className="mt-4">
          <h3 className="text-base font-semibold text-white">{title}</h3>
          <p className="mt-2 text-sm text-slate-400 leading-relaxed">{description}</p>
        </div>

        {/* Actions */}
        <div className="mt-6 flex gap-3">
          <button
            onClick={onCancel}
            disabled={loading}
            className="flex-1 rounded-xl border border-surface-border px-4 py-2.5 text-sm font-medium text-slate-300 hover:bg-surface-light hover:text-white transition-colors disabled:opacity-50"
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={`flex-1 flex items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-white transition-colors disabled:opacity-50 ${styles.buttonBg} ${styles.buttonHover}`}
          >
            {loading ? (
              <div className="h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" />
            ) : (
              confirmLabel
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
