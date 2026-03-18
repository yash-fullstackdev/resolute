package feed

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/rs/zerolog/log"
)

const (
	dhanLTPEndpoint    = "https://api.dhan.co/v2/marketfeed/ltp"
	dhanRestPollInterval = 2 * time.Second
)

// dhanInstrument maps a canonical symbol to its Dhan security ID and exchange segment.
type dhanInstrument struct {
	SecurityID int
	Exchange   string // Dhan exchange segment: "IDX_I", "NSE_EQ", "NSE_FNO", "MCX_COMM", etc.
}

// dhanSecurityIDs maps canonical symbol names to Dhan security IDs and exchange segments.
var dhanSecurityIDs = map[string]dhanInstrument{
	"NIFTY":         {13, "IDX_I"},
	"BANKNIFTY":     {25, "IDX_I"},
	"FINNIFTY":      {27, "IDX_I"},
	"MIDCPNIFTY":    {442, "IDX_I"},
	"NSE:RELIANCE":  {2885, "NSE_EQ"},
	"NSE:HDFCBANK":  {1333, "NSE_EQ"},
	"NSE:INFY":      {1594, "NSE_EQ"},
	"NSE:TCS":       {11536, "NSE_EQ"},
	"NSE:ICICIBANK": {4963, "NSE_EQ"},
	"NSE:SBIN":      {3045, "NSE_EQ"},
}

// dhanExchangeByte maps Dhan exchange segment strings to the byte values used in the
// WebSocket v2 binary subscription protocol.
var dhanExchangeByte = map[string]byte{
	"IDX_I":    0,
	"NSE_EQ":   1,
	"NSE_FNO":  2,
	"BSE_EQ":   3,
	"BSE_FNO":  4,
	"MCX_COMM": 5,
}

// dhanExchangeFromByte is the reverse mapping from byte to exchange segment string.
var dhanExchangeFromByte = map[byte]string{
	0: "IDX_I",
	1: "NSE_EQ",
	2: "NSE_FNO",
	3: "BSE_EQ",
	4: "BSE_FNO",
	5: "MCX_COMM",
}

const (
	dhanWSURL = "wss://api-feed.dhan.co"

	// Subscription request codes.
	dhanSubscribeCode   = byte(21)
	dhanUnsubscribeCode = byte(22)

	// Subscription type: Quote (17) gives us OHLC data.
	dhanSubTypeQuote = uint16(17)

	// Response packet types.
	dhanPacketTicker    = byte(2)
	dhanPacketPrevClose = byte(4)
	dhanPacketQuote     = byte(7)
	dhanPacketDisconn   = byte(50)

	// Header size for all response packets.
	dhanHeaderSize = 8

	// Reconnection parameters.
	dhanReconnectBaseDelay = 2 * time.Second
	dhanReconnectMaxDelay  = 60 * time.Second
)

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

// DhanWSFeedProvider connects to Dhan's WebSocket Market Feed v2 for real-time ticks.
type DhanWSFeedProvider struct {
	mu           sync.RWMutex
	accessToken  string
	clientID     string
	symbols      []SymbolConfig
	tickHandler  func(Tick)
	errorHandler func(error)
	cancel       context.CancelFunc
	wg           sync.WaitGroup
	running      bool
	conn         *websocket.Conn

	// dhanSymbols holds only the symbols that have a known Dhan mapping.
	dhanSymbols []SymbolConfig
	// secIDToSymbol maps "exchangeByte:securityID" to the SymbolConfig for fast lookup.
	secIDToSymbol map[string]SymbolConfig
	// prevClose stores the previous day's close price per symbol (received via type 4 packets).
	prevClose map[string]float64
}

// NewDhanWSFeedProvider creates a Dhan WebSocket feed provider.
func NewDhanWSFeedProvider(accessToken, clientID string, symbols []SymbolConfig) *DhanWSFeedProvider {
	p := &DhanWSFeedProvider{
		accessToken:   accessToken,
		clientID:      clientID,
		symbols:       symbols,
		secIDToSymbol: make(map[string]SymbolConfig),
		prevClose:     make(map[string]float64),
	}

	for _, sym := range symbols {
		inst, ok := dhanSecurityIDs[sym.Symbol]
		if !ok {
			log.Warn().Str("symbol", sym.Symbol).Msg("no Dhan security ID mapping, skipping")
			continue
		}
		p.dhanSymbols = append(p.dhanSymbols, sym)

		exchByte, ok := dhanExchangeByte[inst.Exchange]
		if !ok {
			log.Warn().Str("symbol", sym.Symbol).Str("exchange", inst.Exchange).Msg("unknown Dhan exchange segment, skipping")
			continue
		}
		key := fmt.Sprintf("%d:%d", exchByte, inst.SecurityID)
		p.secIDToSymbol[key] = sym
	}

	return p
}

