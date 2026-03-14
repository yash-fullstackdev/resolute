.PHONY: build run run-paper stop test test-multiuser lint db-migrate db-reset \
       logs logs-workers workers backtest clean frontend frontend-dev

# ── Build & Run ───────────────────────────────────────────────────────────────

build:
	docker compose build

run:
	docker compose up -d
	@echo "Dashboard API:  http://localhost:8000"
	@echo "Auth Service:   http://localhost:8001"
	@echo "Frontend:       http://localhost:3000"
	@echo "NATS monitor:   http://localhost:8222"

run-paper:
	docker compose up -d
	@echo "All users default to paper trading mode from their strategy config"

stop:
	docker compose down

# ── Frontend ──────────────────────────────────────────────────────────────────

frontend:
	docker compose up -d --build frontend
	@echo "Frontend:  http://localhost:3000"

frontend-dev:
	cd frontend && npm install && npm run dev

# ── Testing ───────────────────────────────────────────────────────────────────

test:
	@echo "Running Go unit tests..."
	cd services/feed_gateway && go test ./...
	cd services/order_router && go test ./...
	@echo "Running Python unit tests..."
	cd services/auth_service && python -m pytest tests/
	cd services/signal_engine && python -m pytest tests/
	cd services/user_worker_pool && python -m pytest tests/
	cd services/dashboard_api && python -m pytest tests/

test-multiuser:
	@echo "Running multi-user isolation integration tests..."
	python -m pytest tests/integration/multiuser/ -v

# ── Linting ───────────────────────────────────────────────────────────────────

lint:
	cd services/feed_gateway && golangci-lint run
	cd services/order_router && golangci-lint run
	cd services/auth_service && ruff check .
	cd services/signal_engine && ruff check .
	cd services/user_worker_pool && ruff check .
	cd services/dashboard_api && ruff check .

# ── Database ──────────────────────────────────────────────────────────────────

db-migrate:
	docker compose exec timescaledb psql -U options -d options_db -f /docker-entrypoint-initdb.d/init.sql

db-reset:
	docker compose down -v
	docker compose up -d timescaledb redis
	sleep 5
	$(MAKE) db-migrate

# ── Logs & Monitoring ────────────────────────────────────────────────────────

logs:
	docker compose logs -f --tail=100

logs-workers:
	docker compose logs -f user_worker_pool --tail=200

workers:
	@echo "Active user workers:"
	curl -s http://localhost:8000/admin/system/workers | python -m json.tool

# ── Backtesting ──────────────────────────────────────────────────────────────

backtest:
	cd tests/backtest && python runner.py --config ../../config/base.yaml --start 2024-01-01 --end 2024-12-31

# ── Cleanup ──────────────────────────────────────────────────────────────────

clean:
	docker compose down -v --rmi local
	find . -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
