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

// PaperFeedProvider generates synthetic ticks for development and testing.
// It produces realistic random-walk prices at 1-second intervals for all
// configured symbols, complete with bid/ask spreads and OI simulation.
type PaperFeedProvider struct {
	mu            sync.RWMutex
	symbols       []SymbolConfig
	tickHandler   func(Tick)
	errorHandler  func(error)
	cancel        context.CancelFunc
	wg            sync.WaitGroup
	running       bool

	// Per-symbol state for random walk.
	prices   map[string]float64
	volumes  map[string]int64
	ois      map[string]int64
	opens    map[string]float64
	highs    map[string]float64
	lows     map[string]float64
	closes   map[string]float64
}

// NewPaperFeedProvider creates a paper feed that generates ticks for the given symbols.
func NewPaperFeedProvider(symbols []SymbolConfig) *PaperFeedProvider {
	return &PaperFeedProvider{
		symbols:  symbols,
		prices:   make(map[string]float64),
		volumes:  make(map[string]int64),
		ois:      make(map[string]int64),
		opens:    make(map[string]float64),
		highs:    make(map[string]float64),
		lows:     make(map[string]float64),
		closes:   make(map[string]float64),
	}
}

func (p *PaperFeedProvider) Connect(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		return fmt.Errorf("paper feed already running")
	}

	// Initialise per-symbol state.
	for _, sym := range p.symbols {
		base := sym.BasePrice
		if base <= 0 {
			base = 100.0
		}
		p.prices[sym.Symbol] = base
		p.opens[sym.Symbol] = base
		p.highs[sym.Symbol] = base
		p.lows[sym.Symbol] = base
		p.closes[sym.Symbol] = base * (1.0 + (rand.Float64()-0.5)*0.005) // Prev close ±0.25%
		p.volumes[sym.Symbol] = 0
		if sym.Segment == SegmentNSEFO || sym.Segment == SegmentMCX {
			p.ois[sym.Symbol] = int64(rand.Intn(50000) + 10000)
		}
	}

	childCtx, cancel := context.WithCancel(ctx)
	p.cancel = cancel
	p.running = true

	p.wg.Add(1)
	go p.generateTicks(childCtx)

	log.Info().Int("symbol_count", len(p.symbols)).Msg("paper feed provider connected")
	return nil
}

func (p *PaperFeedProvider) Subscribe(_ []int32) error {
	// Paper feed subscribes to all configured symbols at connect time.
	return nil
}

func (p *PaperFeedProvider) Unsubscribe(_ []int32) error {
	return nil
}

func (p *PaperFeedProvider) OnTick(handler func(Tick)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.tickHandler = handler
}

func (p *PaperFeedProvider) OnError(handler func(error)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.errorHandler = handler
}

func (p *PaperFeedProvider) Close() error {
	p.mu.Lock()
	if p.cancel != nil {
		p.cancel()
	}
	p.running = false
	p.mu.Unlock()
	p.wg.Wait()
	log.Info().Msg("paper feed provider closed")
	return nil
}

func (p *PaperFeedProvider) generateTicks(ctx context.Context) {
	defer p.wg.Done()

	ticker := time.NewTicker(1 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.emitAllTicks()
		}
	}
}

func (p *PaperFeedProvider) emitAllTicks() {
	p.mu.RLock()
	handler := p.tickHandler
	p.mu.RUnlock()

	if handler == nil {
		return
	}

	now := time.Now().UTC()

	for _, sym := range p.symbols {
		tick := p.generateTickForSymbol(sym, now)
		handler(tick)
	}
}

func (p *PaperFeedProvider) generateTickForSymbol(sym SymbolConfig, now time.Time) Tick {
	p.mu.Lock()
	defer p.mu.Unlock()

	current := p.prices[sym.Symbol]

	// Random walk: drift ±0.05% per tick with slight mean reversion toward base.
	volatility := 0.0005
	switch sym.Segment {
	case SegmentNSEFO:
		volatility = 0.002 // Options are more volatile.
	case SegmentMCX:
		volatility = 0.001
	}

	// Mean-reverting random walk.
	drift := (sym.BasePrice - current) / sym.BasePrice * 0.001 // Slight pull toward base.
	change := drift + (rand.NormFloat64() * volatility * current)
	newPrice := math.Max(current+change, 0.05) // Never go below 0.05.
	newPrice = math.Round(newPrice*20) / 20     // Round to 0.05 tick size.

	// Update OHLC.
	p.prices[sym.Symbol] = newPrice
	if newPrice > p.highs[sym.Symbol] {
		p.highs[sym.Symbol] = newPrice
	}
	if newPrice < p.lows[sym.Symbol] {
		p.lows[sym.Symbol] = newPrice
	}

	// Volume: random increment.
	volIncrement := int64(rand.Intn(500) + 50)
	p.volumes[sym.Symbol] += volIncrement

	// OI simulation for derivatives.
	if sym.Segment == SegmentNSEFO || sym.Segment == SegmentMCX {
		oiChange := int64(rand.Intn(201) - 100) // ±100 lots.
		newOI := p.ois[sym.Symbol] + oiChange
		if newOI < 0 {
			newOI = 0
		}
		p.ois[sym.Symbol] = newOI
	}

	// Bid/Ask spread: ~0.05% of price for indices, wider for options.
	spreadPct := 0.0005
	if sym.Segment == SegmentNSEFO {
		spreadPct = 0.005
	} else if sym.Segment == SegmentMCX {
		spreadPct = 0.001
	}
	halfSpread := newPrice * spreadPct / 2
	bid := math.Round((newPrice-halfSpread)*20) / 20
	ask := math.Round((newPrice+halfSpread)*20) / 20

	tick := Tick{
		Symbol:    sym.Symbol,
		Timestamp: now,
		Segment:   sym.Segment,
		LastPrice: newPrice,
		Open:      p.opens[sym.Symbol],
		High:      p.highs[sym.Symbol],
		Low:       p.lows[sym.Symbol],
		Close:     p.closes[sym.Symbol],
		Volume:    p.volumes[sym.Symbol],
		OI:        p.ois[sym.Symbol],
		Bid:       bid,
		Ask:       ask,
		BidQty:    int64(rand.Intn(500) + 50),
		AskQty:    int64(rand.Intn(500) + 50),
	}

	return tick
}
