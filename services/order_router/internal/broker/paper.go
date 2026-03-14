package broker

import (
	"context"
	"fmt"
	"math/rand"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/rs/zerolog"
)

const (
	paperDefaultSlippage = 0.001 // 0.1% default slippage
	paperMinFillTimeMs   = 50
	paperMaxFillTimeMs   = 300
)

// PaperOrder tracks a simulated order in the paper broker.
type PaperOrder struct {
	ID           string
	Leg          OrderLeg
	Status       string
	FilledQty    int
	AveragePrice float64
	CreatedAt    time.Time
	FilledAt     *time.Time
}

// PaperPosition tracks a simulated position in the paper broker.
type PaperPosition struct {
	Symbol       string
	Exchange     string
	Quantity     int
	AveragePrice float64
	LastPrice    float64
	PnL          float64
	Product      string
}

// PaperBroker implements BrokerClient for paper/simulated trading.
// Fully functional: simulates fills at mid-price with configurable slippage,
// random fill times, and in-memory position tracking.
type PaperBroker struct {
	tenantID  string
	slippage  float64
	orders    map[string]*PaperOrder
	positions map[string]*PaperPosition // keyed by symbol
	mu        sync.RWMutex
	rng       *rand.Rand
	log       zerolog.Logger

	// Cash tracking
	initialCash   float64
	availableCash float64
	usedMargin    float64
}

// NewPaperBroker creates a new paper trading broker client.
func NewPaperBroker(tenantID string, slippage float64, initialCash float64, log zerolog.Logger) *PaperBroker {
	if slippage <= 0 {
		slippage = paperDefaultSlippage
	}
	if initialCash <= 0 {
		initialCash = 1_000_000 // 10 lakh default
	}

	return &PaperBroker{
		tenantID:      tenantID,
		slippage:      slippage,
		orders:        make(map[string]*PaperOrder),
		positions:     make(map[string]*PaperPosition),
		rng:           rand.New(rand.NewSource(time.Now().UnixNano())),
		log:           log.With().Str("broker", "paper").Str("tenant_id", tenantID).Logger(),
		initialCash:   initialCash,
		availableCash: initialCash,
		usedMargin:    0,
	}
}

// TenantID returns the tenant ID this client is bound to.
func (p *PaperBroker) TenantID() string {
	return p.tenantID
}

// PlaceOrder simulates placing an order with realistic fill behaviour.
// Fill price = mid-price +/- slippage. Fill time randomised 50-300ms.
func (p *PaperBroker) PlaceOrder(ctx context.Context, leg OrderLeg) (string, error) {
	orderID := uuid.New().String()
	totalQty := leg.Quantity * leg.LotSize

	p.log.Info().
		Str("order_id", orderID).
		Str("symbol", leg.Symbol).
		Str("action", leg.Action).
		Int("quantity", totalQty).
		Str("order_type", leg.OrderType).
		Msg("paper order received")

	// Create the order in PENDING state
	order := &PaperOrder{
		ID:        orderID,
		Leg:       leg,
		Status:    "PENDING",
		CreatedAt: time.Now(),
	}

	p.mu.Lock()
	p.orders[orderID] = order
	p.mu.Unlock()

	// Simulate async fill with random delay
	go p.simulateFill(ctx, orderID)

	return orderID, nil
}

// simulateFill simulates a fill after a random delay of 50-300ms.
func (p *PaperBroker) simulateFill(ctx context.Context, orderID string) {
	// Random fill time between 50-300ms
	p.mu.RLock()
	fillDelayMs := p.rng.Intn(paperMaxFillTimeMs-paperMinFillTimeMs) + paperMinFillTimeMs
	p.mu.RUnlock()

	fillDelay := time.Duration(fillDelayMs) * time.Millisecond

	select {
	case <-ctx.Done():
		p.mu.Lock()
		if order, exists := p.orders[orderID]; exists {
			order.Status = "CANCELLED"
		}
		p.mu.Unlock()
		return
	case <-time.After(fillDelay):
	}

	p.mu.Lock()
	defer p.mu.Unlock()

	order, exists := p.orders[orderID]
	if !exists {
		return
	}

	if order.Status == "CANCELLED" {
		return
	}

	// Set to OPEN first
	order.Status = "OPEN"

	// Calculate fill price with slippage
	fillPrice := p.calculateFillPrice(order.Leg)
	totalQty := order.Leg.Quantity * order.Leg.LotSize

	now := time.Now()
	order.Status = "COMPLETE"
	order.FilledQty = totalQty
	order.AveragePrice = fillPrice
	order.FilledAt = &now

	// Update positions
	p.updatePosition(order.Leg, fillPrice, totalQty)

	// Update margin tracking
	orderValue := fillPrice * float64(totalQty)
	if order.Leg.Action == "BUY" {
		p.availableCash -= orderValue
		p.usedMargin += orderValue
	} else {
		// Selling: margin blocked for short positions
		p.usedMargin += orderValue * 0.2 // approximate margin requirement
	}

	p.log.Info().
		Str("order_id", orderID).
		Float64("fill_price", fillPrice).
		Int("filled_qty", totalQty).
		Int("fill_delay_ms", fillDelayMs).
		Msg("paper order filled")
}

