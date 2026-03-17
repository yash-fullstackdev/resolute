package feed

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/rs/zerolog/log"
)

// dhanInstrument maps a canonical symbol to its Dhan security ID and exchange segment.
type dhanInstrument struct {
	SecurityID int
	Exchange   string // Dhan REST API exchange segment: "IDX_I", "NSE_EQ", "MCX_COMM", etc.
}

// dhanSecurityIDs maps canonical symbol names to Dhan security IDs and exchange segments.
var dhanSecurityIDs = map[string]dhanInstrument{
	"NIFTY":        {13, "IDX_I"},
	"BANKNIFTY":    {25, "IDX_I"},
	"FINNIFTY":     {27, "IDX_I"},
	"MIDCPNIFTY":   {442, "IDX_I"},
	"NSE:RELIANCE": {2885, "NSE_EQ"},
	"NSE:HDFCBANK": {1333, "NSE_EQ"},
	"NSE:INFY":     {1594, "NSE_EQ"},
	"NSE:TCS":      {11536, "NSE_EQ"},
	"NSE:ICICIBANK": {4963, "NSE_EQ"},
	"NSE:SBIN":     {3045, "NSE_EQ"},
}

const (
	dhanLTPEndpoint  = "https://api.dhan.co/v2/marketfeed/ltp"
	dhanOHLCEndpoint = "https://api.dhan.co/v2/marketfeed/ohlc"
	dhanPollInterval = 2 * time.Second
	dhanHTTPTimeout  = 5 * time.Second
)

// DhanFeedProvider fetches real-time LTP data from Dhan's Market Quote REST API
// by polling at 1-second intervals and converting responses to canonical Tick structs.
type DhanFeedProvider struct {
	mu           sync.RWMutex
	accessToken  string
	clientID     string
	symbols      []SymbolConfig
	tickHandler  func(Tick)
	errorHandler func(error)
	cancel       context.CancelFunc
	wg           sync.WaitGroup
	running      bool
	httpClient   *http.Client

	// dhanSymbols holds only the symbols that have a known Dhan mapping.
	dhanSymbols []SymbolConfig
	// requestBody is the pre-built JSON request body for the LTP endpoint.
	requestBody []byte
	// secIDToSymbol maps "exchange:securityID" to the SymbolConfig for fast lookup.
	secIDToSymbol map[string]SymbolConfig
	// lastPrices tracks the most recent price per symbol for OHLC approximation.
	lastPrices map[string]float64
	// prevClose stores the previous day's close price per symbol (fetched once on startup).
	prevClose map[string]float64
}

// NewDhanFeedProvider creates a Dhan feed provider that polls the LTP REST API.
func NewDhanFeedProvider(accessToken, clientID string, symbols []SymbolConfig) *DhanFeedProvider {
	p := &DhanFeedProvider{
		accessToken:   accessToken,
		clientID:      clientID,
		symbols:       symbols,
		httpClient:    &http.Client{Timeout: dhanHTTPTimeout},
		secIDToSymbol: make(map[string]SymbolConfig),
		lastPrices:    make(map[string]float64),
		prevClose:     make(map[string]float64),
	}

	// Build the request body grouped by exchange segment.
	// Only include symbols that have a Dhan mapping.
	exchangeMap := make(map[string][]int)
	for _, sym := range symbols {
		inst, ok := dhanSecurityIDs[sym.Symbol]
		if !ok {
			log.Warn().Str("symbol", sym.Symbol).Msg("no Dhan security ID mapping, skipping")
			continue
		}
		p.dhanSymbols = append(p.dhanSymbols, sym)
		exchangeMap[inst.Exchange] = append(exchangeMap[inst.Exchange], inst.SecurityID)
		key := fmt.Sprintf("%s:%d", inst.Exchange, inst.SecurityID)
		p.secIDToSymbol[key] = sym
	}

	if len(exchangeMap) > 0 {
		body, err := json.Marshal(exchangeMap)
		if err != nil {
			log.Error().Err(err).Msg("failed to marshal Dhan LTP request body")
		} else {
			p.requestBody = body
		}
	}

	return p
}

