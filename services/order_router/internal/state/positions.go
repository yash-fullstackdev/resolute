package state

import (
	"encoding/json"
	"fmt"
	"sync"

	"github.com/nats-io/nats.go"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/order_router/internal/broker"
)

// PositionState maintains in-memory position state keyed by tenant_id -> []Position.
// It responds to NATS request-reply on orders.query.positions.{tenant_id}.
type PositionState struct {
	positions map[string][]broker.Position // keyed by tenant_id
	mu        sync.RWMutex
	nc        *nats.Conn
	log       zerolog.Logger
}

// NewPositionState creates a new PositionState manager.
func NewPositionState(nc *nats.Conn, log zerolog.Logger) *PositionState {
	return &PositionState{
		positions: make(map[string][]broker.Position),
		nc:        nc,
		log:       log.With().Str("component", "position_state").Logger(),
	}
}

// SubscribeQueries subscribes to NATS request-reply for position queries.
// Subject: orders.query.positions.{tenant_id}
func (ps *PositionState) SubscribeQueries() error {
	_, err := ps.nc.Subscribe("orders.query.positions.*", func(msg *nats.Msg) {
		tenantID := extractLastSegment(msg.Subject)
		if tenantID == "" {
			ps.log.Error().Str("subject", msg.Subject).Msg("could not extract tenant_id")
			ps.respondError(msg, "invalid subject")
			return
		}

		positions := ps.GetPositions(tenantID)

		data, err := json.Marshal(positions)
		if err != nil {
			ps.log.Error().Err(err).Str("tenant_id", tenantID).Msg("failed to marshal positions")
			ps.respondError(msg, "marshal error")
			return
		}

		if err := msg.Respond(data); err != nil {
			ps.log.Error().Err(err).Str("tenant_id", tenantID).Msg("failed to respond to position query")
		}

		ps.log.Debug().
			Str("tenant_id", tenantID).
			Int("positions", len(positions)).
			Msg("responded to position query")
	})
	if err != nil {
		return fmt.Errorf("subscribe orders.query.positions.*: %w", err)
	}

	ps.log.Info().Msg("subscribed to position queries")
	return nil
}

// AddPosition adds or updates a position for a tenant.
func (ps *PositionState) AddPosition(tenantID string, position broker.Position) {
	ps.mu.Lock()
	defer ps.mu.Unlock()

	ps.positions[tenantID] = append(ps.positions[tenantID], position)

	ps.log.Info().
		Str("tenant_id", tenantID).
		Str("position_id", position.ID).
		Str("strategy", position.StrategyName).
		Str("underlying", position.Underlying).
		Str("status", position.Status).
		Msg("position added")
}

// UpdatePosition updates an existing position for a tenant.
func (ps *PositionState) UpdatePosition(tenantID, positionID string, updateFn func(*broker.Position)) bool {
	ps.mu.Lock()
	defer ps.mu.Unlock()

	positions, exists := ps.positions[tenantID]
	if !exists {
		return false
	}

	for i := range positions {
		if positions[i].ID == positionID {
			updateFn(&positions[i])
			ps.positions[tenantID] = positions
			return true
		}
	}

	return false
}

// GetPositions returns all positions for a tenant.
func (ps *PositionState) GetPositions(tenantID string) []broker.Position {
	ps.mu.RLock()
	defer ps.mu.RUnlock()

	positions, exists := ps.positions[tenantID]
	if !exists {
		return []broker.Position{}
	}

	// Return a copy to avoid race conditions
	result := make([]broker.Position, len(positions))
	copy(result, positions)
	return result
}

// GetOpenPositions returns only open positions for a tenant.
func (ps *PositionState) GetOpenPositions(tenantID string) []broker.Position {
	ps.mu.RLock()
	defer ps.mu.RUnlock()

	positions, exists := ps.positions[tenantID]
	if !exists {
		return []broker.Position{}
	}

	open := make([]broker.Position, 0)
	for _, p := range positions {
		if p.Status == "OPEN" {
			open = append(open, p)
		}
	}
	return open
}

// ClosePosition marks a position as closed with the given status.
func (ps *PositionState) ClosePosition(tenantID, positionID, status string, realisedPnL float64) bool {
	return ps.UpdatePosition(tenantID, positionID, func(p *broker.Position) {
		p.Status = status
		p.RealisedPnL = realisedPnL
	})
}

// RemoveTenant removes all positions for a tenant (on worker.stopped).
func (ps *PositionState) RemoveTenant(tenantID string) {
	ps.mu.Lock()
	defer ps.mu.Unlock()

	delete(ps.positions, tenantID)
	ps.log.Info().Str("tenant_id", tenantID).Msg("removed all positions for tenant")
}

// GetAllTenantPositionCounts returns a map of tenant_id -> position count (for metrics).
func (ps *PositionState) GetAllTenantPositionCounts() map[string]int {
	ps.mu.RLock()
	defer ps.mu.RUnlock()

	counts := make(map[string]int, len(ps.positions))
	for tenantID, positions := range ps.positions {
		openCount := 0
		for _, p := range positions {
			if p.Status == "OPEN" {
				openCount++
			}
		}
		counts[tenantID] = openCount
	}
	return counts
}

func (ps *PositionState) respondError(msg *nats.Msg, errMsg string) {
	resp := map[string]string{"error": errMsg}
	data, _ := json.Marshal(resp)
	_ = msg.Respond(data)
}

// extractLastSegment extracts the last segment from a dot-separated NATS subject.
func extractLastSegment(subject string) string {
	for i := len(subject) - 1; i >= 0; i-- {
		if subject[i] == '.' {
			return subject[i+1:]
		}
	}
	return subject
}
