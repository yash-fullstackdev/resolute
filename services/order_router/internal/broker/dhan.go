package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"sync"
	"time"

	"github.com/rs/zerolog"
	"golang.org/x/time/rate"
)

const (
	dhanBaseURL       = "https://api.dhan.co/v2"
	dhanRateLimit     = 25 // requests per second per user
	dhanLimitTimeout  = 3 * time.Second
	dhanMaxRetries    = 3
	dhanTickSize      = 0.05
	dhanNetworkRetry  = 2
	dhanRetryBackoff  = 500 * time.Millisecond
)

// DhanClient implements BrokerClient for DhanHQ REST API.
type DhanClient struct {
	tenantID    string
	apiKey      string
	accessToken string
	clientID    string
	httpClient  *http.Client
	limiter     *rate.Limiter
	log         zerolog.Logger
	mu          sync.RWMutex
}

// NewDhanClient creates a new Dhan broker client with per-user rate limiting.
func NewDhanClient(creds BrokerCredentials, log zerolog.Logger) *DhanClient {
	return &DhanClient{
		tenantID:    creds.TenantID,
		apiKey:      creds.APIKey,
		accessToken: creds.AccessToken,
		clientID:    creds.ClientID,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		limiter: rate.NewLimiter(rate.Limit(dhanRateLimit), dhanRateLimit),
		log:     log.With().Str("broker", "dhan").Str("tenant_id", creds.TenantID).Logger(),
	}
}

// TenantID returns the tenant ID this client is bound to.
func (d *DhanClient) TenantID() string {
	return d.tenantID
}

// UpdateAccessToken refreshes the stored access token (thread-safe).
func (d *DhanClient) UpdateAccessToken(token string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.accessToken = token
}

func (d *DhanClient) getAccessToken() string {
	d.mu.RLock()
	defer d.mu.RUnlock()
	return d.accessToken
}

// dhanOrderRequest is the Dhan API order placement payload.
type dhanOrderRequest struct {
	DhanClientID    string  `json:"dhanClientId"`
	TransactionType string  `json:"transactionType"` // BUY / SELL
	ExchangeSegment string  `json:"exchangeSegment"` // NSE_FNO / NSE_EQ / MCX_COMM
	ProductType     string  `json:"productType"`     // INTRADAY / CNC / MARGIN
	OrderType       string  `json:"orderType"`       // LIMIT / MARKET
	Validity        string  `json:"validity"`        // DAY
	TradingSymbol   string  `json:"tradingSymbol"`
	SecurityID      string  `json:"securityId"`
	Quantity        int     `json:"quantity"`
	Price           float64 `json:"price,omitempty"`
	DisclosedQty    int     `json:"disclosedQuantity"`
}

type dhanOrderResponse struct {
	OrderID   string `json:"orderId"`
	Status    string `json:"orderStatus"`
	Message   string `json:"remarks"`
	ErrorCode string `json:"errorCode"`
}

type dhanOrderStatusResponse struct {
	OrderID      string  `json:"orderId"`
	OrderStatus  string  `json:"orderStatus"`
	FilledQty    int     `json:"filledQty"`
	AveragePrice float64 `json:"averageTradedPrice"`
	Message      string  `json:"remarks"`
}

type dhanPositionResponse struct {
	Symbol       string  `json:"tradingSymbol"`
	Exchange     string  `json:"exchangeSegment"`
	BuyQty       int     `json:"buyQty"`
	SellQty      int     `json:"sellQty"`
	NetQty       int     `json:"netQty"`
	BuyAvgPrice  float64 `json:"buyAvg"`
	SellAvgPrice float64 `json:"sellAvg"`
	RealizedPnL  float64 `json:"realizedProfit"`
	UnrealPnL    float64 `json:"unrealizedProfit"`
	Product      string  `json:"productType"`
}

type dhanMarginResponse struct {
	AvailableBalance float64 `json:"availableBalance"`
	UsedMargin       float64 `json:"utilizedMargin"`
	Collateral       float64 `json:"collateralAmount"`
}

