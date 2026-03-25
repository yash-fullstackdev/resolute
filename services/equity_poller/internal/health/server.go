package health

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

// MetricsProvider returns the latest cycle metrics.
type MetricsProvider interface {
	GetLastMetrics() types.CycleMetrics
}

// Server serves the /health JSON endpoint on port 8081.
type Server struct {
	mu        sync.RWMutex
	startTime time.Time
	provider  MetricsProvider
	logger    zerolog.Logger
	server    *http.Server
}

// NewServer creates a health server.
func NewServer(provider MetricsProvider, logger zerolog.Logger) *Server {
	return &Server{
		startTime: time.Now(),
		provider:  provider,
		logger:    logger.With().Str("component", "health").Logger(),
	}
}

// healthResponse is the JSON shape returned by the health endpoint.
type healthResponse struct {
	Status          string `json:"status"`
	LastCycleMs     int64  `json:"last_cycle_ms"`
	StocksProcessed int    `json:"stocks_processed"`
	Errors          int    `json:"errors"`
	UptimeSec       int64  `json:"uptime_sec"`
}

// Start begins serving the health endpoint in a background goroutine.
func (s *Server) Start() {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)

	s.server = &http.Server{
		Addr:         ":8081",
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	}

	go func() {
		s.logger.Info().Str("addr", ":8081").Msg("health server started")
		if err := s.server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			s.logger.Error().Err(err).Msg("health server error")
		}
	}()
}

// Shutdown gracefully shuts down the health server.
func (s *Server) Shutdown(ctx context.Context) {
	if s.server != nil {
		if err := s.server.Shutdown(ctx); err != nil {
			s.logger.Error().Err(err).Msg("health server shutdown error")
		}
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	metrics := s.provider.GetLastMetrics()

	resp := healthResponse{
		Status:          "ok",
		LastCycleMs:     metrics.CycleTotalMs,
		StocksProcessed: metrics.StocksProcessed,
		Errors:          metrics.Errors,
		UptimeSec:       int64(time.Since(s.startTime).Seconds()),
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(resp)
}
