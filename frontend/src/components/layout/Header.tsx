"use client";

import { useAuthStore } from "@/stores/authStore";
import { useUIStore } from "@/stores/uiStore";
import { TIER_NAMES, TIER_COLORS } from "@/lib/constants";
import { Bell, Menu, LogOut, User } from "lucide-react";
import { useAuth } from "@/hooks/useAuth";

export function Header() {
  const user = useAuthStore((s) => s.user);
  const tier = useAuthStore((s) => s.tier);
  const { toggleSidebar } = useUIStore();
  const notifications = useUIStore((s) => s.notifications);
  const unreadCount = notifications.filter((n) => !n.read).length;
  const { logout } = useAuth();

  const tierColorClass = TIER_COLORS[tier] ?? "bg-slate-600 text-slate-200";

  return (
    <header className="flex h-16 items-center justify-between border-b border-surface-border bg-surface-dark px-4 lg:px-6">
      {/* Left: hamburger + title */}
      <div className="flex items-center gap-3">
        <button
          onClick={toggleSidebar}
          className="rounded-md p-2 text-slate-400 hover:bg-surface-light hover:text-white lg:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>
        <h1 className="text-lg font-semibold text-white">RESOLUTE</h1>
      </div>

      {/* Right: tier badge, notifications, user */}
      <div className="flex items-center gap-4">
        {/* Tier badge */}
        <span className={`rounded-full px-3 py-1 text-xs font-semibold ${tierColorClass}`}>
          {TIER_NAMES[tier] ?? tier}
        </span>

        {/* Notification bell */}
        <button className="relative rounded-md p-2 text-slate-400 hover:bg-surface-light hover:text-white">
          <Bell className="h-5 w-5" />
          {unreadCount > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-loss text-[10px] font-bold text-white">
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
          )}
        </button>

        {/* User menu */}
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-full bg-accent/20 text-accent-light">
            <User className="h-4 w-4" />
          </div>
          <span className="hidden text-sm text-slate-300 md:block">
            {user?.full_name ?? "User"}
          </span>
          <button
            onClick={logout}
            className="rounded-md p-2 text-slate-400 hover:bg-surface-light hover:text-loss"
            title="Logout"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </header>
  );
}