func (p *DhanWSFeedProvider) Connect(ctx context.Context) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	if p.running {
		return fmt.Errorf("dhan websocket feed already running")
	}

	if len(p.dhanSymbols) == 0 {
		return fmt.Errorf("no symbols with Dhan security ID mappings configured")
	}

	if p.accessToken == "" {
		return fmt.Errorf("dhan access token is empty")
	}

	childCtx, cancel := context.WithCancel(ctx)
	p.cancel = cancel
	p.running = true

	p.wg.Add(1)
	go p.connectionLoop(childCtx)

	log.Info().
		Int("dhan_symbols", len(p.dhanSymbols)).
		Int("total_symbols", len(p.symbols)).
		Msg("dhan websocket feed provider started")
	return nil
}

func (p *DhanWSFeedProvider) Subscribe(_ []int32) error {
	// All configured symbols are subscribed on connect.
	return nil
}

func (p *DhanWSFeedProvider) Unsubscribe(_ []int32) error {
	return nil
}

func (p *DhanWSFeedProvider) OnTick(handler func(Tick)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.tickHandler = handler
}

func (p *DhanWSFeedProvider) OnError(handler func(error)) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.errorHandler = handler
}

func (p *DhanWSFeedProvider) Close() error {
	p.mu.Lock()
	if p.cancel != nil {
		p.cancel()
	}
	p.running = false
	conn := p.conn
	p.mu.Unlock()

	if conn != nil {
		// Send a close frame, ignore errors.
		_ = conn.WriteMessage(
			websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""),
		)
		_ = conn.Close()
	}

	p.wg.Wait()
	log.Info().Msg("dhan websocket feed provider closed")
	return nil
}

// connectionLoop manages the WebSocket connection with automatic reconnection.
func (p *DhanWSFeedProvider) connectionLoop(ctx context.Context) {
	defer p.wg.Done()

	delay := dhanReconnectBaseDelay

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		err := p.connectAndRead(ctx)
		if ctx.Err() != nil {
			return // Context cancelled, shutting down.
		}

		if err != nil {
			errMsg := err.Error()
			if strings.Contains(errMsg, "429") {
				// WebSocket rate-limited — fall back to REST polling until it clears.
				log.Warn().Msg("dhan: websocket rate-limited, falling back to REST polling")
				p.restPollFallback(ctx)
				delay = dhanReconnectBaseDelay // Reset delay after REST fallback.
				continue
			}

			log.Error().Err(err).Dur("retry_in", delay).Msg("dhan websocket disconnected, reconnecting")

			p.mu.RLock()
			errHandler := p.errorHandler
			p.mu.RUnlock()
			if errHandler != nil {
				errHandler(fmt.Errorf("dhan websocket disconnected: %w", err))
			}
		}

		// Wait before reconnecting with exponential backoff.
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}

		delay = time.Duration(float64(delay) * 1.5)
		if delay > dhanReconnectMaxDelay {
			delay = dhanReconnectMaxDelay
		}
	}
}

// connectAndRead dials the WebSocket, subscribes, and reads messages until disconnection.
func (p *DhanWSFeedProvider) connectAndRead(ctx context.Context) error {
	// Build WebSocket URL with auth parameters.
	u, _ := url.Parse(dhanWSURL)
	q := u.Query()
	q.Set("version", "2")
	q.Set("token", p.accessToken)
	q.Set("clientId", p.clientID)
	q.Set("authType", "2")
	u.RawQuery = q.Encode()

	log.Info().Str("url", u.String()).Msg("dhan: dialing websocket")

	header := make(map[string][]string)
	header["access-token"] = []string{p.accessToken}
	if p.clientID != "" {
		header["client-id"] = []string{p.clientID}
	}

	conn, resp, err := websocket.DefaultDialer.DialContext(ctx, u.String(), header)
	if err != nil {
		statusCode := 0
		if resp != nil {
			statusCode = resp.StatusCode
		}
		return fmt.Errorf("websocket dial (status %d): %w", statusCode, err)
	}

	p.mu.Lock()
	p.conn = conn
	p.mu.Unlock()

	defer func() {
		conn.Close()
		p.mu.Lock()
		p.conn = nil
		p.mu.Unlock()
	}()

	// Send subscription packet for all instruments.
	if err := p.sendSubscription(conn, dhanSubscribeCode); err != nil {
		return fmt.Errorf("send subscription: %w", err)
	}

	log.Info().Int("instruments", len(p.dhanSymbols)).Msg("dhan: subscribed to instruments via websocket")

	// Read loop.
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		_, message, err := conn.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("read message: %w", err)
		}

		p.handleMessage(message)
	}
}