// PlaceOrder places an order via Dhan REST API with smart limit pricing.
// Smart limit pricing: start at (bid+ask)/2, retry adjusting by 1 tick, then market.
func (d *DhanClient) PlaceOrder(ctx context.Context, leg OrderLeg) (string, error) {
	d.log.Info().
		Str("symbol", leg.Symbol).
		Str("action", leg.Action).
		Int("quantity", leg.Quantity*leg.LotSize).
		Str("order_type", leg.OrderType).
		Msg("placing order")

	totalQty := leg.Quantity * leg.LotSize
	exchangeSegment := mapExchangeSegmentDhan(leg.Exchange)
	product := mapProductTypeDhan(leg.Product)

	// If order type is LIMIT and no limit price specified, use smart pricing
	orderType := leg.OrderType
	limitPrice := 0.0
	if leg.LimitPrice != nil {
		limitPrice = *leg.LimitPrice
	}

	if orderType == "LIMIT" && limitPrice == 0 {
		// Caller should provide bid/ask for smart pricing; fallback to MARKET
		orderType = "MARKET"
	}

	// Smart limit pricing with retry logic
	if orderType == "LIMIT" {
		return d.placeSmartLimitOrder(ctx, leg, totalQty, exchangeSegment, product, limitPrice)
	}

	return d.placeMarketOrder(ctx, leg, totalQty, exchangeSegment, product)
}

// placeSmartLimitOrder implements the smart limit pricing retry logic.
// Start at given price, if not filled in 3s, adjust by 1 tick. After 3 retries, go market.
func (d *DhanClient) placeSmartLimitOrder(ctx context.Context, leg OrderLeg, totalQty int, exchangeSegment, product string, startPrice float64) (string, error) {
	price := roundToTick(startPrice, dhanTickSize)

	for attempt := 0; attempt < dhanMaxRetries; attempt++ {
		orderID, err := d.doPlaceOrder(ctx, leg, totalQty, exchangeSegment, product, "LIMIT", price)
		if err != nil {
			return "", fmt.Errorf("place limit order attempt %d: %w", attempt+1, err)
		}

		// Wait for fill with timeout
		filled, status, err := d.waitForFill(ctx, orderID, dhanLimitTimeout)
		if err != nil {
			return orderID, err
		}
		if filled {
			d.log.Info().
				Str("order_id", orderID).
				Float64("fill_price", status.AveragePrice).
				Int("attempt", attempt+1).
				Msg("limit order filled")
			return orderID, nil
		}

		// Not filled — cancel and adjust price
		if cancelErr := d.CancelOrder(ctx, orderID); cancelErr != nil {
			d.log.Warn().Err(cancelErr).Str("order_id", orderID).Msg("failed to cancel unfilled limit order")
		}

		// Adjust price by 1 tick toward market (buy = increase, sell = decrease)
		if leg.Action == "BUY" {
			price = roundToTick(price+dhanTickSize, dhanTickSize)
		} else {
			price = roundToTick(price-dhanTickSize, dhanTickSize)
		}

		d.log.Info().
			Float64("new_price", price).
			Int("attempt", attempt+1).
			Msg("adjusting limit price by 1 tick")
	}

	// All limit attempts exhausted — convert to market order
	d.log.Warn().Msg("limit order not filled after retries, converting to market order")
	return d.placeMarketOrder(ctx, leg, totalQty, exchangeSegment, product)
}

func (d *DhanClient) placeMarketOrder(ctx context.Context, leg OrderLeg, totalQty int, exchangeSegment, product string) (string, error) {
	return d.doPlaceOrder(ctx, leg, totalQty, exchangeSegment, product, "MARKET", 0)
}

