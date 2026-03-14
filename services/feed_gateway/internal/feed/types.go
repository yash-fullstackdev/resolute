package feed

import (
	"context"
	"time"
)

// Segment constants matching the canonical data model.
const (
	SegmentNSEIndex = "NSE_INDEX"
	SegmentNSEFO    = "NSE_FO"
	SegmentMCX      = "MCX"
)

// Option type constants.
const (
	OptionTypeCE = "CE"
	OptionTypePE = "PE"
)

// Tick is the canonical tick struct shared across all services.
// Field names and JSON tags match the spec in 02_data_models.md exactly.
type Tick struct {
	Symbol          string    `json:"symbol"`
	Timestamp       time.Time `json:"timestamp"`
	Segment         string    `json:"segment"`
	LastPrice       float64   `json:"last_price"`
	Open            float64   `json:"open"`
	High            float64   `json:"high"`
	Low             float64   `json:"low"`
	Close           float64   `json:"close"`
	Volume          int64     `json:"volume"`
	OI              int64     `json:"oi"`
	Bid             float64   `json:"bid"`
	Ask             float64   `json:"ask"`
	BidQty          int64     `json:"bid_qty"`
	AskQty          int64     `json:"ask_qty"`
	Expiry          *string   `json:"expiry,omitempty"`
	Strike          *float64  `json:"strike,omitempty"`
	OptionType      *string   `json:"option_type,omitempty"`
	UnderlyingPrice *float64  `json:"underlying_price,omitempty"`
}

// FeedProvider is the interface every broker feed adapter must implement.
type FeedProvider interface {
	// Connect establishes the connection to the feed source.
	Connect(ctx context.Context) error

	// Subscribe adds instrument tokens to the active subscription set.
	Subscribe(tokens []int32) error

	// Unsubscribe removes instrument tokens from the active subscription set.
	Unsubscribe(tokens []int32) error

	// OnTick registers a callback invoked for every normalised tick.
	OnTick(handler func(Tick))

	// OnError registers a callback invoked on feed errors.
	OnError(handler func(error))

	// Close gracefully shuts down the feed connection.
	Close() error
}

// SymbolConfig holds per-symbol configuration used by feed providers.
type SymbolConfig struct {
	Symbol     string  // e.g. "NIFTY", "BANKNIFTY", "MCX:GOLD"
	Segment    string  // SegmentNSEIndex | SegmentNSEFO | SegmentMCX
	Token      int32   // Broker instrument token
	BasePrice  float64 // Reference price for paper feed random walk
	LotSize    int     // Contract lot size (for OI simulation)
}

// StaleFeedAlert is published when no tick is received for a symbol within the timeout.
type StaleFeedAlert struct {
	Symbol    string    `json:"symbol"`
	Segment   string    `json:"segment"`
	LastTick  time.Time `json:"last_tick"`
	AlertTime time.Time `json:"alert_time"`
	Message   string    `json:"message"`
}
