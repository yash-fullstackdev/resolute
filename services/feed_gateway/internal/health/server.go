package health

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/rs/zerolog/log"
)

// Status holds the current health state of the feed gateway.
type Status struct {
	mu              sync.RWMutex
	feedConnected   bool
	lastTickTime    time.Time
	reconnectCount  int
	startTime       time.Time
	totalTicks      int64
}

// NewStatus creates a new health status tracker.
func NewStatus() *Status {
	return &Status{
		startTime: time.Now(),
	}
}

// SetFeedConnected updates the feed connection state.
func (s *Status) SetFeedConnected(connected bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.feedConnected = connected
}

// SetLastTickTime records when the last tick was received.
func (s *Status) SetLastTickTime(t time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastTickTime = t
}

// IncrementReconnectCount bumps the reconnection counter.
func (s *Status) IncrementReconnectCount() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reconnectCount++
}

// IncrementTotalTicks increments the total tick counter.
func (s *Status) IncrementTotalTicks() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.totalTicks++
}

// TotalTicks returns the total number of ticks processed.
func (s *Status) TotalTicks() int64 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.totalTicks
}

// Uptime returns the service uptime.
func (s *Status) Uptime() time.Duration {
	return time.Since(s.startTime)
}

// snapshot returns the current health state as a map.
func (s *Status) snapshot() map[string]interface{} {
	s.mu.RLock()
	defer s.mu.RUnlock()

	lastTick := ""
	if !s.lastTickTime.IsZero() {
		lastTick = s.lastTickTime.Format(time.RFC3339Nano)
	}

	return map[string]interface{}{
		"service":          "feed_gateway",
		"status":           s.overallStatus(),
		"feed_connected":   s.feedConnected,
		"last_tick_time":   lastTick,
		"reconnect_count":  s.reconnectCount,
		"total_ticks":      s.totalTicks,
		"uptime_seconds":   int64(time.Since(s.startTime).Seconds()),
		"timestamp":        time.Now().UTC().Format(time.RFC3339Nano),
	}
}

func (s *Status) overallStatus() string {
	if !s.feedConnected {
		return "degraded"
	}
	if !s.lastTickTime.IsZero() && time.Since(s.lastTickTime) > 30*time.Second {
		return "degraded"
	}
	return "healthy"
}

// Server provides HTTP health check and Prometheus metrics endpoints.
type Server struct {
	healthServer  *http.Server
	metricsServer *http.Server
	status        *Status
}

// NewServer creates health check (port 8080) and metrics (port 9090) HTTP servers.
func NewServer(status *Status) *Server {
	healthMux := http.NewServeMux()
	metricsMux := http.NewServeMux()

	s := &Server{
		status: status,
		healthServer: &http.Server{
			Addr:         ":8080",
			Handler:      healthMux,
			ReadTimeout:  5 * time.Second,
			WriteTimeout: 5 * time.Second,
		},
		metricsServer: &http.Server{
			Addr:         ":9090",
			Handler:      metricsMux,
			ReadTimeout:  5 * time.Second,
			WriteTimeout: 10 * time.Second,
		},
	}

	healthMux.HandleFunc("/health", s.handleHealth)
	healthMux.HandleFunc("/ready", s.handleReady)
	metricsMux.Handle("/metrics", promhttp.Handler())

	return s
}

// Start launches both HTTP servers in background goroutines.
func (s *Server) Start() {
	go func() {
		log.Info().Str("addr", s.healthServer.Addr).Msg("health check server starting")
		if err := s.healthServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error().Err(err).Msg("health check server error")
		}
	}()

	go func() {
		log.Info().Str("addr", s.metricsServer.Addr).Msg("prometheus metrics server starting")
		if err := s.metricsServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Error().Err(err).Msg("metrics server error")
		}
	}()
}

// Shutdown gracefully stops both HTTP servers.
func (s *Server) Shutdown(ctx context.Context) {
	if err := s.healthServer.Shutdown(ctx); err != nil {
		log.Error().Err(err).Msg("health server shutdown error")
	}
	if err := s.metricsServer.Shutdown(ctx); err != nil {
		log.Error().Err(err).Msg("metrics server shutdown error")
	}
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	snap := s.status.snapshot()

	w.Header().Set("Content-Type", "application/json")
	status := snap["status"].(string)
	if status != "healthy" {
		w.WriteHeader(http.StatusServiceUnavailable)
	} else {
		w.WriteHeader(http.StatusOK)
	}
	json.NewEncoder(w).Encode(snap)
}

func (s *Server) handleReady(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	s.status.mu.RLock()
	connected := s.status.feedConnected
	s.status.mu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	if connected {
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]string{"status": "ready"})
	} else {
		w.WriteHeader(http.StatusServiceUnavailable)
		json.NewEncoder(w).Encode(map[string]string{"status": "not_ready"})
	}
}
