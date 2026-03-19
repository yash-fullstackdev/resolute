# ─── Stage 1: Compile Rust backtest engine ───────────────────────────────────
# Use python:3.12 as builder base so maturin links against the correct Python ABI
FROM python:3.12-slim-bookworm AS rust-builder

# Install Rust toolchain (stable, latest) + build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl pkg-config \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable --profile minimal \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

# Install maturin
RUN pip install --no-cache-dir maturin

WORKDIR /build
COPY backtest_engine/ ./backtest_engine/

# Build the .whl (links against Python 3.12 from this stage)
RUN cd backtest_engine && \
    maturin build --release --interpreter python3 --out /wheels

# ─── Stage 2: Python runtime ─────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Security: non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install Python deps
COPY services/dashboard_api/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install the compiled Rust extension wheel from stage 1
COPY --from=rust-builder /wheels/ /wheels/
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# Copy dashboard_api service code
COPY services/dashboard_api/ ./dashboard_api/

# Copy backtest Python module (runner, config, reporting, adapter, etc.)
COPY backtest/ ./backtest/

# Copy user_worker_pool package — strategies need capital_tier and base modules
COPY services/user_worker_pool/ ./services/user_worker_pool/
RUN touch ./services/__init__.py \
 && touch ./services/user_worker_pool/__init__.py

# Ownership
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "dashboard_api.main:combined_app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--limit-concurrency", "200", \
     "--no-access-log", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
