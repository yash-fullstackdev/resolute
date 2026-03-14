# ── Stage 1: Build ────────────────────────────────────────────────
FROM golang:1.22-alpine AS builder

RUN apk add --no-cache git ca-certificates tzdata

WORKDIR /app

# Copy dependency files first for layer caching
COPY go.mod go.sum ./
RUN go mod download

# Copy source code
COPY . .

# Build static binary
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -ldflags="-w -s" -o /order_router ./cmd/main.go

# ── Stage 2: Runtime ─────────────────────────────────────────────
FROM alpine:3.20

RUN apk add --no-cache ca-certificates tzdata

# Create non-root user
RUN addgroup -g 1001 -S appgroup && \
    adduser -u 1001 -S appuser -G appgroup

WORKDIR /app

# Copy binary and timezone data
COPY --from=builder /order_router .
COPY --from=builder /usr/share/zoneinfo /usr/share/zoneinfo

# Set timezone for IST-based token refresh scheduling
ENV TZ=Asia/Kolkata

# Switch to non-root user
USER appuser

EXPOSE 9091

ENTRYPOINT ["./order_router"]
