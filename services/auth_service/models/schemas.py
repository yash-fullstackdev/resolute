"""
Pydantic request/response models with strict validation per security spec.
"""

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator


# ── Auth Request Models ───────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z\s]+$")

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Must contain at least one lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Must contain at least one digit")
        if not re.search(r"[!@#$%^&*]", v):
            raise ValueError("Must contain at least one special character (!@#$%^&*)")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=64, max_length=64)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=64, max_length=64)


# ── Auth Response Models ─────────────────────────────────────────────────────


class RegisterResponse(BaseModel):
    tenant_id: str
    message: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    tenant_id: str
    tier: str
    expires_at: datetime


class TokenRefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: datetime


class LogoutResponse(BaseModel):
    message: str


class LogoutAllResponse(BaseModel):
    message: str
    sessions_revoked: int


# ── Broker Request/Response Models ────────────────────────────────────────────


class BrokerConnectRequest(BaseModel):
    broker: str = Field(pattern=r"^(dhan|zerodha)$")
    api_key: str = Field(min_length=10, max_length=100)
    api_secret: str = Field(min_length=10, max_length=100)
    client_id: str = Field(min_length=5, max_length=20)
    totp_secret: str = Field(min_length=16, max_length=64)


class BrokerConnectResponse(BaseModel):
    broker: str
    is_verified: bool
    message: str


class BrokerStatusItem(BaseModel):
    broker: str
    is_verified: bool
    is_primary: bool
    token_expires_at: datetime | None
    connected_at: datetime


class BrokerStatusResponse(BaseModel):
    brokers: list[BrokerStatusItem]


class BrokerDisconnectResponse(BaseModel):
    broker: str
    message: str


class BrokerRefreshResponse(BaseModel):
    broker: str
    token_expires_at: datetime | None
    message: str


# ── Subscription Models ──────────────────────────────────────────────────────


class SubscriptionResponse(BaseModel):
    tier: str
    status: str
    trial_ends_at: datetime | None
    subscription_ends_at: datetime | None


class SubscriptionUpgradeRequest(BaseModel):
    target_tier: str = Field(pattern=r"^(SIGNAL|SEMI_AUTO|FULL_AUTO)$")


class SubscriptionUpgradeResponse(BaseModel):
    tier: str
    status: str
    message: str


class TierInfo(BaseModel):
    tier: str
    name: str
    description: str
    features: list[str]
    price_monthly_inr: int
    price_yearly_inr: int
    max_custom_strategies: int
    auto_execution: bool
    semi_auto_execution: bool
    broker_connect: bool


class TiersResponse(BaseModel):
    tiers: list[TierInfo]


# ── Internal Models ───────────────────────────────────────────────────────────


class TenantRecord(BaseModel):
    id: str
    email: str
    name: str
    subscription_tier: str
    subscription_status: str
    trial_ends_at: datetime | None
    subscription_ends_at: datetime | None
    created_at: datetime
    is_active: bool
    email_verified: bool


class BrokerCredentialRecord(BaseModel):
    broker: str
    api_key: str
    api_secret: str
    client_id: str
    totp_secret: str
    access_token: str | None
    token_expires_at: datetime | None
    is_verified: bool


class ActiveTenantItem(BaseModel):
    tenant_id: str
    email: str
    tier: str
    brokers: list[str]


class ActiveTenantsResponse(BaseModel):
    tenants: list[ActiveTenantItem]
    count: int


# ── Standardized Error Response ───────────────────────────────────────────────


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.utcnow())


# ── Health Check ──────────────────────────────────────────────────────────────


class HealthCheckItem(BaseModel):
    status: str
    latency_ms: float | None = None


class HealthCheckResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str
    checks: dict[str, HealthCheckItem]