// calculateFillPrice calculates the simulated fill price with slippage.
func (p *PaperBroker) calculateFillPrice(leg OrderLeg) float64 {
	basePrice := 0.0

	if leg.LimitPrice != nil && *leg.LimitPrice > 0 {
		basePrice = *leg.LimitPrice
	} else {
		// Use a synthetic mid-price based on strike for simulation
		// In production, this would come from market data
		basePrice = leg.Strike * 0.05 // rough estimate for option premium
		if basePrice < 1 {
			basePrice = 10 // minimum price floor
		}
	}

	// Apply slippage: buys get worse (higher), sells get worse (lower)
	slippageAmount := basePrice * p.slippage

	// Randomise slippage between 0 and max
	actualSlippage := slippageAmount * p.rng.Float64()

	if leg.Action == "BUY" {
		return roundToTick(basePrice+actualSlippage, 0.05)
	}
	return roundToTick(basePrice-actualSlippage, 0.05)
}

// updatePosition updates the in-memory position for a filled order.
func (p *PaperBroker) updatePosition(leg OrderLeg, fillPrice float64, totalQty int) {
	key := fmt.Sprintf("%s_%s_%s", leg.Symbol, leg.Exchange, leg.Product)

	pos, exists := p.positions[key]
	if !exists {
		pos = &PaperPosition{
			Symbol:   leg.Symbol,
			Exchange: leg.Exchange,
			Product:  leg.Product,
		}
		p.positions[key] = pos
	}

	if leg.Action == "BUY" {
		// Average up the position
		if pos.Quantity >= 0 {
			totalCost := pos.AveragePrice*float64(pos.Quantity) + fillPrice*float64(totalQty)
			pos.Quantity += totalQty
			if pos.Quantity > 0 {
				pos.AveragePrice = totalCost / float64(pos.Quantity)
			}
		} else {
			// Closing short position
			pos.PnL += (pos.AveragePrice - fillPrice) * float64(min(totalQty, -pos.Quantity))
			pos.Quantity += totalQty
			if pos.Quantity > 0 {
				pos.AveragePrice = fillPrice
			}
		}
	} else {
		// SELL
		if pos.Quantity <= 0 {
			totalCost := pos.AveragePrice*float64(-pos.Quantity) + fillPrice*float64(totalQty)
			pos.Quantity -= totalQty
			if pos.Quantity < 0 {
				pos.AveragePrice = totalCost / float64(-pos.Quantity)
			}
		} else {
			// Closing long position
			pos.PnL += (fillPrice - pos.AveragePrice) * float64(min(totalQty, pos.Quantity))
			pos.Quantity -= totalQty
			if pos.Quantity < 0 {
				pos.AveragePrice = fillPrice
			}
		}
	}

	pos.LastPrice = fillPrice
}

// CancelOrder cancels a paper order if it has not been filled.
func (p *PaperBroker) CancelOrder(ctx context.Context, brokerOrderID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	order, exists := p.orders[brokerOrderID]
	if !exists {
		return fmt.Errorf("order %s not found", brokerOrderID)
	}

	if order.Status == "COMPLETE" {
		return fmt.Errorf("order %s already complete, cannot cancel", brokerOrderID)
	}

	order.Status = "CANCELLED"
	p.log.Info().Str("order_id", brokerOrderID).Msg("paper order cancelled")
	return nil
}

// GetOrderStatus retrieves the current status of a paper order.
func (p *PaperBroker) GetOrderStatus(ctx context.Context, brokerOrderID string) (OrderStatus, error) {
	p.mu.RLock()
	defer p.mu.RUnlock()

	order, exists := p.orders[brokerOrderID]
	if !exists {
		return OrderStatus{}, fmt.Errorf("order %s not found", brokerOrderID)
	}

	return OrderStatus{
		BrokerOrderID: order.ID,
		Status:        order.Status,
		FilledQty:     order.FilledQty,
		AveragePrice:  order.AveragePrice,
		StatusMessage: fmt.Sprintf("paper order %s", order.Status),
	}, nil
}

// GetPositions retrieves all simulated positions for this user.
func (p *PaperBroker) GetPositions(ctx context.Context) ([]BrokerPosition, error) {
	p.mu.RLock()
	defer p.mu.RUnlock()

	positions := make([]BrokerPosition, 0, len(p.positions))
	for _, pos := range p.positions {
		if pos.Quantity == 0 {
			continue // Skip flat positions
		}
		positions = append(positions, BrokerPosition{
			Symbol:       pos.Symbol,
			Exchange:     pos.Exchange,
			Quantity:     pos.Quantity,
			AveragePrice: pos.AveragePrice,
			LastPrice:    pos.LastPrice,
			PnL:          pos.PnL,
			Product:      pos.Product,
		})
	}

	return positions, nil
}

// GetMargins retrieves simulated margin information.
func (p *PaperBroker) GetMargins(ctx context.Context) (Margins, error) {
	p.mu.RLock()
	defer p.mu.RUnlock()

	return Margins{
		AvailableCash:   p.availableCash,
		UsedMargin:      p.usedMargin,
		AvailableMargin: p.availableCash,
		CollateralValue: 0,
		TotalMarginUsed: p.usedMargin,
	}, nil
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
