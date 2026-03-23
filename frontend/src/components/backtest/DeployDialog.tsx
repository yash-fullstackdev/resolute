"use client";

import { useState } from "react";
import { Rocket, X } from "lucide-react";

const INP = "w-full rounded-lg border border-surface-border bg-surface-light px-2.5 py-2 text-sm text-white focus:outline-none focus:ring-1 focus:ring-accent-light";

export function DeployDialog({ defaultName, onConfirm, onCancel }: {
  defaultName: string;
  onConfirm: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(defaultName);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="w-full max-w-md rounded-2xl border border-surface-border bg-surface-dark p-6 space-y-4 shadow-2xl">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-bold text-white">Deploy to Paper Trading</h3>
          <button onClick={onCancel} className="text-slate-500 hover:text-white transition-colors">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-1">
          <label className="text-[10px] font-medium text-slate-500 uppercase">Instance Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={INP}
            autoFocus
          />
          <p className="text-[10px] text-slate-600">This is how the strategy instance will appear in your Strategies page.</p>
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onCancel} className="rounded-lg px-4 py-2 text-sm text-slate-400 hover:text-white transition-colors">
            Cancel
          </button>
          <button
            onClick={() => onConfirm(name.trim() || defaultName)}
            disabled={!name.trim()}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-light transition-colors disabled:opacity-40"
          >
            <Rocket className="h-3.5 w-3.5" /> Deploy
          </button>
        </div>
      </div>
    </div>
  );
}
