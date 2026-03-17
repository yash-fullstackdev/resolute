# ── Build stage ────────────────────────────────────────────────────────────────
FROM golang:1.22-alpine AS builder

RUN apk add --no-cache ca-certificates tzdata git

WORKDIR /build

# Cache dependency downloads.
COPY go.mod go.sum* ./
RUN go mod download 2>/dev/null || true

# Copy source, resolve deps, and build.
COPY . .
RUN rm -f go.sum && GONOSUMCHECK=* GONOSUMDB=* GOFLAGS=-mod=mod go mod tidy
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-w -s" \
    -o /feed_gateway \
    ./cmd/main.go

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -g 1000 appgroup \
    && adduser -u 1000 -G appgroup -s /bin/sh -D appuser

WORKDIR /app

COPY --from=builder /feed_gateway .
COPY --from=builder /usr/share/zoneinfo /usr/share/zoneinfo

# Ensure IST timezone data is available.
ENV TZ=Asia/Kolkata

USER appuser

# Health check port.
EXPOSE 8080
# Prometheus metrics port.
EXPOSE 9090

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://localhost:8080/health || exit 1

ENTRYPOINT ["/app/feed_gateway"]
