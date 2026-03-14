# CLAUDE.md — Project Instructions for Claude Code

This file provides context and instructions for Claude Code when working on this codebase.

## Project Overview

**Resolute** is a multi-tenant SaaS platform for algorithmic options trading in Indian markets (NSE, MCX). It consists of 7 services (6 backend + 1 frontend) with 20 built-in strategies, an AI strategy builder, and a discipline enforcement engine.

## Repository Structure

```
resolute/
├── config/                    # YAML configs (symbols, strategies, capital tiers)
├── services/
│   ├── auth_service/          # Python FastAPI — JWT, credential vault, subscriptions
│   ├── feed_gateway/          # Go — Market data ingestion via WebSocket → NATS
│   ├── signal_engine/         # Python — Greeks, IV, PCR, regime classification
│   ├── user_worker_pool/      # Python — Strategy evaluation, discipline, AI builder
│   ├── order_router/          # Go — Broker execution (Dhan/Zerodha/Paper)
│   └── dashboard_api/         # Python FastAPI — REST API, WebSocket, admin
├── frontend/                  # Next.js 15, TypeScript, Tailwind, shadcn/ui
├── shared/
│   ├── schema/                # TimescaleDB init SQL (16 tables, RLS policies)
│   └── proto/                 # Protobuf definitions (tick, signal, order)
├── specs/                     # 23 specification files (in parent directory)
├── docker-compose.yml         # 10 services with health checks
├── Makefile                   # Build, test, run, lint targets
└── .env.example               # All environment variables documented
```

## Tech Stack

- **Python 3.12** — FastAPI, asyncio, SQLAlchemy async, structlog, numpy, numba
- **Go 1.22** — zerolog, nats.go, prometheus, pgx
- **TypeScript** — Next.js 15 (App Router), React 19, Tailwind CSS, Zustand, TanStack Query
- **Infrastructure** — TimescaleDB (PostgreSQL 15 + RLS), NATS, Redis, Docker Compose

## Key Commands

```bash
make build          # Build all Docker images
make run            # Start all 10 services
make stop           # Stop all services
make test           # Run all unit tests (Go + Python)
make test-multiuser # Run multi-tenant isolation tests
make lint           # Lint all services
make logs           # Tail all service logs
make logs-workers   # Tail user_worker_pool logs
make db-reset       # Reset database (destructive)
make clean          # Remove all containers, volumes, images
```

## Development Patterns

### Python Services (auth_service, signal_engine, user_worker_pool, dashboard_api)

- **Framework**: FastAPI with async endpoints
- **Logging**: `structlog` only — never use `print()`. Every log must have: `service`, `event`, `tenant_id` (where applicable)
- **Database**: SQLAlchemy async with `asyncpg`. Always use `rls_session(tenant_id)` context manager for tenant queries — it does `SET LOCAL app.current_tenant_id`
- **Validation**: Pydantic v2 models for all API inputs with strict validation
- **Error format**: Standardized `{"error": {"code": "...", "message": "...", "details": {}}, "request_id": "...", "timestamp": "..."}`
- **Dependencies**: Pinned in `requirements.txt` per service

### Go Services (feed_gateway, order_router)

- **Logging**: `zerolog` only — structured JSON logging
- **NATS subjects**: Always use constants from the types file, never hardcode subject strings
- **Concurrency**: Use `sync.RWMutex` for shared state, context-based cancellation
- **Errors**: Wrap errors with context, never ignore errors

### Frontend (Next.js)

- **Router**: App Router (not Pages Router)
- **State**: Zustand stores (authStore, liveDataStore, uiStore)
- **API**: Axios with JWT interceptor, TanStack Query for caching
- **Styling**: Tailwind CSS classes directly, dark mode default
- **Types**: Strict TypeScript, no `any`
- **Formatting**: INR with Indian numbering (`₹1,00,000`), times in IST

## Critical Constraints (Must Follow)

