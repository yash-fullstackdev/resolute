"use client";

import { useEffect, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";
import { isAuthenticated as checkAuth } from "@/lib/auth";

interface AuthGuardProps {
  children: ReactNode;
}

export function AuthGuard({ children }: AuthGuardProps) {
  const router = useRouter();
  const { isAuthenticated, isLoading, loadUser, user } = useAuthStore();

  useEffect(() => {
    if (!checkAuth()) {
      router.replace("/login");
      return;
    }
    if (!user && !isLoading) {
      void loadUser();
    }
  }, [router, user, isLoading, loadUser]);

  if (!isAuthenticated || isLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-surface-dark">
        <div className="flex flex-col items-center gap-4">
          <div className="h-8 w-8 animate-spin rounded-full border-2 border-accent border-t-transparent" />
          <p className="text-sm text-slate-400">Loading...</p>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
