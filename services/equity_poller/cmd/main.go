package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/dhan"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/health"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/poller"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/publisher"

	"github.com/jackc/pgx/v5/pgxpool"
)

func main() {
	// Structured JSON logging via zerolog.
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs
	log.Logger = zerolog.New(os.Stdout).With().Timestamp().Str("service", "equity_poller").Logger()

	log.Info().Msg("equity_poller starting")

	// ── Configuration from environment ──
	dhanBaseURL := envOrDefault("DHAN_BASE_URL", "https://api.dhan.co/v2")
	dhanAccessToken := os.Getenv("DHAN_ACCESS_TOKEN")
	if dhanAccessToken == "" {
		log.Fatal().Msg("DHAN_ACCESS_TOKEN is required")
	}

	natsURL := envOrDefault("NATS_URL", "nats://localhost:4222")
	dbURL := envOrDefault("DATABASE_URL", "postgres://resolute:resolute@localhost:5432/resolute?sslmode=disable")

	// ── Connect to TimescaleDB ──
	poolCfg, err := pgxpool.ParseConfig(dbURL)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to parse DATABASE_URL")
	}
	poolCfg.MaxConns = 10
	poolCfg.MinConns = 2
	poolCfg.MaxConnLifetime = 30 * time.Minute

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to connect to TimescaleDB")
	}
	defer pool.Close()

	// Verify DB connection.
	if err := pool.Ping(ctx); err != nil {
		log.Fatal().Err(err).Msg("failed to ping TimescaleDB")
	}
	log.Info().Str("db", dbURL).Msg("connected to TimescaleDB")

	// ── Connect to NATS ──
	pub, err := publisher.NewNATSPublisher(natsURL, log.Logger)
	if err != nil {
		log.Fatal().Err(err).Str("nats_url", natsURL).Msg("failed to connect to NATS")
	}
	defer pub.Close()

	// ── Create Dhan HTTP client ──
	dhanClient := dhan.NewClient(dhanBaseURL, dhanAccessToken)

	// ── Create and start poller ──
	p := poller.New(dhanClient, pool, pub, log.Logger)

	// ── Health endpoint ──
	healthSrv := health.NewServer(p.GetState(), log.Logger)
	healthSrv.Start()

	// ── Signal handling for graceful shutdown ──
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	// Run poller in background.
	go p.Run(ctx)

	// Wait for shutdown signal.
	sig := <-sigCh
	log.Info().Str("signal", sig.String()).Msg("received shutdown signal")
	cancel()

	// Give components time to clean up.
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	healthSrv.Shutdown(shutdownCtx)
	pub.Close()

	log.Info().Msg("equity_poller stopped")
}

func envOrDefault(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}
