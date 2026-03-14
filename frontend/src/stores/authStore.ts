import { create } from "zustand";
import type { User, TokenPair } from "@/types/auth";
import type { CapitalTier } from "@/types/strategy";
import { setTokens, clearTokens, getAccessToken } from "@/lib/auth";
import { authClient } from "@/lib/api";
import { apiClient } from "@/lib/api";

interface AuthState {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  tier: CapitalTier;

  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string, fullName: string) => Promise<void>;
  logout: () => void;
  loadUser: () => Promise<void>;
  setUser: (user: User) => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  user: null,
  isAuthenticated: !!getAccessToken(),
  isLoading: false,
  tier: "STARTER",

  login: async (email: string, password: string) => {
    set({ isLoading: true });
    try {
      const response = await authClient.post("/login", { email, password });
      const data = response.data as {
        access_token: string;
        refresh_token: string;
        tenant_id: string;
        tier: string;
        expires_at: string;
      };
      setTokens({
        access_token: data.access_token,
        refresh_token: data.refresh_token,
      });
      set({
        user: {
          id: data.tenant_id,
          tenant_id: data.tenant_id,
          email,
          full_name: "",
          capital_tier: data.tier as CapitalTier,
          is_active: true,
          is_verified: true,
          broker_connected: false,
          created_at: "",
          updated_at: "",
        },
        isAuthenticated: true,
        tier: data.tier as CapitalTier,
        isLoading: false,
      });
    } catch {
      set({ isLoading: false });
      throw new Error("Invalid email or password");
    }
  },

  register: async (email: string, password: string, fullName: string) => {
    set({ isLoading: true });
    try {
      await authClient.post("/register", {
        email,
        password,
        name: fullName,
      });
      set({ isLoading: false });
    } catch {
      set({ isLoading: false });
      throw new Error("Registration failed");
    }
  },

  logout: () => {
    clearTokens();
    set({
      user: null,
      isAuthenticated: false,
      tier: "STARTER",
    });
  },

  loadUser: async () => {
    const token = getAccessToken();
    if (!token) return;

    set({ isLoading: true });
    try {
      const response = await apiClient.get("/user/me");
      const user = response.data as User;
      set({
        user,
        isAuthenticated: true,
        tier: user.capital_tier,
        isLoading: false,
      });
    } catch {
      clearTokens();
      set({
        user: null,
        isAuthenticated: false,
        isLoading: false,
      });
    }
  },

  setUser: (user: User) => {
    set({ user, tier: user.capital_tier });
  },
}));
