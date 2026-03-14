package broker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"sync"
	"time"

	"github.com/rs/zerolog"
	"golang.org/x/time/rate"
)

const (
	kiteBaseURL      = "https://api.kite.trade"
	kiteRateLimit    = 10 // Zerodha rate limit: 10 req/sec
	kiteNetworkRetry = 2
	kiteRetryBackoff = 500 * time.Millisecond
	kiteLimitTimeout = 3 * time.Second
	kiteMaxRetries   = 3
	kiteTickSize     = 0.05
)

// ZerodhaClient implements BrokerClient for Zerodha Kite Connect API.
type ZerodhaClient struct {
	tenantID    string
	apiKey      string
	accessToken string
	httpClient  *http.Client
	limiter     *rate.Limiter
	log         zerolog.Logger
	mu          sync.RWMutex
}

// NewZerodhaClient creates a new Zerodha Kite Connect broker client.
func NewZerodhaClient(creds BrokerCredentials, log zerolog.Logger) *ZerodhaClient {
	return &ZerodhaClient{
		tenantID:    creds.TenantID,
		apiKey:      creds.APIKey,
		accessToken: creds.AccessToken,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
		limiter: rate.NewLimiter(rate.Limit(kiteRateLimit), kiteRateLimit),
		log:     log.With().Str("broker", "zerodha").Str("tenant_id", creds.TenantID).Logger(),
	}
}

// TenantID returns the tenant ID this client is bound to.
func (z *ZerodhaClient) TenantID() string {
	return z.tenantID
}

// UpdateAccessToken refreshes the stored access token (thread-safe).
func (z *ZerodhaClient) UpdateAccessToken(token string) {
	z.mu.Lock()
	defer z.mu.Unlock()
	z.accessToken = token
}

func (z *ZerodhaClient) getAccessToken() string {
	z.mu.RLock()
	defer z.mu.RUnlock()
	return z.accessToken
}

func (z *ZerodhaClient) authHeader() string {
	return fmt.Sprintf("token %s:%s", z.apiKey, z.getAccessToken())
}

type kiteOrderResponse struct {
	Status string `json:"status"`
	Data   struct {
		OrderID string `json:"order_id"`
	} `json:"data"`
	Message   string `json:"message"`
	ErrorType string `json:"error_type"`
}

type kiteOrderStatusData struct {
	OrderID       string  `json:"order_id"`
	Status        string  `json:"status"`
	FilledQty     int     `json:"filled_quantity"`
	AveragePrice  float64 `json:"average_price"`
	StatusMessage string  `json:"status_message"`
}

type kiteOrderStatusResponse struct {
	Status string              `json:"status"`
	Data   []kiteOrderStatusData `json:"data"`
}

type kitePositionData struct {
	TradingSymbol string  `json:"tradingsymbol"`
	Exchange      string  `json:"exchange"`
	Quantity      int     `json:"quantity"`
	AveragePrice  float64 `json:"average_price"`
	LastPrice     float64 `json:"last_price"`
	PnL           float64 `json:"pnl"`
	Product       string  `json:"product"`
}

type kitePositionsResponse struct {
	Status string `json:"status"`
	Data   struct {
		Net []kitePositionData `json:"net"`
	} `json:"data"`
}

type kiteMarginData struct {
	Available struct {
		Cash       float64 `json:"cash"`
		Collateral float64 `json:"collateral"`
	} `json:"available"`
	Utilised struct {
		Debits float64 `json:"debits"`
	} `json:"utilised"`
}

type kiteMarginsResponse struct {
	Status string `json:"status"`
	Data   struct {
		Equity kiteMarginData `json:"equity"`
	} `json:"data"`
}

// PlaceOrder places an order via Kite Connect with smart limit pricing.
func (z *ZerodhaClient) PlaceOrder(ctx context.Context, leg OrderLeg) (string, error) {
	z.log.Info().
		Str("symbol", leg.Symbol).
		Str("action", leg.Action).
		Int("quantity", leg.Quantity*leg.LotSize).
		Str("order_type", leg.OrderType).
		Msg("placing order")

	totalQty := leg.Quantity * leg.LotSize

	orderType := leg.OrderType
	limitPrice := 0.0
	if leg.LimitPrice != nil {
		limitPrice = *leg.LimitPrice
	}

	if orderType == "LIMIT" && limitPrice == 0 {
		orderType = "MARKET"
	}

	if orderType == "LIMIT" {
		return z.placeSmartLimitOrder(ctx, leg, totalQty, limitPrice)
	}

	return z.placeMarketOrder(ctx, leg, totalQty)
}

