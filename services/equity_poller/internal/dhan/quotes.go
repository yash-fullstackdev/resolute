package dhan

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"

	"github.com/rs/zerolog"
)

// QuoteOHLC holds OHLC data from Dhan quote response.
type QuoteOHLC struct {
	Open  float32 `json:"open"`
	High  float32 `json:"high"`
	Low   float32 `json:"low"`
	Close float32 `json:"close"` // Previous day's EOD closing price.
}

// DepthLevel holds a single bid/ask level.
type DepthLevel struct {
	Price    float32 `json:"price"`
	Quantity uint32  `json:"quantity"`
}

// Depth holds order book depth.
type Depth struct {
	Buy  []DepthLevel `json:"buy"`
	Sell []DepthLevel `json:"sell"`
}

// QuoteItem holds the parsed quote for a single security.
type QuoteItem struct {
	LastPrice float32   `json:"last_price"`
	OHLC      QuoteOHLC `json:"ohlc"`
	Volume    uint64    `json:"volume"`
	OI        uint64    `json:"oi"`
	DepthData Depth     `json:"depth"`
}

// BidPrice returns top-of-book bid price.
func (q *QuoteItem) BidPrice() float32 {
	if len(q.DepthData.Buy) > 0 {
		return q.DepthData.Buy[0].Price
	}
	return 0
}

// AskPrice returns top-of-book ask price.
func (q *QuoteItem) AskPrice() float32 {
	if len(q.DepthData.Sell) > 0 {
		return q.DepthData.Sell[0].Price
	}
	return 0
}

// BidQty returns top-of-book bid quantity.
func (q *QuoteItem) BidQty() uint32 {
	if len(q.DepthData.Buy) > 0 {
		return q.DepthData.Buy[0].Quantity
	}
	return 0
}

// AskQty returns top-of-book ask quantity.
func (q *QuoteItem) AskQty() uint32 {
	if len(q.DepthData.Sell) > 0 {
		return q.DepthData.Sell[0].Quantity
	}
	return 0
}

// quoteRequest is the POST body for Dhan /marketfeed/quote endpoint.
type quoteRequest struct {
	NSEEQ []uint64 `json:"NSE_EQ"`
}

// quoteResponse represents the top-level Dhan quote API response.
type quoteResponse struct {
	Status string                                `json:"status"`
	Data   map[string]map[string]json.RawMessage `json:"data"`
}

// FetchQuotes fetches quotes for a batch of security IDs (max 1000 per call).
// Returns a map of securityID string -> QuoteItem.
func FetchQuotes(client *Client, securityIDs []string, logger zerolog.Logger) (map[string]*QuoteItem, int, string, error) {
	ids := make([]uint64, 0, len(securityIDs))
	for _, s := range securityIDs {
		id, err := strconv.ParseUint(s, 10, 64)
		if err != nil {
			continue
		}
		ids = append(ids, id)
	}

	reqBody := quoteRequest{NSEEQ: ids}
	bodyBytes, err := json.Marshal(reqBody)
	if err != nil {
		return nil, 0, "", fmt.Errorf("marshal quote request: %w", err)
	}

	url := client.BaseURL + "/marketfeed/quote"
	req, err := http.NewRequest("POST", url, bytes.NewReader(bodyBytes))
	if err != nil {
		return nil, 0, "", fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("access-token", client.AccessToken)
	req.Header.Set("client-id", client.ClientID)
	req.Header.Set("Content-Type", "application/json")

	resp, err := client.HTTP.Do(req)
	if err != nil {
		return nil, 0, "", fmt.Errorf("HTTP request failed: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, "", fmt.Errorf("read response body: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		errBody := string(respBody)
		if len(errBody) > 500 {
			errBody = errBody[:500]
		}
		return nil, resp.StatusCode, errBody, fmt.Errorf("Dhan API status=%d", resp.StatusCode)
	}

	var qr quoteResponse
	if err := json.Unmarshal(respBody, &qr); err != nil {
		preview := string(respBody)
		if len(preview) > 300 {
			preview = preview[:300]
		}
		return nil, resp.StatusCode, preview, fmt.Errorf("JSON parse error: %w", err)
	}

	if qr.Status != "success" {
		return nil, resp.StatusCode, string(respBody), fmt.Errorf("Dhan API status=%s", qr.Status)
	}

	// Parse the nested data: { "NSE_EQ": { "1333": {...}, ... } }
	result := make(map[string]*QuoteItem)
	for _, items := range qr.Data {
		for secID, raw := range items {
			var qi QuoteItem
			if err := json.Unmarshal(raw, &qi); err != nil {
				logger.Warn().Str("security_id", secID).Err(err).Msg("skipping unparseable quote")
				continue
			}
			result[secID] = &qi
		}
	}

	return result, resp.StatusCode, "", nil
}
