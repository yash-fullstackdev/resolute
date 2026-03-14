"use client";

import { useState } from "react";
import type { Condition, ConditionOperator } from "@/types/strategy";
import { Plus, Trash2 } from "lucide-react";

const OPERATORS: Array<{ value: ConditionOperator; label: string }> = [
  { value: "GREATER_THAN", label: ">" },
  { value: "LESS_THAN", label: "<" },
  { value: "EQUALS", label: "=" },
  { value: "CROSSES_ABOVE", label: "Crosses Above" },
  { value: "CROSSES_BELOW", label: "Crosses Below" },
  { value: "BETWEEN", label: "Between" },
];

interface ConditionBuilderProps {
  conditions: Condition[];
  onChange: (conditions: Condition[]) => void;
  label: string;
  availableOperands: string[];
}

export function ConditionBuilder({
  conditions,
  onChange,
  label,
  availableOperands,
}: ConditionBuilderProps) {
  const [nextId, setNextId] = useState(conditions.length + 1);

  const addCondition = (group: number) => {
    const newCondition: Condition = {
      id: `cond_${nextId}`,
      left_operand: availableOperands[0] ?? "",
      operator: "GREATER_THAN",
      right_operand: 0,
      group,
    };
    setNextId((prev) => prev + 1);
    onChange([...conditions, newCondition]);
  };

  const removeCondition = (id: string) => {
    onChange(conditions.filter((c) => c.id !== id));
  };

  const updateCondition = (id: string, updates: Partial<Condition>) => {
    onChange(
      conditions.map((c) => (c.id === id ? { ...c, ...updates } : c))
    );
  };

  // Group conditions by group number
  const groups = new Map<number, Condition[]>();
  for (const cond of conditions) {
    const existing = groups.get(cond.group) ?? [];
    existing.push(cond);
    groups.set(cond.group, existing);
  }

  const maxGroup = Math.max(0, ...Array.from(groups.keys()));

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-white">{label}</h3>

      {Array.from(groups.entries()).map(([groupNum, groupConditions]) => (
        <div key={groupNum} className="rounded-lg border border-surface-border bg-surface-dark p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-slate-400">
              Group {groupNum + 1} (AND)
            </span>
          </div>

          {groupConditions.map((cond) => (
            <div key={cond.id} className="flex items-center gap-2">
              {/* Left operand */}
              <select
                value={cond.left_operand}
                onChange={(e) => updateCondition(cond.id, { left_operand: e.target.value })}
                className="flex-1 rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
              >
                {availableOperands.map((op) => (
                  <option key={op} value={op}>
                    {op}
                  </option>
                ))}
              </select>

              {/* Operator */}
              <select
                value={cond.operator}
                onChange={(e) =>
                  updateCondition(cond.id, { operator: e.target.value as ConditionOperator })
                }
                className="rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
              >
                {OPERATORS.map((op) => (
                  <option key={op.value} value={op.value}>
                    {op.label}
                  </option>
                ))}
              </select>

              {/* Right operand */}
              <input
                type="text"
                value={String(cond.right_operand)}
                onChange={(e) => {
                  const num = Number(e.target.value);
                  updateCondition(cond.id, {
                    right_operand: isNaN(num) ? e.target.value : num,
                  });
                }}
                className="w-24 rounded-md border border-surface-border bg-surface px-2 py-1.5 text-xs text-white focus:border-accent focus:outline-none"
              />

              <button
                onClick={() => removeCondition(cond.id)}
                className="rounded p-1 text-slate-500 hover:text-loss"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}

          <button
            onClick={() => addCondition(groupNum)}
            className="flex items-center gap-1 text-xs text-accent-light hover:text-accent"
          >
            <Plus className="h-3 w-3" /> Add Condition
          </button>
        </div>
      ))}

      {/* Add OR group */}
      <button
        onClick={() => addCondition(maxGroup + 1)}
        className="flex items-center gap-1 rounded-md border border-dashed border-surface-border px-3 py-2 text-xs text-slate-400 hover:border-accent hover:text-accent-light"
      >
        <Plus className="h-3 w-3" /> Add OR Group
      </button>
    </div>
  );
}