func (z *ZerodhaClient) placeSmartLimitOrder(ctx context.Context, leg OrderLeg, totalQty int, startPrice float64) (string, error) {
	price := roundToTick(startPrice, kiteTickSize)

	for attempt := 0; attempt < kiteMaxRetries; attempt++ {
		orderID, err := z.doPlaceOrder(ctx, leg, totalQty, "LIMIT", price)
		if err != nil {
			return "", fmt.Errorf("place limit order attempt %d: %w", attempt+1, err)
		}

		filled, status, err := z.waitForFill(ctx, orderID, kiteLimitTimeout)
		if err != nil {
			return orderID, err
		}
		if filled {
			z.log.Info().
				Str("order_id", orderID).
				Float64("fill_price", status.AveragePrice).
				Int("attempt", attempt+1).
				Msg("limit order filled")
			return orderID, nil
		}

		if cancelErr := z.CancelOrder(ctx, orderID); cancelErr != nil {
			z.log.Warn().Err(cancelErr).Str("order_id", orderID).Msg("failed to cancel unfilled limit order")
		}

		if leg.Action == "BUY" {
			price = roundToTick(price+kiteTickSize, kiteTickSize)
		} else {
			price = roundToTick(price-kiteTickSize, kiteTickSize)
		}

		z.log.Info().Float64("new_price", price).Int("attempt", attempt+1).Msg("adjusting limit price by 1 tick")
	}

	z.log.Warn().Msg("limit order not filled after retries, converting to market order")
	return z.placeMarketOrder(ctx, leg, totalQty)
}

func (z *ZerodhaClient) placeMarketOrder(ctx context.Context, leg OrderLeg, totalQty int) (string, error) {
	return z.doPlaceOrder(ctx, leg, totalQty, "MARKET", 0)
}

func (z *ZerodhaClient) doPlaceOrder(ctx context.Context, leg OrderLeg, totalQty int, orderType string, price float64) (string, error) {
	if err := z.limiter.Wait(ctx); err != nil {
		return "", fmt.Errorf("rate limiter: %w", err)
	}

	exchange := mapExchangeKite(leg.Exchange)
	product := mapProductKite(leg.Product)

	params := url.Values{}
	params.Set("tradingsymbol", leg.Symbol)
	params.Set("exchange", exchange)
	params.Set("transaction_type", leg.Action)
	params.Set("order_type", orderType)
	params.Set("product", product)
	params.Set("quantity", strconv.Itoa(totalQty))
	params.Set("validity", "DAY")
	if orderType == "LIMIT" && price > 0 {
		params.Set("price", strconv.FormatFloat(price, 'f', 2, 64))
	}

	var resp kiteOrderResponse
	err := z.doFormRequestWithRetry(ctx, "POST", kiteBaseURL+"/orders/regular", params, &resp)
	if err != nil {
		return "", err
	}

	if resp.Status == "error" {
		return "", fmt.Errorf("kite order error [%s]: %s", resp.ErrorType, resp.Message)
	}

	z.log.Info().Str("broker_order_id", resp.Data.OrderID).Msg("order placed successfully")
	return resp.Data.OrderID, nil
}

