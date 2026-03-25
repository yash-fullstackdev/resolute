package publisher

import (
	"encoding/json"
	"fmt"

	"github.com/nats-io/nats.go"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

const (
	// SubjectBatch is the NATS subject for batch snapshot messages.
	SubjectBatch = "equity.snapshots.batch"
)

// NATSPublisher publishes enriched snapshot batches to NATS.
type NATSPublisher struct {
	conn   *nats.Conn
	logger zerolog.Logger
}

// NewNATSPublisher connects to NATS and returns a publisher.
func NewNATSPublisher(natsURL string, logger zerolog.Logger) (*NATSPublisher, error) {
	nc, err := nats.Connect(natsURL,
		nats.Name("equity_poller"),
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*1e9), // 2 seconds
	)
	if err != nil {
		return nil, fmt.Errorf("NATS connect: %w", err)
	}

	logger.Info().Str("nats_url", natsURL).Msg("connected to NATS")

	return &NATSPublisher{
		conn:   nc,
		logger: logger,
	}, nil
}

// PublishBatch serializes and publishes a BatchMessage to NATS.
func (p *NATSPublisher) PublishBatch(batch types.BatchMessage) error {
	data, err := json.Marshal(batch)
	if err != nil {
		return fmt.Errorf("marshal batch: %w", err)
	}

	if err := p.conn.Publish(SubjectBatch, data); err != nil {
		return fmt.Errorf("publish to %s: %w", SubjectBatch, err)
	}

	return nil
}

// Close drains and closes the NATS connection.
func (p *NATSPublisher) Close() {
	if p.conn != nil {
		p.conn.Drain()
	}
}
