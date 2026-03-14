# Resolute — India Options Strategy Builder

A high-performance, multi-tenant SaaS platform for algorithmic options strategy execution in Indian markets (NSE indices, NSE equity F&O, MCX commodities).

## What This Platform Does

- **20 built-in strategies** — option buying, selling, and hybrid spreads
- **AI Strategy Builder** — compose custom strategies from 30+ technical indicators using natural language or visual builder
- **Capital-tier gating** — strategies unlocked based on user's capital (₹10k → ₹10L+)
- **Discipline enforcement** — mandatory stop-loss, target, time-stop on every trade. Pre-market plan locking. Circuit breaker. Override friction.
- **Multi-tenant** — each user has isolated data (PostgreSQL RLS), own broker account, own risk config
- **Real-time** — sub-200ms tick processing, WebSocket live feeds, NATS messaging

## Architecture

```
NSE/MCX WebSocket ──→ feed_gateway (Go) ──→ NATS
                                              │
                      signal_engine (Python) ←─┘ Greeks, IV, PCR, Regime
                                              │
                      user_worker_pool (Python) ← Per-user strategy evaluation
                         ├── 20 strategies        + discipline enforcement
                         ├── AI custom strategies
                         └── discipline engine
                                              │
                      order_router (Go) ←─────┘ Per-user broker execution
                         ├── Dhan (primary)
                         ├── Zerodha (fallback)
                         └── Paper trading
                                              │
                      dashboard_api (Python) ──→ REST + WebSocket API
                      auth_service (Python) ───→ JWT, credentials vault
                      frontend (Next.js) ──────→ Trading dashboard
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend (data plane) | Go 1.22 (feed_gateway, order_router) |
| Backend (logic plane) | Python 3.12 + FastAPI (signal_engine, user_worker_pool, auth, dashboard) |
| Frontend | Next.js 15, TypeScript, Tailwind CSS, shadcn/ui |
| Database | TimescaleDB (PostgreSQL 15 + time-series + RLS) |
| Messaging | NATS (sub-millisecond pub/sub) |
| Cache | Redis 7 (JWT blacklist, rate limiting, session management) |
| Containerisation | Docker Compose (10 services) |
| Monitoring | Prometheus metrics + health endpoints per service |

## Quick Start

### Prerequisites

- Docker & Docker Compose v2+
- Git

### 1. Clone & Configure

```bash
git clone https://github.com/yash-fullstackdev/resolute.git
cd resolute

# Create environment file from template
cp .env.example .env
```

### 2. Generate Secrets

Edit `.env` and fill in the required secrets:

```bash
# Generate JWT secret (64-char hex)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Generate admin JWT secret (separate from user JWT)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Generate credential encryption master key (32-byte hex)
python3 -c "import secrets; print(secrets.token_hex(32))"

# Generate internal auth token
python3 -c "import secrets; print(secrets.token_hex(24))"

# Generate database password
openssl rand -base64 24
```

Your `.env` should have at minimum:

```env
DB_PASSWORD=<generated>
JWT_SECRET=<generated>
ADMIN_JWT_SECRET=<generated>
CREDENTIAL_MASTER_KEY=<generated>
AUTH_INTERNAL_TOKEN=<generated>
```

### 3. Start the Platform

```bash
# Build all services
make build

# Start everything (paper trading mode by default)
make run
```

This starts all 10 services:

| Service | Port | Description |
|---------|------|-------------|
| Dashboard API | http://localhost:8000 | REST + WebSocket API |
| Auth Service | http://localhost:8001 | Authentication & credential vault |
| Frontend | http://localhost:3000 | Web dashboard |
| NATS Monitor | http://localhost:8222 | NATS monitoring dashboard |
| TimescaleDB | localhost:5432 | Database |
| Redis | localhost:6379 | Cache |

### 4. Verify Services Are Running

```bash
# Check all services
docker compose ps

# Check health endpoints
curl http://localhost:8000/health
curl http://localhost:8001/health