func (z *ZerodhaClient) waitForFill(ctx context.Context, orderID string, timeout time.Duration) (bool, OrderStatus, error) {
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
			status, err := z.GetOrderStatus(ctx, orderID)
			if err != nil {
				z.log.Warn().Err(err).Str("order_id", orderID).Msg("error polling order status")
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

// CancelOrder cancels an order via Kite Connect.
func (z *ZerodhaClient) CancelOrder(ctx context.Context, brokerOrderID string) error {
	if err := z.limiter.Wait(ctx); err != nil {
		return fmt.Errorf("rate limiter: %w", err)
	}

	url := fmt.Sprintf("%s/orders/regular/%s", kiteBaseURL, brokerOrderID)
	err := z.doJSONRequestWithRetry(ctx, "DELETE", url, nil, nil)
	if err != nil {
		return fmt.Errorf("cancel order %s: %w", brokerOrderID, err)
	}

	z.log.Info().Str("broker_order_id", brokerOrderID).Msg("order cancelled")
	return nil
}

// GetOrderStatus retrieves the current status of an order.
func (z *ZerodhaClient) GetOrderStatus(ctx context.Context, brokerOrderID string) (OrderStatus, error) {
	if err := z.limiter.Wait(ctx); err != nil {
		return OrderStatus{}, fmt.Errorf("rate limiter: %w", err)
	}

	url := fmt.Sprintf("%s/orders/%s", kiteBaseURL, brokerOrderID)
	var resp kiteOrderStatusResponse
	err := z.doJSONRequestWithRetry(ctx, "GET", url, nil, &resp)
	if err != nil {
		return OrderStatus{}, err
	}

	if len(resp.Data) == 0 {
		return OrderStatus{}, fmt.Errorf("no order data for %s", brokerOrderID)
	}

	// Last entry has the most recent status
	latest := resp.Data[len(resp.Data)-1]
	return OrderStatus{
		BrokerOrderID: latest.OrderID,
		Status:        mapKiteStatus(latest.Status),
		FilledQty:     latest.FilledQty,
		AveragePrice:  latest.AveragePrice,
		StatusMessage: latest.StatusMessage,
	}, nil
}

// GetPositions retrieves all positions for this user.
func (z *ZerodhaClient) GetPositions(ctx context.Context) ([]BrokerPosition, error) {
	if err := z.limiter.Wait(ctx); err != nil {
		return nil, fmt.Errorf("rate limiter: %w", err)
	}

	var resp kitePositionsResponse
	err := z.doJSONRequestWithRetry(ctx, "GET", kiteBaseURL+"/portfolio/positions", nil, &resp)
	if err != nil {
		return nil, err
	}

	positions := make([]BrokerPosition, 0, len(resp.Data.Net))
	for _, p := range resp.Data.Net {
		positions = append(positions, BrokerPosition{
			Symbol:       p.TradingSymbol,
			Exchange:     p.Exchange,
			Quantity:     p.Quantity,
			AveragePrice: p.AveragePrice,
			LastPrice:    p.LastPrice,
			PnL:          p.PnL,
			Product:      p.Product,
		})
	}

	return positions, nil
}

// GetMargins retrieves margin information for this user.
func (z *ZerodhaClient) GetMargins(ctx context.Context) (Margins, error) {
	if err := z.limiter.Wait(ctx); err != nil {
		return Margins{}, fmt.Errorf("rate limiter: %w", err)
	}

	var resp kiteMarginsResponse
	err := z.doJSONRequestWithRetry(ctx, "GET", kiteBaseURL+"/user/margins", nil, &resp)
	if err != nil {
		return Margins{}, err
	}

	eq := resp.Data.Equity
	available := eq.Available.Cash + eq.Available.Collateral - eq.Utilised.Debits
	return Margins{
		AvailableCash:   eq.Available.Cash,
		UsedMargin:      eq.Utilised.Debits,
		AvailableMargin: available,
		CollateralValue: eq.Available.Collateral,
		TotalMarginUsed: eq.Utilised.Debits,
	}, nil
}

// doFormRequestWithRetry executes a form-encoded HTTP request with retry.
func (z *ZerodhaClient) doFormRequestWithRetry(ctx context.Context, method, reqURL string, params url.Values, result interface{}) error {
	var lastErr error

	for attempt := 0; attempt <= kiteNetworkRetry; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(kiteRetryBackoff * time.Duration(attempt)):
			}
		}

		var bodyReader io.Reader
		if params != nil {
			bodyReader = bytes.NewBufferString(params.Encode())
		}

		req, err := http.NewRequestWithContext(ctx, method, reqURL, bodyReader)
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}

		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		req.Header.Set("Authorization", z.authHeader())
		req.Header.Set("X-Kite-Version", "3")

		resp, err := z.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("http request: %w", err)
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

// doJSONRequestWithRetry executes a JSON HTTP request with retry.
func (z *ZerodhaClient) doJSONRequestWithRetry(ctx context.Context, method, reqURL string, body []byte, result interface{}) error {
	var lastErr error

	for attempt := 0; attempt <= kiteNetworkRetry; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(kiteRetryBackoff * time.Duration(attempt)):
			}
		}

		var bodyReader io.Reader
		if body != nil {
			bodyReader = bytes.NewReader(body)
		}

		req, err := http.NewRequestWithContext(ctx, method, reqURL, bodyReader)
		if err != nil {
			return fmt.Errorf("create request: %w", err)
		}

		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", z.authHeader())
		req.Header.Set("X-Kite-Version", "3")

		resp, err := z.httpClient.Do(req)
		if err != nil {
			lastErr = fmt.Errorf("http request: %w", err)
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

func mapExchangeKite(exchange string) string {
	switch exchange {
	case "NFO":
		return "NFO"
	case "NSE":
		return "NSE"
	case "MCX":
		return "MCX"
	default:
		return "NFO"
	}
}

func mapProductKite(product string) string {
	switch product {
	case "MIS":
		return "MIS"
	case "NRML":
		return "NRML"
	default:
		return "MIS"
	}
}

func mapKiteStatus(status string) string {
	switch status {
	case "COMPLETE":
		return "COMPLETE"
	case "OPEN", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED", "TRIGGER PENDING":
		return "OPEN"
	case "REJECTED":
		return "REJECTED"
	case "CANCELLED":
		return "CANCELLED"
	default:
		return status
	}
}
