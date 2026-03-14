import type { CapitalTier } from "./strategy";

export interface User {
  id: string;
  email: string;
  full_name: string;
  tenant_id: string;
  capital_tier: CapitalTier;
  is_active: boolean;
  is_verified: boolean;
  broker_connected: boolean;
  created_at: string;
  updated_at: string;
}

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

export interface RegisterResponse {
  user: User;
  message: string;
  verification_required: boolean;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
}