// sendSubscription builds and sends a binary subscription packet for all configured instruments.
func (p *DhanWSFeedProvider) sendSubscription(conn *websocket.Conn, requestCode byte) error {
	// Packet: 1 byte request code + 7 bytes per instrument.
	numInstruments := len(p.dhanSymbols)
	if numInstruments > 100 {
		numInstruments = 100 // Protocol max.
	}

	packetSize := 1 + numInstruments*7
	packet := make([]byte, packetSize)
	packet[0] = requestCode

	offset := 1
	for i, sym := range p.dhanSymbols {
		if i >= 100 {
			break
		}

		inst, ok := dhanSecurityIDs[sym.Symbol]
		if !ok {
			continue
		}

		exchByte, ok := dhanExchangeByte[inst.Exchange]
		if !ok {
			continue
		}

		// Byte 0: Exchange segment byte.
		packet[offset] = exchByte
		// Bytes 1-4: Security ID (big-endian uint32).
		binary.BigEndian.PutUint32(packet[offset+1:offset+5], uint32(inst.SecurityID))
		// Bytes 5-6: Subscription type (big-endian uint16) — 17 = Quote.
		binary.BigEndian.PutUint16(packet[offset+5:offset+7], dhanSubTypeQuote)

		offset += 7
	}

	return conn.WriteMessage(websocket.BinaryMessage, packet)
}

