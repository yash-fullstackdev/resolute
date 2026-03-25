package poller

import (
	"sync"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/derived"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

// State holds all in-memory state for the polling loop.
// It is not shared across goroutines — only the poller goroutine mutates it.
type State struct {
	mu sync.Mutex

	// Per-symbol running VWAP / delta state, keyed by security_id.
	DerivedState map[string]*derived.RunningState

	// Symbol map: security_id -> symbol name. Loaded from equity_instruments.
	SymbolMap map[string]string

	// Active security IDs to poll.
	SecurityIDs []string

	// Last completed bucket number.
	LastBucket uint16

	// Trading date (YYYY-MM-DD).
	TradingDate string

	// Consecutive API failures.
	ConsecutiveFailures int

	// Last cycle metrics.
	LastCycleMetrics types.CycleMetrics
}

// NewState creates an empty polling state.
func NewState() *State {
	return &State{
		DerivedState: make(map[string]*derived.RunningState),
		SymbolMap:    make(map[string]string),
	}
}

// Reset clears all daily state for a new trading day.
func (s *State) Reset(tradingDate string) {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.DerivedState = make(map[string]*derived.RunningState)
	s.LastBucket = 0
	s.TradingDate = tradingDate
	s.ConsecutiveFailures = 0
	s.LastCycleMetrics = types.CycleMetrics{}
}

// GetDerivedState returns the running state for a security, creating it if needed.
func (s *State) GetDerivedState(securityID string) *derived.RunningState {
	rs, ok := s.DerivedState[securityID]
	if !ok {
		rs = &derived.RunningState{}
		s.DerivedState[securityID] = rs
	}
	return rs
}

// SetLastMetrics stores the latest cycle metrics (used by health endpoint).
func (s *State) SetLastMetrics(m types.CycleMetrics) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.LastCycleMetrics = m
}

// GetLastMetrics returns the latest cycle metrics.
func (s *State) GetLastMetrics() types.CycleMetrics {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.LastCycleMetrics
}