# View logs
make logs
```

### 5. Create Your First User

```bash
# Register
curl -X POST http://localhost:8001/auth/v1/register \
  -H "Content-Type: application/json" \
  -d '{
    "email": "trader@example.com",
    "password": "SecurePass123!",
    "name": "Test Trader"
  }'

# Login
curl -X POST http://localhost:8001/auth/v1/login \
  -H "Content-Type: application/json" \
  -d '{
    "email": "trader@example.com",
    "password": "SecurePass123!"
  }'
# Returns: { "access_token": "...", "refresh_token": "...", "tenant_id": "...", "tier": "TRIAL" }
```

### 6. Connect a Broker (Paper Mode)

By default, all users start in **paper trading mode**. No broker credentials needed for paper trading — the platform simulates fills with realistic slippage.

To connect a real broker (Dhan or Zerodha):

```bash
curl -X POST http://localhost:8000/api/v1/broker/connect \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "broker": "dhan",
    "api_key": "<your_dhan_api_key>",
    "api_secret": "<your_dhan_api_secret>",
    "client_id": "<your_client_id>",
    "totp_secret": "<your_totp_secret>"
  }'
```

### 7. Open the Dashboard

Visit **http://localhost:3000** and log in with your credentials.

## Common Operations

### Start in Paper Trading Mode

```bash
make run-paper
```

### View Worker Logs

```bash
make logs-workers
```

### Check Active Workers

```bash
make workers
```

### Run Tests

```bash
make test              # Unit tests (Go + Python)
make test-multiuser    # Multi-tenant isolation tests
```

### Reset Database

```bash
make db-reset
```

### Stop Everything

```bash
make stop
```

### Clean Everything (including data)

```bash
make clean
```

## Project Structure

```
resolute/
├── config/
│   ├── base.yaml              # Symbols, market hours, risk params
│   └── strategies.yaml        # All 20 strategy configs + capital tiers
├── services/
│   ├── auth_service/          # Python — JWT, credential vault, subscriptions
│   ├── feed_gateway/          # Go — Market data ingestion
│   ├── signal_engine/         # Python — Greeks, IV, chain snapshots
│   ├── user_worker_pool/      # Python — Strategies, discipline, AI builder
│   ├── order_router/          # Go — Broker execution, order routing
│   └── dashboard_api/         # Python — REST API, WebSocket, admin
├── frontend/                  # Next.js 15 — Trading dashboard
├── shared/
│   ├── schema/                # TimescaleDB init SQL (16 tables, RLS)
│   └── proto/                 # Protobuf definitions
├── docker-compose.yml
├── Makefile
└── .env.example
```

## Capital Tiers & Strategy Access

| Tier | Capital | Strategies Available |
|------|---------|---------------------|
| **STARTER** | ₹10k – ₹50k | 8 buying strategies (long call/put, straddle, strangle, spreads, PCR, event) |
| **GROWTH** | ₹50k – ₹2L | + 3 hybrid strategies (iron butterfly, diagonal, ratio back spread) |
| **PRO** | ₹2L – ₹10L | + 7 selling strategies (short straddle/strangle, credit spreads, iron condor, jade lizard, covered call) |
| **INSTITUTIONAL** | ₹10L+ | All above + unlimited AI custom strategies |

## Strategy Categories

### Buying (8 strategies)
- Long Call, Long Put
- Long Straddle, Long Strangle
- Bull Call Spread, Bear Put Spread
- PCR Contrarian, Event Directional
- MCX Gold/Silver, MCX Crude Put

### Hybrid (3 strategies)
- Iron Butterfly Long, Diagonal Spread, Ratio Back Spread

### Selling (7 strategies)
- Short Straddle, Short Strangle
- Credit Spread Call/Put, Iron Condor
- Jade Lizard, Covered Call

### AI Custom Strategies
Users compose custom strategies from 30+ indicators (RSI, MACD, Bollinger, SuperTrend, VWAP, etc.) and deploy across multiple symbols.

## Discipline System

Every trade on this platform carries:
- **Mandatory stop-loss** — computed at signal time, locked per-user
- **Mandatory target** — profit target set before entry
- **Mandatory time-stop** — hard exit time (e.g., Wednesday 3PM for weekly options)

Additional safeguards:
- **Pre-market plan locking** — users set rules before 09:10 IST, platform enforces during trading
- **Circuit breaker** — halts all trading if daily loss limit hit (no override possible)
- **Override friction** — 60-second cooldown + historical P&L impact shown before allowing stop-loss modification
- **Trade journal** — automatic discipline scoring (0–100) for every closed position

## API Documentation

### Authentication
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/auth/v1/register` | Register new user |
| POST | `/auth/v1/login` | Login, get JWT + refresh token |
| POST | `/auth/v1/refresh` | Refresh access token |
| POST | `/auth/v1/logout` | Revoke session |