// restPollFallback polls the Dhan REST LTP endpoint every 2s as fallback when WebSocket is rate-limited.
func (p *DhanWSFeedProvider) restPollFallback(ctx context.Context) {
	exchangeMap := make(map[string][]int)
	for _, sym := range p.dhanSymbols {
		inst, ok := dhanSecurityIDs[sym.Symbol]
		if !ok {
			continue
		}
		exchangeMap[inst.Exchange] = append(exchangeMap[inst.Exchange], inst.SecurityID)
	}
	reqBody, err := json.Marshal(exchangeMap)
	if err != nil {
		return
	}
	secLookup := make(map[string]SymbolConfig)
	for _, sym := range p.dhanSymbols {
		inst, ok := dhanSecurityIDs[sym.Symbol]
		if !ok {
			continue
		}
		secLookup[fmt.Sprintf("%s:%d", inst.Exchange, inst.SecurityID)] = sym
	}
	httpClient := &http.Client{Timeout: 5 * time.Second}
	ticker := time.NewTicker(dhanRestPollInterval)
	defer ticker.Stop()
	log.Info().Msg("dhan: REST polling fallback started")
	for {
		select {
		case <-ctx.Done():
			log.Info().Msg("dhan: REST polling fallback stopped")
			return
		case <-ticker.C:
		}
		p.mu.RLock()
		handler := p.tickHandler
		p.mu.RUnlock()
		if handler == nil {
			continue
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, dhanLTPEndpoint, bytes.NewReader(reqBody))
		if err != nil {
			continue
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("access-token", p.accessToken)
		if p.clientID != "" {
			req.Header.Set("client-id", p.clientID)
		}
		resp, err := httpClient.Do(req)
		if err != nil {
			continue
		}
		if resp.StatusCode != 200 {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 429 {
				time.Sleep(10 * time.Second)
			}
			continue
		}
		var ltpResp struct {
			Data map[string]map[string]struct {
				LastPrice float64 `json:"last_price"`
			} `json:"data"`
		}
		json.NewDecoder(resp.Body).Decode(&ltpResp)
		resp.Body.Close()
		now := time.Now().UTC()
		for exchange, instruments := range ltpResp.Data {
			for secIDStr, data := range instruments {
				secID, _ := strconv.Atoi(secIDStr)
				key := fmt.Sprintf("%s:%d", exchange, secID)
				sym, ok := secLookup[key]
				if !ok || data.LastPrice <= 0 {
					continue
				}
				p.mu.RLock()
				closePrice := p.prevClose[sym.Symbol]
				p.mu.RUnlock()
				if closePrice <= 0 {
					closePrice = data.LastPrice
				}
				handler(Tick{
					Symbol:    cleanSymbolName(sym.Symbol),
					Timestamp: now,
					Segment:   sym.Segment,
					LastPrice: data.LastPrice,
					Close:     closePrice,
				})
			}
		}
	}
}

// SubscribeDynamic adds a single instrument to the live subscription set.
// If a WebSocket connection is active, it sends a subscription packet immediately.
// Thread-safe — can be called from any goroutine (e.g. a NATS handler).
func (p *DhanWSFeedProvider) SubscribeDynamic(symbol string, securityID int, exchange string) {
	exchByte, ok := dhanExchangeByte[exchange]
	if !ok {
		log.Warn().Str("symbol", symbol).Str("exchange", exchange).Msg("SubscribeDynamic: unknown exchange segment, ignoring")
		return
	}

	key := fmt.Sprintf("%d:%d", exchByte, securityID)

	p.mu.Lock()

	// Check if already subscribed.
	if _, exists := p.secIDToSymbol[key]; exists {
		p.mu.Unlock()
		log.Debug().Str("symbol", symbol).Msg("SubscribeDynamic: already subscribed, skipping")
		return
	}

	// Register the instrument in lookup maps.
	sym := SymbolConfig{
		Symbol:  symbol,
		Segment: SegmentNSEIndex, // default; caller can refine
	}
	p.secIDToSymbol[key] = sym
	p.dhanSymbols = append(p.dhanSymbols, sym)

	// Also register in the global security ID table so reconnections pick it up.
	dhanSecurityIDs[symbol] = dhanInstrument{SecurityID: securityID, Exchange: exchange}

	conn := p.conn
	p.mu.Unlock()

	log.Info().Str("symbol", symbol).Int("security_id", securityID).Str("exchange", exchange).Msg("SubscribeDynamic: instrument registered")

	// If there is a live connection, send a single-instrument subscription packet.
	if conn != nil {
		packet := make([]byte, 8) // 1 byte request code + 7 bytes instrument
		packet[0] = dhanSubscribeCode
		packet[1] = exchByte
		binary.BigEndian.PutUint32(packet[2:6], uint32(securityID))
		binary.BigEndian.PutUint16(packet[6:8], dhanSubTypeQuote)

		if err := conn.WriteMessage(websocket.BinaryMessage, packet); err != nil {
			log.Error().Err(err).Str("symbol", symbol).Msg("SubscribeDynamic: failed to send subscription packet")
		} else {
			log.Info().Str("symbol", symbol).Msg("SubscribeDynamic: subscription packet sent on live connection")
		}
	}
}

// handleMessage parses a binary response packet from the Dhan WebSocket feed.
func (p *DhanWSFeedProvider) handleMessage(data []byte) {
	if len(data) < dhanHeaderSize {
		return
	}

	packetType := data[0]

	switch packetType {
	case dhanPacketDisconn:
		log.Warn().Msg("dhan: received disconnect packet from server")
		p.mu.RLock()
		conn := p.conn
		p.mu.RUnlock()
		if conn != nil {
			_ = conn.Close()
		}
		return

	case dhanPacketPrevClose:
		p.handlePrevClose(data)

	case dhanPacketQuote:
		p.handleQuote(data)

	case dhanPacketTicker:
		p.handleTicker(data)

	default:
		log.Debug().Uint8("packet_type", packetType).Int("len", len(data)).Msg("dhan: unknown packet type")
	}
}

// handlePrevClose processes a type 4 (previous close) packet.
func (p *DhanWSFeedProvider) handlePrevClose(data []byte) {
	if len(data) < dhanHeaderSize+4 {
		return
	}

	exchByte := data[1]
	securityID := binary.BigEndian.Uint32(data[2:6])
	prevClose := float64(int32(binary.BigEndian.Uint32(data[dhanHeaderSize:dhanHeaderSize+4]))) / 100.0

	key := fmt.Sprintf("%d:%d", exchByte, securityID)

	p.mu.Lock()
	sym, ok := p.secIDToSymbol[key]
	if ok && prevClose > 0 {
		p.prevClose[sym.Symbol] = prevClose
	}
	p.mu.Unlock()

	if ok {
		log.Debug().Str("symbol", sym.Symbol).Float64("prev_close", prevClose).Msg("dhan: received previous close")
	}
}

// handleQuote processes a type 7 (quote) packet with full OHLC data.
func (p *DhanWSFeedProvider) handleQuote(data []byte) {
	if len(data) < dhanHeaderSize+44 {
		return
	}

	exchByte := data[1]
	securityID := binary.BigEndian.Uint32(data[2:6])

	key := fmt.Sprintf("%d:%d", exchByte, securityID)

	p.mu.RLock()
	sym, ok := p.secIDToSymbol[key]
	handler := p.tickHandler
	closePrice := p.prevClose[sym.Symbol]
	p.mu.RUnlock()

	if !ok || handler == nil {
		return
	}

	payload := data[dhanHeaderSize:]

	ltp := float64(int32(binary.BigEndian.Uint32(payload[0:4]))) / 100.0
	// LTQ at payload[4:8] — not needed for Tick.
	ltt := int64(int32(binary.BigEndian.Uint32(payload[8:12])))
	volume := int64(int32(binary.BigEndian.Uint32(payload[12:16])))
	// Avg price at payload[16:20] — not needed for Tick.
	totalBuyQty := int64(int32(binary.BigEndian.Uint32(payload[20:24])))
	totalSellQty := int64(int32(binary.BigEndian.Uint32(payload[24:28])))
	open := float64(int32(binary.BigEndian.Uint32(payload[28:32]))) / 100.0
	close_ := float64(int32(binary.BigEndian.Uint32(payload[32:36]))) / 100.0
	high := float64(int32(binary.BigEndian.Uint32(payload[36:40]))) / 100.0
	low := float64(int32(binary.BigEndian.Uint32(payload[40:44]))) / 100.0

	if ltp <= 0 {
		return
	}

	// Use packet close as prev close if we don't have one yet.
	if closePrice <= 0 && close_ > 0 {
		p.mu.Lock()
		p.prevClose[sym.Symbol] = close_
		p.mu.Unlock()
		closePrice = close_
	}

	var ts time.Time
	if ltt > 0 {
		ts = time.Unix(ltt, 0).UTC()
	} else {
		ts = time.Now().UTC()
	}

	// Approximate bid/ask from total buy/sell quantities and LTP.
	_ = totalBuyQty
	_ = totalSellQty

	tick := Tick{
		Symbol:    cleanSymbolName(sym.Symbol),
		Timestamp: ts,
		Segment:   sym.Segment,
		LastPrice: ltp,
		Open:      open,
		High:      high,
		Low:       low,
		Close:     closePrice,
		Volume:    volume,
		OI:        0,
		Bid:       0,
		Ask:       0,
		BidQty:    totalBuyQty,
		AskQty:    totalSellQty,
	}

	handler(tick)
}

// handleTicker processes a type 2 (ticker) packet with just LTP data.
func (p *DhanWSFeedProvider) handleTicker(data []byte) {
	if len(data) < dhanHeaderSize+20 {
		return
	}

	exchByte := data[1]
	securityID := binary.BigEndian.Uint32(data[2:6])

	key := fmt.Sprintf("%d:%d", exchByte, securityID)

	p.mu.RLock()
	sym, ok := p.secIDToSymbol[key]
	handler := p.tickHandler
	closePrice := p.prevClose[sym.Symbol]
	p.mu.RUnlock()

	if !ok || handler == nil {
		return
	}

	payload := data[dhanHeaderSize:]

	ltp := float64(int32(binary.BigEndian.Uint32(payload[0:4]))) / 100.0
	// LTQ at payload[4:8].
	ltt := int64(int32(binary.BigEndian.Uint32(payload[8:12])))
	volume := int64(int32(binary.BigEndian.Uint32(payload[12:16])))
	// Avg traded price at payload[16:20].

	if ltp <= 0 {
		return
	}

	var ts time.Time
	if ltt > 0 {
		ts = time.Unix(ltt, 0).UTC()
	} else {
		ts = time.Now().UTC()
	}

	tick := Tick{
		Symbol:    cleanSymbolName(sym.Symbol),
		Timestamp: ts,
		Segment:   sym.Segment,
		LastPrice: ltp,
		Open:      0,
		High:      0,
		Low:       0,
		Close:     closePrice,
		Volume:    volume,
		OI:        0,
		Bid:       0,
		Ask:       0,
		BidQty:    0,
		AskQty:    0,
	}

	handler(tick)
}