func (p *DhanFeedProvider) Connect(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		return fmt.Errorf("dhan feed already running")
	}

	if len(p.dhanSymbols) == 0 {
		return fmt.Errorf("no symbols with Dhan security ID mappings configured")
	}

	if p.accessToken == "" {
		return fmt.Errorf("dhan access token is empty")
	}

	if p.requestBody == nil {
		return fmt.Errorf("dhan request body not built")
	}

	// Fetch previous close prices in background (non-blocking).
	go p.fetchPrevClose(ctx)

	childCtx, cancel := context.WithCancel(ctx)
	p.cancel = cancel
	p.running = true

	p.wg.Add(1)
	go p.pollLoop(childCtx)

	log.Info().
		Int("dhan_symbols", len(p.dhanSymbols)).
		Int("total_symbols", len(p.symbols)).
		Msg("dhan feed provider connected, polling started")
	return nil
}

func (p *DhanFeedProvider) Subscribe(_ []int32) error {
	// All configured symbols are polled automatically.
	return nil
}

func (p *DhanFeedProvider) Unsubscribe(_ []int32) error {
	return nil
}

func (p *DhanFeedProvider) OnTick(handler func(Tick)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.tickHandler = handler
}

func (p *DhanFeedProvider) OnError(handler func(error)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.errorHandler = handler
}

func (p *DhanFeedProvider) Close() error {
	p.mu.Lock()
	if p.cancel != nil {
		p.cancel()
	}
	p.running = false
	p.mu.Unlock()
	p.wg.Wait()
	log.Info().Msg("dhan feed provider closed")
	return nil
}

func (p *DhanFeedProvider) pollLoop(ctx context.Context) {
	defer p.wg.Done()

	ticker := time.NewTicker(dhanPollInterval)
	defer ticker.Stop()

	// Do an immediate first poll.
	p.poll(ctx)

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			p.poll(ctx)
		}
	}
}

// cleanSymbolName strips exchange prefixes like "NSE:" for the canonical tick symbol.
func cleanSymbolName(sym string) string {
	if strings.HasPrefix(sym, "NSE:") {
		return strings.TrimPrefix(sym, "NSE:")
	}
	if strings.HasPrefix(sym, "MCX:") {
		return strings.TrimPrefix(sym, "MCX:")
	}
	return sym
}

