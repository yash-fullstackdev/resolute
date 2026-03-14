package main

import (
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog"

	_ "github.com/jackc/pgx/v5/stdlib"

	"github.com/resolute/india-options-builder/services/order_router/internal/executor"
	"github.com/resolute/india-options-builder/services/order_router/internal/pool"
	"github.com/resolute/india-options-builder/services/order_router/internal/session"
	"github.com/resolute/india-options-builder/services/order_router/internal/state"
)

func main() {
	// Initialize zerolog
	log := zerolog.New(os.Stdout).With().
		Timestamp().
		Str("service", "order_router").
		Logger()

	log.Info().Msg("starting order_router service")

	// ── NATS connection ──────────────────────────────────────────────
	natsURL := getEnv("NATS_URL", "nats://localhost:4222")
	nc, err := nats.Connect(natsURL,
		nats.Name("order_router"),
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*time.Second),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			log.Warn().Err(err).Msg("NATS disconnected")
		}),
		nats.ReconnectHandler(func(_ *nats.Conn) {
			log.Info().Msg("NATS reconnected")
		}),
		nats.ClosedHandler(func(_ *nats.Conn) {
			log.Info().Msg("NATS connection closed")
		}),
	)
	if err != nil {
		log.Fatal().Err(err).Str("url", natsURL).Msg("failed to connect to NATS")
	}
	defer nc.Close()
	log.Info().Str("url", natsURL).Msg("connected to NATS")

	// ── Database connection (optional — graceful degradation if unavailable) ──
	var db *sql.DB
	dbURL := os.Getenv("DATABASE_URL")
	if dbURL != "" {
		db, err = sql.Open("pgx", dbURL)
		if err != nil {
			log.Warn().Err(err).Msg("failed to open database connection, running without persistence")
		} else {
			db.SetMaxOpenConns(20)
			db.SetMaxIdleConns(5)
			db.SetConnMaxLifetime(5 * time.Minute)

			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			if pingErr := db.PingContext(ctx); pingErr != nil {
				log.Warn().Err(pingErr).Msg("database ping failed, running without persistence")
				db.Close()
				db = nil
			} else {
				log.Info().Msg("connected to TimescaleDB")
			}
			cancel()
		}
	} else {
		log.Info().Msg("no DATABASE_URL set, running without persistence")
	}
	if db != nil {
		defer db.Close()
	}

	// ── Initialize components ────────────────────────────────────────

	// Position state manager
	positionState := state.NewPositionState(nc, log)
	if err := positionState.SubscribeQueries(); err != nil {
		log.Fatal().Err(err).Msg("failed to subscribe position queries")
	}

	// Order executor
	exec := executor.NewOrderExecutor(nc, db, positionState, log)

	// Broker pool
	brokerPool := pool.NewBrokerPool(nc, exec, log)
	exec.SetClientGetter(brokerPool)

	// Subscribe to worker lifecycle events
	if err := brokerPool.SubscribeLifecycleEvents(); err != nil {
		log.Fatal().Err(err).Msg("failed to subscribe to lifecycle events")
	}

	// Token refresher
	tokenRefresher, err := session.NewTokenRefresher(brokerPool, nc, log)
	if err != nil {
		log.Fatal().Err(err).Msg("failed to create token refresher")
	}
	tokenRefresher.Start()

	// ── Prometheus metrics server ────────────────────────────────────
	metricsAddr := getEnv("METRICS_ADDR", ":9091")
	metricsMux := http.NewServeMux()
	metricsMux.Handle("/metrics", promhttp.Handler())
	metricsMux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, `{"status":"ok","service":"order_router","nats_connected":%t}`, nc.IsConnected())
	})

	metricsServer := &http.Server{
		Addr:         metricsAddr,
		Handler:      metricsMux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
	}

	go func() {
		log.Info().Str("addr", metricsAddr).Msg("starting metrics server")
		if err := metricsServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal().Err(err).Msg("metrics server failed")
		}
	}()

	// ── Graceful shutdown ────────────────────────────────────────────
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	sig := <-sigCh
	log.Info().Str("signal", sig.String()).Msg("received shutdown signal")

	// Stop token refresher
	tokenRefresher.Stop()

	// Shutdown broker pool (unsubscribe all, clean up clients)
	brokerPool.Shutdown()

	// Drain NATS connection
	if err := nc.Drain(); err != nil {
		log.Warn().Err(err).Msg("error draining NATS connection")
	}

	// Shutdown metrics server
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()
	if err := metricsServer.Shutdown(shutdownCtx); err != nil {
		log.Warn().Err(err).Msg("error shutting down metrics server")
	}

	log.Info().Msg("order_router service stopped")
}

func getEnv(key, fallback string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return fallback
}