### Trading (requires JWT)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/positions` | Open positions |
| DELETE | `/api/v1/positions/{id}` | Manual close (FULL_AUTO tier) |
| GET | `/api/v1/signals` | Recent signals |
| GET | `/api/v1/chain/{underlying}` | Live options chain |
| GET | `/api/v1/performance` | P&L summary |
| GET | `/api/v1/performance/daily` | Daily P&L series |

### Plan & Discipline
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET/POST | `/api/v1/plan` | Today's trading plan |
| POST | `/api/v1/plan/lock` | Lock plan early |
| GET | `/api/v1/discipline/score` | Discipline score (0–100) |
| POST | `/api/v1/discipline/override` | Request override (60s cooldown) |
| GET | `/api/v1/journal` | Trade journal |
| GET | `/api/v1/reports/weekly` | Weekly discipline report |

### Strategy Config
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/config` | User's strategy config |
| PUT | `/api/v1/config/strategy/{name}` | Update strategy params |
| POST | `/api/v1/strategies/custom` | Create custom AI strategy |
| POST | `/api/v1/strategies/ai/build` | Build strategy from natural language |

### WebSocket
| Endpoint | Description |
|----------|-------------|
| `ws://localhost:8000/ws/v1/signals?token=<jwt>` | Live signals, fills, alerts |

## Environment Variables

See [.env.example](.env.example) for all required variables with descriptions.

Key variables:
- `JWT_SECRET` — Signs user JWTs (64-char hex)
- `CREDENTIAL_MASTER_KEY` — AES-256 key for broker credential encryption
- `FEED_BROKER` — Market data source (`dhan` / `zerodha` / `paper`)
- `PLAN_LOCK_TIME_IST` — Auto-lock daily plan time (default: 09:10)
- `DEFAULT_DAILY_LOSS_LIMIT_INR` — Circuit breaker threshold (default: ₹5,000)

## Security

- **Multi-tenant isolation** — PostgreSQL Row-Level Security on all per-user tables
- **Credential encryption** — AES-256-GCM with key versioning and rotation support
- **JWT authentication** — 15-minute access tokens, 7-day refresh tokens with rotation
- **Rate limiting** — Per-IP (auth) and per-user (API) rate limits
- **CORS/CSRF** — Strict origin policy, CSRF tokens on state-changing requests
- **Audit logging** — Immutable audit trail for all security events
- **Input validation** — Pydantic strict validation on all API inputs

## Monitoring

Each service exposes Prometheus metrics:

| Service | Metrics Port | Key Metrics |
|---------|-------------|-------------|
| feed_gateway | :9090 | tick_count_total, publish_latency_ms |
| signal_engine | :9091 | chain_computation_seconds, greeks_duration |
| order_router | :9091 | orders_executed_total, order_latency |
| user_worker_pool | :9093 | signals_generated, orders_validated, circuit_breaker_halts |
| dashboard_api | :8000/metrics | api_requests_total, request_duration |

## Development

### Run Frontend Locally (without Docker)

```bash
cd frontend
npm install
npm run dev
# Opens on http://localhost:3000
```

### Run Individual Service

```bash
# Just infrastructure + one service
docker compose up -d nats timescaledb redis auth_service
```

### Lint

```bash
make lint
```

## License

Proprietary — Bitontree Internal Platform Development.

---

*Built with Claude Code — 187 files, 30,000+ lines of production code.*