func (d *DhanClient) doPlaceOrder(ctx context.Context, leg OrderLeg, totalQty int, exchangeSegment, product, orderType string, price float64) (string, error) {
	if err := d.limiter.Wait(ctx); err != nil {
		return "", fmt.Errorf("rate limiter: %w", err)
	}

	req := dhanOrderRequest{
		DhanClientID:    d.clientID,
		TransactionType: leg.Action,
		ExchangeSegment: exchangeSegment,
		ProductType:     product,
		OrderType:       orderType,
		Validity:        "DAY",
		TradingSymbol:   leg.Symbol,
		SecurityID:      leg.Symbol, // In production, map symbol to security ID
		Quantity:        totalQty,
		Price:           price,
		DisclosedQty:    0,
	}

	body, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("marshal order request: %w", err)
	}

	var resp dhanOrderResponse
	err = d.doRequestWithRetry(ctx, "POST", dhanBaseURL+"/orders", body, &resp)
	if err != nil {
		return "", err
	}

	if resp.ErrorCode != "" {
		return "", fmt.Errorf("dhan order error [%s]: %s", resp.ErrorCode, resp.Message)
	}

	d.log.Info().
		Str("broker_order_id", resp.OrderID).
		Str("status", resp.Status).
		Msg("order placed successfully")

	return resp.OrderID, nil
}

// waitForFill polls for order fill status within the timeout period.
func (d *DhanClient) waitForFill(ctx context.Context, orderID string, timeout time.Duration) (bool, OrderStatus, error) {
	deadline := time.After(timeout)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return false, OrderStatus{}, ctx.Err()
		case <-deadline:
			return false, OrderStatus{}, nil
		case <-ticker.C:
			status, err := d.GetOrderStatus(ctx, orderID)
			if err != nil {
				d.log.Warn().Err(err).Str("order_id", orderID).Msg("error polling order status")
				continue
			}
			switch status.Status {
			case "COMPLETE":
				return true, status, nil
			case "REJECTED", "CANCELLED":
				return false, status, fmt.Errorf("order %s: %s", status.Status, status.StatusMessage)
			}
		}
	}
}

// CancelOrder cancels an order via Dhan REST API.
func (d *DhanClient) CancelOrder(ctx context.Context, brokerOrderID string) error {
	if err := d.limiter.Wait(ctx); err != nil {
		return fmt.Errorf("rate limiter: %w", err)
	}

	url := fmt.Sprintf("%s/orders/%s", dhanBaseURL, brokerOrderID)
	err := d.doRequestWithRetry(ctx, "DELETE", url, nil, nil)
	if err != nil {
		return fmt.Errorf("cancel order %s: %w", brokerOrderID, err)
	}

	d.log.Info().Str("broker_order_id", brokerOrderID).Msg("order cancelled")
	return nil
}

// GetOrderStatus retrieves the current status of an order.
func (d *DhanClient) GetOrderStatus(ctx context.Context, brokerOrderID string) (OrderStatus, error) {
	if err := d.limiter.Wait(ctx); err != nil {
		return OrderStatus{}, fmt.Errorf("rate limiter: %w", err)
	}

	url := fmt.Sprintf("%s/orders/%s", dhanBaseURL, brokerOrderID)
	var resp dhanOrderStatusResponse
	err := d.doRequestWithRetry(ctx, "GET", url, nil, &resp)
	if err != nil {
		return OrderStatus{}, err
	}

	return OrderStatus{
		BrokerOrderID: resp.OrderID,
		Status:        mapDhanStatus(resp.OrderStatus),
		FilledQty:     resp.FilledQty,
		AveragePrice:  resp.AveragePrice,
		StatusMessage: resp.Message,
	}, nil
}

// GetPositions retrieves all positions for this user.
func (d *DhanClient) GetPositions(ctx context.Context) ([]BrokerPosition, error) {
	if err := d.limiter.Wait(ctx); err != nil {
		return nil, fmt.Errorf("rate limiter: %w", err)
	}

	var dhanPositions []dhanPositionResponse
	err := d.doRequestWithRetry(ctx, "GET", dhanBaseURL+"/positions", nil, &dhanPositions)
	if err != nil {
		return nil, err
	}

	positions := make([]BrokerPosition, 0, len(dhanPositions))
	for _, p := range dhanPositions {
		avgPrice := p.BuyAvgPrice
		if p.NetQty < 0 {
			avgPrice = p.SellAvgPrice
		}
		positions = append(positions, BrokerPosition{
			Symbol:   p.Symbol,
			Exchange: p.Exchange,
			Quantity: p.NetQty,
			AveragePrice: avgPrice,
			PnL:      p.RealizedPnL + p.UnrealPnL,
			Product:  p.Product,
		})
	}

	return positions, nil
}

