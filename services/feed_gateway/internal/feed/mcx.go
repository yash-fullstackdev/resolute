package feed

import (
	"context"
	"fmt"
	"math"
	"math/rand"
	"sync"
	"time"

	"github.com/rs/zerolog/log"
)

// MCXFeedProvider handles MCX commodity feed data.
// In production this connects to the broker WebSocket for MCX instruments.
// Currently operates in stub/mock mode generating synthetic data.
type MCXFeedProvider struct {
	mu             sync.RWMutex
	symbols        []SymbolConfig
	mapping        *SymbolMapping
	tickHandler    func(Tick)
	errorHandler   func(error)
	cancel         context.CancelFunc
	wg             sync.WaitGroup
	running        bool
	reconnectCount int

	prices  map[string]float64
	volumes map[string]int64
	ois     map[string]int64
	opens   map[string]float64
	highs   map[string]float64
	lows    map[string]float64
	closes  map[string]float64
}

// NewMCXFeedProvider creates a new MCX feed provider.
func NewMCXFeedProvider(symbols []SymbolConfig, mapping *SymbolMapping, apiKey, accessToken string) *MCXFeedProvider {
	var mcxSymbols []SymbolConfig
	for _, s := range symbols {
		if s.Segment == SegmentMCX {
			mcxSymbols = append(mcxSymbols, s)
		}
	}

	return &MCXFeedProvider{
		symbols: mcxSymbols,
		mapping: mapping,
		prices:  make(map[string]float64),
		volumes: make(map[string]int64),
		ois:     make(map[string]int64),
		opens:   make(map[string]float64),
		highs:   make(map[string]float64),
		lows:    make(map[string]float64),
		closes:  make(map[string]float64),
	}
}

func (m *MCXFeedProvider) Connect(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.running {
		return fmt.Errorf("MCX feed already running")
	}

	log.Info().Msg("MCX feed provider connecting (stub/mock mode)")

	for _, sym := range m.symbols {
		base := sym.BasePrice
		if base <= 0 {
			base = 100.0
		}
		m.prices[sym.Symbol] = base
		m.opens[sym.Symbol] = base
		m.highs[sym.Symbol] = base
		m.lows[sym.Symbol] = base
		m.closes[sym.Symbol] = base * (1.0 + (rand.Float64()-0.5)*0.005)
		m.volumes[sym.Symbol] = 0
		m.ois[sym.Symbol] = int64(rand.Intn(20000) + 5000)
	}

	childCtx, cancel := context.WithCancel(ctx)
	m.cancel = cancel
	m.running = true

	m.wg.Add(1)
	go m.generateTicks(childCtx)

	log.Info().Int("symbol_count", len(m.symbols)).Msg("MCX feed provider connected")
	return nil
}

func (m *MCXFeedProvider) Subscribe(tokens []int32) error {
	log.Info().Ints32("tokens", tokens).Msg("MCX feed: subscribe request (stub)")
	return nil
}

func (m *MCXFeedProvider) Unsubscribe(tokens []int32) error {
	log.Info().Ints32("tokens", tokens).Msg("MCX feed: unsubscribe request (stub)")
	return nil
}

func (m *MCXFeedProvider) OnTick(handler func(Tick)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.tickHandler = handler
}

func (m *MCXFeedProvider) OnError(handler func(error)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.errorHandler = handler
}

func (m *MCXFeedProvider) Close() error {
	m.mu.Lock()
	if m.cancel != nil {
		m.cancel()
	}
	m.running = false
	m.mu.Unlock()
	m.wg.Wait()
	log.Info().Msg("MCX feed provider closed")
	return nil
}

// ReconnectCount returns the number of reconnection attempts.
func (m *MCXFeedProvider) ReconnectCount() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.reconnectCount
}

func (m *MCXFeedProvider) generateTicks(ctx context.Context) {
	defer m.wg.Done()
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			m.emitAllTicks()
		}
	}
}

func (m *MCXFeedProvider) emitAllTicks() {
	m.mu.RLock()
	handler := m.tickHandler
	m.mu.RUnlock()

	if handler == nil {
		return
	}

	now := time.Now().UTC()
	for _, sym := range m.symbols {
		tick := m.generateTick(sym, now)
		handler(tick)
	}
}

func (m *MCXFeedProvider) generateTick(sym SymbolConfig, now time.Time) Tick {
	m.mu.Lock()
	defer m.mu.Unlock()

	current := m.prices[sym.Symbol]
	volatility := 0.001 // Commodities moderate volatility.

	drift := (sym.BasePrice - current) / sym.BasePrice * 0.001
	change := drift + (rand.NormFloat64() * volatility * current)
	newPrice := math.Max(current+change, 0.05)
	newPrice = math.Round(newPrice*20) / 20

	m.prices[sym.Symbol] = newPrice
	if newPrice > m.highs[sym.Symbol] {
		m.highs[sym.Symbol] = newPrice
	}
	if newPrice < m.lows[sym.Symbol] {
		m.lows[sym.Symbol] = newPrice
	}

	m.volumes[sym.Symbol] += int64(rand.Intn(300) + 20)

	oiChange := int64(rand.Intn(201) - 100)
	newOI := m.ois[sym.Symbol] + oiChange
	if newOI < 0 {
		newOI = 0
	}
	m.ois[sym.Symbol] = newOI

	spreadPct := 0.001
	halfSpread := newPrice * spreadPct / 2
	bid := math.Round((newPrice-halfSpread)*20) / 20
	ask := math.Round((newPrice+halfSpread)*20) / 20

	return Tick{
		Symbol:    sym.Symbol,
		Timestamp: now,
		Segment:   sym.Segment,
		LastPrice: newPrice,
		Open:      m.opens[sym.Symbol],
		High:      m.highs[sym.Symbol],
		Low:       m.lows[sym.Symbol],
		Close:     m.closes[sym.Symbol],
		Volume:    m.volumes[sym.Symbol],
		OI:        m.ois[sym.Symbol],
		Bid:       bid,
		Ask:       ask,
		BidQty:    int64(rand.Intn(300) + 20),
		AskQty:    int64(rand.Intn(300) + 20),
	}
}
