"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useUIStore } from "@/stores/uiStore";
import {
  LayoutDashboard,
  BarChart3,
  Zap,
  ClipboardList,
  Layers,
  Wrench,
  Shield,
  TrendingUp,
  Settings,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

const navItems = [
  { href: "/overview", label: "Overview", icon: LayoutDashboard },
  { href: "/positions", label: "Positions", icon: BarChart3 },
  { href: "/signals", label: "Signals", icon: Zap },
  { href: "/plan", label: "Plan", icon: ClipboardList },
  { href: "/strategies", label: "Strategies", icon: Layers },
  { href: "/strategies/builder", label: "Builder", icon: Wrench },
  { href: "/discipline", label: "Discipline", icon: Shield },
  { href: "/performance", label: "Performance", icon: TrendingUp },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function Sidebar() {
  const pathname = usePathname();
  const { sidebarOpen, toggleSidebar } = useUIStore();

  return (
    <>
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          onClick={toggleSidebar}
        />
      )}

      <aside
        className={`fixed left-0 top-0 z-50 flex h-full flex-col border-r border-surface-border bg-surface-dark transition-all duration-300 lg:relative lg:z-auto ${
          sidebarOpen ? "w-64" : "w-16"
        } ${sidebarOpen ? "translate-x-0" : "-translate-x-full lg:translate-x-0"}`}
      >
        {/* Logo */}
        <div className="flex h-16 items-center justify-between border-b border-surface-border px-4">
          {sidebarOpen && (
            <span className="text-lg font-bold text-white">RESOLUTE</span>
          )}
          <button
            onClick={toggleSidebar}
            className="hidden rounded-md p-1 text-slate-400 hover:bg-surface-light hover:text-white lg:block"
          >
            {sidebarOpen ? <ChevronLeft className="h-5 w-5" /> : <ChevronRight className="h-5 w-5" />}
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 space-y-1 overflow-y-auto p-3">
          {navItems.map((item) => {
            const isActive = pathname === item.href || pathname.startsWith(item.href + "/");
            const Icon = item.icon;

            return (
              <Link
                key={item.href}
                href={item.href}
                className={`flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-accent/10 text-accent-light"
                    : "text-slate-400 hover:bg-surface-light hover:text-white"
                }`}
              >
                <Icon className="h-5 w-5 flex-shrink-0" />
                {sidebarOpen && <span>{item.label}</span>}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div className="border-t border-surface-border p-3">
          {sidebarOpen && (
            <p className="text-xs text-slate-500">India Options Builder v0.1</p>
          )}
        </div>
      </aside>
    </>
  );
}