// GetMargins retrieves margin information for this user.
func (d *DhanClient) GetMargins(ctx context.Context) (Margins, error) {
	if err := d.limiter.Wait(ctx); err != nil {
		return Margins{}, fmt.Errorf("rate limiter: %w", err)
	}

	var resp dhanMarginResponse
	err := d.doRequestWithRetry(ctx, "GET", dhanBaseURL+"/fundlimit", nil, &resp)
	if err != nil {
		return Margins{}, err
	}

	return Margins{
		AvailableCash:   resp.AvailableBalance,
		UsedMargin:      resp.UsedMargin,
		AvailableMargin: resp.AvailableBalance - resp.UsedMargin,
		CollateralValue: resp.Collateral,
		TotalMarginUsed: resp.UsedMargin,
	}, nil
}

// doRequestWithRetry executes an HTTP request with network timeout retry (2x with 500ms backoff).
func (d *DhanClient) doRequestWithRetry(ctx context.Context, method, url string, body []byte, result interface{}) error {
	var lastErr error

	for attempt := 0; attempt <= dhanNetworkRetry; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(dhanRetryBackoff * time.Duration(attempt)):
			}
			d.log.Debug().Int("attempt", attempt+1).Str("url", url).Msg("retrying request")
		}

		var bodyReader io.Reader
		if body != nil {
			bodyReader = bytes.NewReader(body)
		}

		req, err := http.NewRequestWithContext(ctx, method, url, bodyReader)
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}

		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("access-token", d.getAccessToken())
		req.Header.Set("client-id", d.clientID)

		resp, err := d.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("http request: %w", err)
			d.log.Warn().Err(err).Str("method", method).Str("url", url).Msg("request failed, will retry")
			continue
		}

		respBody, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			lastErr = fmt.Errorf("read response: %w", err)
			continue
		}

		if resp.StatusCode >= 500 {
			lastErr = fmt.Errorf("server error %d: %s", resp.StatusCode, string(respBody))
			continue
		}

		if resp.StatusCode >= 400 {
			return fmt.Errorf("client error %d: %s", resp.StatusCode, string(respBody))
		}

		if result != nil && len(respBody) > 0 {
			if err := json.Unmarshal(respBody, result); err != nil {
				return fmt.Errorf("unmarshal response: %w", err)
			}
		}

		return nil
	}

	return fmt.Errorf("all retries exhausted: %w", lastErr)
}

// roundToTick rounds a price to the nearest tick size.
func roundToTick(price, tickSize float64) float64 {
	return math.Round(price/tickSize) * tickSize
}

// mapExchangeSegmentDhan maps exchange string to Dhan segment.
func mapExchangeSegmentDhan(exchange string) string {
	switch exchange {
	case "NFO":
		return "NSE_FNO"
	case "NSE":
		return "NSE_EQ"
	case "MCX":
		return "MCX_COMM"
	default:
		return "NSE_FNO"
	}
}

// mapProductTypeDhan maps product string to Dhan product type.
func mapProductTypeDhan(product string) string {
	switch product {
	case "MIS":
		return "INTRADAY"
	case "NRML":
		return "MARGIN"
	default:
		return "INTRADAY"
	}
}

// mapDhanStatus normalizes Dhan order status strings.
func mapDhanStatus(status string) string {
	switch status {
	case "TRADED", "COMPLETE":
		return "COMPLETE"
	case "PENDING", "TRANSIT":
		return "OPEN"
	case "REJECTED":
		return "REJECTED"
	case "CANCELLED":
		return "CANCELLED"
	default:
		return status
	}
}