1. **No secrets in code** — All credentials via environment variables only
2. **Every DB query must set RLS context** — `SET LOCAL app.current_tenant_id` before tenant queries. Failure = data leak.
3. **Every order must have stop_loss_price, target_price, time_stop** — Discipline gate rejects orders missing any of these. No exceptions.
4. **order_router subscribes to `orders.new.validated.{tenant_id}` only** — Never consume from `orders.new.{tenant_id}` directly
5. **Circuit breaker is absolute** — Once halted, no orders pass. No override. No admin bypass.
6. **JWT tenant_id is authoritative** — Never trust tenant_id from request body/query. Only use JWT claim.
7. **Broker credentials never logged** — Never in logs, API responses, or NATS messages
8. **Subscription tier gating is middleware** — Route handlers never contain tier-check logic
9. **Greeks skip deep ITM** — If moneyness > 15%, mark Greeks unreliable and skip
10. **NATS subjects are constants** — Defined in shared files, imported everywhere
11. **TimescaleDB writes are async** — DB failures never block signal generation or order routing
12. **Capital tier enforcement** — Selling strategies blocked for users below PRO tier (₹2L)

## Multi-Tenancy Model

- **Database**: PostgreSQL Row-Level Security (RLS) on all per-tenant tables
- **API**: JWT `tenant_id` claim injected by middleware into every request
- **Workers**: Each user gets an isolated `UserWorker` asyncio task
- **Broker**: Each user has their own `BrokerClient` instance in `order_router`
- **NATS**: Per-user subjects namespaced by `{tenant_id}`

## Strategy System

- **20 built-in strategies** organized into 3 categories:
  - BUYING (8) — STARTER tier, ₹10k+
  - HYBRID (3) — GROWTH tier, ₹50k+
  - SELLING (7) — PRO tier, ₹2L+
- **AI Custom strategies** — Users compose from 30+ indicators, deploy on multiple symbols
- All strategies implement `BaseStrategy` ABC with `evaluate()` and `should_exit()`
- Strategy registry in `user_worker_pool/strategies/__init__.py`

## Testing

- Unit tests: `services/{service}/tests/` — pytest (Python), `go test` (Go)
- Integration tests: `tests/integration/multiuser/` — multi-tenant isolation
- Backtest: `tests/backtest/runner.py` — historical tick replay

## Spec Files

Detailed specifications live in `../specs/` (23 files). Key ones:
- `specs/README.md` — Index of all spec files with usage guide
- `specs/00_overview_and_architecture.md` — Start here for context
- `specs/18_constraints_and_testing.md` — All constraints and test requirements
- `specs/19_security_and_operations.md` — Security, monitoring, backup specs
- `specs/20_frontend.md` — Frontend page/component spec

## Common Tasks

### Adding a new strategy
1. Create file in `services/user_worker_pool/strategies/{name}.py`
2. Extend `BaseStrategy`, set `name`, `category`, `min_capital_tier`, `requires_margin`
3. Implement `evaluate()` and `should_exit()`
4. Add stop rules to `risk/stop_loss.py`
5. Register in `strategies/__init__.py` STRATEGY_REGISTRY
6. Add config entry in `config/strategies.yaml`

### Adding a new API endpoint
1. Create or edit router in `services/dashboard_api/routers/`
2. Add Pydantic models in `models/schemas.py`
3. Use `request.state.tenant_id` for tenant scoping
4. Add tier requirement in `middleware/subscription.py` TIER_REQUIREMENTS
5. Scope all DB queries with `rls_session(tenant_id)`

### Adding a new indicator
1. Add to appropriate file in `services/user_worker_pool/custom/indicators/`
2. Add enum value to `IndicatorType` in `indicators/__init__.py`
3. Add dispatch case in `custom/indicator_engine.py`
4. Add to indicator catalog in `dashboard_api/routers/strategies.py`
5. Add to frontend constants in `frontend/src/lib/constants.ts`