// fetchPrevClose calls the Dhan OHLC endpoint once to get previous close prices.
func (p *DhanFeedProvider) fetchPrevClose(ctx context.Context) {
	if p.requestBody == nil {
		return
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, dhanOHLCEndpoint, bytes.NewReader(p.requestBody))
	if err != nil {
		log.Warn().Err(err).Msg("dhan: failed to create OHLC request")
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("access-token", p.accessToken)
	if p.clientID != "" {
		req.Header.Set("client-id", p.clientID)
	}

	resp, err := p.httpClient.Do(req)
	if err != nil {
		log.Warn().Err(err).Msg("dhan: OHLC request failed")
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		log.Warn().Int("status", resp.StatusCode).Str("body", string(body)).Msg("dhan: OHLC non-200")
		return
	}

	var ohlcResp dhanOHLCResponse
	if err := json.NewDecoder(resp.Body).Decode(&ohlcResp); err != nil {
		log.Warn().Err(err).Msg("dhan: failed to decode OHLC response")
		return
	}

	p.mu.Lock()
	defer p.mu.Unlock()
	count := 0
	for exchange, instruments := range ohlcResp.Data {
		for secIDStr, data := range instruments {
			key := fmt.Sprintf("%s:%s", exchange, secIDStr)
			sym, ok := p.secIDToSymbol[key]
			if !ok {
				secID, err := strconv.Atoi(secIDStr)
				if err != nil {
					continue
				}
				key = fmt.Sprintf("%s:%d", exchange, secID)
				sym, ok = p.secIDToSymbol[key]
				if !ok {
					continue
				}
			}
			if data.Close > 0 {
				p.prevClose[sym.Symbol] = data.Close
				count++
			}
		}
	}
	log.Info().Int("symbols_with_close", count).Msg("dhan: fetched previous close prices")
}

type dhanOHLCResponse struct {
	Data map[string]map[string]dhanOHLCData `json:"data"`
}

type dhanOHLCData struct {
	Open  float64 `json:"open"`
	High  float64 `json:"high"`
	Low   float64 `json:"low"`
	Close float64 `json:"close"`
}

// dhanLTPResponse represents the Dhan Market Feed LTP API response.
// The "data" field maps exchange segment -> security ID (string) -> price data.
type dhanLTPResponse struct {
	Data map[string]map[string]dhanLTPData `json:"data"`
}

type dhanLTPData struct {
	LastPrice float64 `json:"last_price"`
}

func (p *DhanFeedProvider) poll(ctx context.Context) {
	p.mu.RLock()
	handler := p.tickHandler
	errHandler := p.errorHandler
	p.mu.RUnlock()

	if handler == nil {
		return
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, dhanLTPEndpoint, bytes.NewReader(p.requestBody))
	if err != nil {
		if errHandler != nil {
			errHandler(fmt.Errorf("dhan: create request: %w", err))
		}
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("access-token", p.accessToken)
	if p.clientID != "" {
		req.Header.Set("client-id", p.clientID)
	}

	resp, err := p.httpClient.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return // Context cancelled, shutting down.
		}
		if errHandler != nil {
			errHandler(fmt.Errorf("dhan: HTTP request failed: %w", err))
		}
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 1024))
		log.Warn().
			Int("status", resp.StatusCode).
			Str("body", string(body)).
			Msg("dhan LTP API returned non-200 status")
		if resp.StatusCode == 429 {
			// Rate limited — back off for 10 seconds
			time.Sleep(10 * time.Second)
		}
		if errHandler != nil {
			errHandler(fmt.Errorf("dhan: API status %d: %s", resp.StatusCode, string(body)))
		}
		return
	}

	var ltpResp dhanLTPResponse
	if err := json.NewDecoder(resp.Body).Decode(&ltpResp); err != nil {
		if errHandler != nil {
			errHandler(fmt.Errorf("dhan: decode response: %w", err))
		}
		return
	}

	now := time.Now().UTC()

	for exchange, instruments := range ltpResp.Data {
		for secIDStr, data := range instruments {
			key := fmt.Sprintf("%s:%s", exchange, secIDStr)
			sym, ok := p.secIDToSymbol[key]
			if !ok {
				// Try with int conversion in case of leading zeros or format differences.
				secID, err := strconv.Atoi(secIDStr)
				if err != nil {
					continue
				}
				key = fmt.Sprintf("%s:%d", exchange, secID)
				sym, ok = p.secIDToSymbol[key]
				if !ok {
					continue
				}
			}

			if data.LastPrice <= 0 {
				continue
			}

			p.mu.Lock()
			prevPrice, hasPrev := p.lastPrices[sym.Symbol]
			if !hasPrev {
				prevPrice = data.LastPrice
			}
			p.lastPrices[sym.Symbol] = data.LastPrice
			p.mu.Unlock()

			// Use previous close for accurate change % calculation.
			p.mu.RLock()
			closePrice, hasClose := p.prevClose[sym.Symbol]
			p.mu.RUnlock()
			if !hasClose {
				closePrice = prevPrice // Fallback to last polled price.
			}

			tick := Tick{
				Symbol:    cleanSymbolName(sym.Symbol),
				Timestamp: now,
				Segment:   sym.Segment,
				LastPrice: data.LastPrice,
				Open:      closePrice,
				High:      data.LastPrice,
				Low:       data.LastPrice,
				Close:     closePrice,
				Volume:    0,
				OI:        0,
				Bid:       0,
				Ask:       0,
				BidQty:    0,
				AskQty:    0,
			}

			handler(tick)
		}
	}
}
