package feed

import (
	"fmt"
	"strings"
	"time"
)

// SymbolMapping maps broker-specific instrument tokens to canonical symbols.
type SymbolMapping struct {
	entries map[int32]SymbolConfig
	byName  map[string]SymbolConfig
}

// NewSymbolMapping creates a new mapping from the provided symbol configs.
func NewSymbolMapping(configs []SymbolConfig) *SymbolMapping {
	sm := &SymbolMapping{
		entries: make(map[int32]SymbolConfig, len(configs)),
		byName:  make(map[string]SymbolConfig, len(configs)),
	}
	for _, c := range configs {
		sm.entries[c.Token] = c
		sm.byName[c.Symbol] = c
	}
	return sm
}

// Lookup returns the SymbolConfig for a given broker instrument token.
func (sm *SymbolMapping) Lookup(token int32) (SymbolConfig, bool) {
	c, ok := sm.entries[token]
	return c, ok
}

// LookupByName returns the SymbolConfig for a given canonical symbol name.
func (sm *SymbolMapping) LookupByName(symbol string) (SymbolConfig, bool) {
	c, ok := sm.byName[symbol]
	return c, ok
}

// AllConfigs returns all symbol configs in the mapping.
func (sm *SymbolMapping) AllConfigs() []SymbolConfig {
	result := make([]SymbolConfig, 0, len(sm.entries))
	for _, c := range sm.entries {
		result = append(result, c)
	}
	return result
}

// Normaliser converts raw broker ticks into the canonical Tick struct.
type Normaliser struct {
	mapping *SymbolMapping
}

// NewNormaliser creates a new Normaliser with the given symbol mapping.
func NewNormaliser(mapping *SymbolMapping) *Normaliser {
	return &Normaliser{mapping: mapping}
}

// RawTick represents a raw tick from a broker feed before normalisation.
type RawTick struct {
	Token     int32
	LastPrice float64
	Open      float64
	High      float64
	Low       float64
	Close     float64
	Volume    int64
	OI        int64
	Bid       float64
	Ask       float64
	BidQty    int64
	AskQty    int64
	Timestamp time.Time
}

// Normalise converts a RawTick into a canonical Tick using the symbol mapping.
func (n *Normaliser) Normalise(raw RawTick) (Tick, error) {
	cfg, ok := n.mapping.Lookup(raw.Token)
	if !ok {
		return Tick{}, fmt.Errorf("unknown instrument token: %d", raw.Token)
	}

	tick := Tick{
		Symbol:    cfg.Symbol,
		Timestamp: raw.Timestamp.UTC(),
		Segment:   cfg.Segment,
		LastPrice: raw.LastPrice,
		Open:      raw.Open,
		High:      raw.High,
		Low:       raw.Low,
		Close:     raw.Close,
		Volume:    raw.Volume,
		OI:        raw.OI,
		Bid:       raw.Bid,
		Ask:       raw.Ask,
		BidQty:    raw.BidQty,
		AskQty:    raw.AskQty,
	}

	return tick, nil
}

// DeriveNATSSubject determines the correct NATS subject for a tick based on the spec.
// Subjects:
//
//	ticks.nse.index.{symbol}                        -> e.g. ticks.nse.index.NIFTY
//	ticks.nse.fo.{symbol}.{expiry}.{strike}.{type}  -> e.g. ticks.nse.fo.NIFTY.20250130.22000.CE
//	ticks.mcx.{commodity}                           -> e.g. ticks.mcx.GOLD
func DeriveNATSSubject(t Tick) string {
	switch t.Segment {
	case SegmentNSEIndex:
		return fmt.Sprintf("ticks.nse.index.%s", t.Symbol)
	case SegmentNSEFO:
		expiry := ""
		if t.Expiry != nil {
			expiry = *t.Expiry
		}
		strike := ""
		if t.Strike != nil {
			strike = fmt.Sprintf("%.0f", *t.Strike)
		}
		optType := ""
		if t.OptionType != nil {
			optType = *t.OptionType
		}
		return fmt.Sprintf("ticks.nse.fo.%s.%s.%s.%s", t.Symbol, expiry, strike, optType)
	case SegmentMCX:
		// Strip "MCX:" prefix if present for the subject.
		commodity := t.Symbol
		if strings.HasPrefix(commodity, "MCX:") {
			commodity = strings.TrimPrefix(commodity, "MCX:")
		}
		return fmt.Sprintf("ticks.mcx.%s", commodity)
	default:
		return fmt.Sprintf("ticks.unknown.%s", t.Symbol)
	}
}
