package publisher

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/rs/zerolog/log"

	"github.com/resolute/india-options-builder/services/feed_gateway/internal/feed"
)

// NATS subject constants.
const (
	SubjectHeartbeat  = "heartbeat.feed_gateway"
	SubjectStaleFeed  = "alerts.stale_feed"
)

// Prometheus metrics for the publisher.
var (
	ticksPublished = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "feed_gateway_tick_count_total",
		Help: "Total number of ticks published to NATS",
	}, []string{"segment", "symbol"})

	publishLatency = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "feed_gateway_publish_latency_ms",
		Help:    "Latency of publishing ticks to NATS in milliseconds",
		Buckets: []float64{0.1, 0.5, 1, 2, 5, 10, 25, 50, 100},
	}, []string{"segment"})

	reconnectCount = promauto.NewCounter(prometheus.CounterOpts{
		Name: "feed_gateway_reconnect_count",
		Help: "Number of NATS reconnection attempts",
	})
)

// NATSPublisher publishes ticks to NATS subjects following the spec schema.
type NATSPublisher struct {
	conn *nats.Conn
	mu   sync.RWMutex

	// Stale feed detection state.
	lastTickTime   map[string]time.Time
	staleMu        sync.Mutex
}

// NewNATSPublisher creates a publisher connected to the given NATS URL.
// It configures automatic reconnection with exponential backoff (max 30s).
func NewNATSPublisher(natsURL string) (*NATSPublisher, error) {
	np := &NATSPublisher{
		lastTickTime: make(map[string]time.Time),
	}

	opts := []nats.Option{
		nats.Name("feed_gateway"),
		nats.ReconnectWait(1 * time.Second),
		nats.MaxReconnects(-1), // Unlimited reconnects.
		nats.CustomReconnectDelay(func(attempts int) time.Duration {
			// Exponential backoff: 1s, 2s, 4s, 8s, 16s, 30s (capped).
			delay := time.Duration(1<<uint(attempts)) * time.Second
			if delay > 30*time.Second {
				delay = 30 * time.Second
			}
			return delay
		}),
		nats.DisconnectErrHandler(func(nc *nats.Conn, err error) {
			log.Warn().Err(err).Msg("NATS disconnected")
		}),
		nats.ReconnectHandler(func(nc *nats.Conn) {
			reconnectCount.Inc()
			log.Info().Str("url", nc.ConnectedUrl()).Msg("NATS reconnected")
		}),
		nats.ClosedHandler(func(nc *nats.Conn) {
			log.Warn().Msg("NATS connection closed")
		}),
		nats.ErrorHandler(func(nc *nats.Conn, sub *nats.Subscription, err error) {
			log.Error().Err(err).Msg("NATS async error")
		}),
	}

	conn, err := nats.Connect(natsURL, opts...)
	if err != nil {
		return nil, fmt.Errorf("failed to connect to NATS at %s: %w", natsURL, err)
	}

	np.conn = conn
	log.Info().Str("url", natsURL).Msg("NATS publisher connected")
	return np, nil
}

// Publish sends a single tick to the appropriate NATS subject.
func (np *NATSPublisher) Publish(subject string, tick feed.Tick) error {
	start := time.Now()

	data, err := json.Marshal(tick)
	if err != nil {
		return fmt.Errorf("failed to marshal tick: %w", err)
	}

	err = np.conn.Publish(subject, data)
	if err != nil {
		return fmt.Errorf("failed to publish to %s: %w", subject, err)
	}

	elapsed := float64(time.Since(start).Microseconds()) / 1000.0
	publishLatency.WithLabelValues(tick.Segment).Observe(elapsed)
	ticksPublished.WithLabelValues(tick.Segment, tick.Symbol).Inc()

	// Track last tick time for stale detection.
	np.staleMu.Lock()
	np.lastTickTime[tick.Symbol] = time.Now()
	np.staleMu.Unlock()

	return nil
}

// PublishBatch publishes multiple ticks efficiently, deriving subjects automatically.
func (np *NATSPublisher) PublishBatch(ticks []feed.Tick) error {
	var firstErr error
	for _, tick := range ticks {
		subject := feed.DeriveNATSSubject(tick)
		if err := np.Publish(subject, tick); err != nil {
			if firstErr == nil {
				firstErr = err
			}
			log.Error().Err(err).Str("symbol", tick.Symbol).Msg("failed to publish tick")
		}
	}
	return firstErr
}

// PublishHeartbeat sends a heartbeat message with feed health stats.
func (np *NATSPublisher) PublishHeartbeat(symbolCount int, totalTicks int64, uptime time.Duration) error {
	hb := map[string]interface{}{
		"service":      "feed_gateway",
		"timestamp":    time.Now().UTC().Format(time.RFC3339Nano),
		"symbol_count": symbolCount,
		"total_ticks":  totalTicks,
		"uptime_s":     int64(uptime.Seconds()),
		"status":       "healthy",
	}

	data, err := json.Marshal(hb)
	if err != nil {
		return fmt.Errorf("failed to marshal heartbeat: %w", err)
	}

	return np.conn.Publish(SubjectHeartbeat, data)
}

// PublishStaleFeedAlert publishes an alert when a symbol's feed is stale.
func (np *NATSPublisher) PublishStaleFeedAlert(alert feed.StaleFeedAlert) error {
	data, err := json.Marshal(alert)
	if err != nil {
		return fmt.Errorf("failed to marshal stale feed alert: %w", err)
	}

	return np.conn.Publish(SubjectStaleFeed, data)
}

// GetLastTickTime returns the last tick time for a given symbol.
func (np *NATSPublisher) GetLastTickTime(symbol string) (time.Time, bool) {
	np.staleMu.Lock()
	defer np.staleMu.Unlock()
	t, ok := np.lastTickTime[symbol]
	return t, ok
}

// Conn returns the underlying NATS connection for subscribing to subjects.
func (np *NATSPublisher) Conn() *nats.Conn {
	np.mu.RLock()
	defer np.mu.RUnlock()
	return np.conn
}

// Close flushes pending messages and closes the NATS connection.
func (np *NATSPublisher) Close() error {
	if np.conn != nil {
		if err := np.conn.Flush(); err != nil {
			log.Warn().Err(err).Msg("failed to flush NATS on close")
		}
		np.conn.Close()
		log.Info().Msg("NATS publisher closed")
	}
	return nil
}
