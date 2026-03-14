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

// NSEFeedProvider is a stub/mock feed provider for NSE market data.
// In production this would connect to a broker WebSocket (Zerodha Kite, Upstox, etc.).
// For now it delegates to a paper-style generator with NSE-specific behaviour.
type NSEFeedProvider struct {
	mu             sync.RWMutex
	symbols        []SymbolConfig
	mapping        *SymbolMapping
	tickHandler    func(Tick)
	errorHandler   func(error)
	cancel         context.CancelFunc
	wg             sync.WaitGroup
	running        bool
	reconnectCount int

	// Per-symbol state.
	prices  map[string]float64
	volumes map[string]int64
	ois     map[string]int64
	opens   map[string]float64
	highs   map[string]float64
	lows    map[string]float64
	closes  map[string]float64
}

// NewNSEFeedProvider creates a new NSE feed provider.
// apiKey and accessToken are reserved for production broker integration.
func NewNSEFeedProvider(symbols []SymbolConfig, mapping *SymbolMapping, apiKey, accessToken string) *NSEFeedProvider {
	// Filter to NSE symbols only.
	var nseSymbols []SymbolConfig
	for _, s := range symbols {
		if s.Segment == SegmentNSEIndex || s.Segment == SegmentNSEFO {
			nseSymbols = append(nseSymbols, s)
		}
	}

	return &NSEFeedProvider{
		symbols: nseSymbols,
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

func (n *NSEFeedProvider) Connect(ctx context.Context) error {
	n.mu.Lock()
	defer n.mu.Unlock()

	if n.running {
		return fmt.Errorf("NSE feed already running")
	}

	// TODO: In production, establish WebSocket connection to broker here.
	// For now, generate synthetic data.
	log.Info().Msg("NSE feed provider connecting (stub/mock mode)")

	// Initialise state.
	for _, sym := range n.symbols {
		base := sym.BasePrice
		if base <= 0 {
			base = 100.0
		}
		n.prices[sym.Symbol] = base
		n.opens[sym.Symbol] = base
		n.highs[sym.Symbol] = base
		n.lows[sym.Symbol] = base
		n.closes[sym.Symbol] = base * (1.0 + (rand.Float64()-0.5)*0.005)
		n.volumes[sym.Symbol] = 0
		if sym.Segment == SegmentNSEFO {
			n.ois[sym.Symbol] = int64(rand.Intn(50000) + 10000)
		}
	}

	childCtx, cancel := context.WithCancel(ctx)
	n.cancel = cancel
	n.running = true

	n.wg.Add(1)
	go n.generateTicks(childCtx)

	log.Info().Int("symbol_count", len(n.symbols)).Msg("NSE feed provider connected")
	return nil
}

func (n *NSEFeedProvider) Subscribe(tokens []int32) error {
	log.Info().Ints32("tokens", tokens).Msg("NSE feed: subscribe request (stub)")
	return nil
}

func (n *NSEFeedProvider) Unsubscribe(tokens []int32) error {
	log.Info().Ints32("tokens", tokens).Msg("NSE feed: unsubscribe request (stub)")
	return nil
}

func (n *NSEFeedProvider) OnTick(handler func(Tick)) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.tickHandler = handler
}

func (n *NSEFeedProvider) OnError(handler func(error)) {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.errorHandler = handler
}

func (n *NSEFeedProvider) Close() error {
	n.mu.Lock()
	if n.cancel != nil {
		n.cancel()
	}
	n.running = false
	n.mu.Unlock()
	n.wg.Wait()
	log.Info().Msg("NSE feed provider closed")
	return nil
}

// ReconnectCount returns the number of reconnection attempts.
func (n *NSEFeedProvider) ReconnectCount() int {
	n.mu.RLock()
	defer n.mu.RUnlock()
	return n.reconnectCount
}

func (n *NSEFeedProvider) generateTicks(ctx context.Context) {
	defer n.wg.Done()
	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			n.emitAllTicks()
		}
	}
}

func (n *NSEFeedProvider) emitAllTicks() {
	n.mu.RLock()
	handler := n.tickHandler
	n.mu.RUnlock()

	if handler == nil {
		return
	}

	now := time.Now().UTC()
	for _, sym := range n.symbols {
		tick := n.generateTick(sym, now)
		handler(tick)
	}
}

func (n *NSEFeedProvider) generateTick(sym SymbolConfig, now time.Time) Tick {
	n.mu.Lock()
	defer n.mu.Unlock()

	current := n.prices[sym.Symbol]
	volatility := 0.0005
	if sym.Segment == SegmentNSEFO {
		volatility = 0.002
	}

	drift := (sym.BasePrice - current) / sym.BasePrice * 0.001
	change := drift + (rand.NormFloat64() * volatility * current)
	newPrice := math.Max(current+change, 0.05)
	newPrice = math.Round(newPrice*20) / 20

	n.prices[sym.Symbol] = newPrice
	if newPrice > n.highs[sym.Symbol] {
		n.highs[sym.Symbol] = newPrice
	}
	if newPrice < n.lows[sym.Symbol] {
		n.lows[sym.Symbol] = newPrice
	}

	n.volumes[sym.Symbol] += int64(rand.Intn(500) + 50)

	if sym.Segment == SegmentNSEFO {
		oiChange := int64(rand.Intn(201) - 100)
		newOI := n.ois[sym.Symbol] + oiChange
		if newOI < 0 {
			newOI = 0
		}
		n.ois[sym.Symbol] = newOI
	}

	spreadPct := 0.0005
	if sym.Segment == SegmentNSEFO {
		spreadPct = 0.005
	}
	halfSpread := newPrice * spreadPct / 2
	bid := math.Round((newPrice-halfSpread)*20) / 20
	ask := math.Round((newPrice+halfSpread)*20) / 20

	return Tick{
		Symbol:    sym.Symbol,
		Timestamp: now,
		Segment:   sym.Segment,
		LastPrice: newPrice,
		Open:      n.opens[sym.Symbol],
		High:      n.highs[sym.Symbol],
		Low:       n.lows[sym.Symbol],
		Close:     n.closes[sym.Symbol],
		Volume:    n.volumes[sym.Symbol],
		OI:        n.ois[sym.Symbol],
		Bid:       bid,
		Ask:       ask,
		BidQty:    int64(rand.Intn(500) + 50),
		AskQty:    int64(rand.Intn(500) + 50),
	}
}
