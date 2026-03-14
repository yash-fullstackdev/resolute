"use client";

import { useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuthStore } from "@/stores/authStore";

export function useAuth() {
  const router = useRouter();
  const { user, isAuthenticated, isLoading, tier, login, register, logout, loadUser } =
    useAuthStore();

  const handleLogin = useCallback(
    async (email: string, password: string) => {
      await login(email, password);
      router.push("/overview");
    },
    [login, router]
  );

  const handleRegister = useCallback(
    async (email: string, password: string, fullName: string) => {
      await register(email, password, fullName);
      router.push("/login");
    },
    [register, router]
  );

  const handleLogout = useCallback(() => {
    logout();
    router.push("/login");
  }, [logout, router]);

  return {
    user,
    isAuthenticated,
    isLoading,
    tier,
    login: handleLogin,
    register: handleRegister,
    logout: handleLogout,
    loadUser,
  };
}
