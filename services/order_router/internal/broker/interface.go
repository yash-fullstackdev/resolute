package broker

import (
	"context"
	"time"
)

// BrokerClient is the unified interface for all broker implementations.
// Each instance is bound to a single tenant (user).
type BrokerClient interface {
	PlaceOrder(ctx context.Context, leg OrderLeg) (string, error) // returns broker_order_id
	CancelOrder(ctx context.Context, brokerOrderID string) error
	GetOrderStatus(ctx context.Context, brokerOrderID string) (OrderStatus, error)
	GetPositions(ctx context.Context) ([]BrokerPosition, error)
	GetMargins(ctx context.Context) (Margins, error)
	TenantID() string // identifies which user this client belongs to
}

// OrderLeg represents a single leg of a multi-leg order.
type OrderLeg struct {
	Symbol         string   `json:"symbol"`
	Exchange       string   `json:"exchange"`        // "NSE" | "NFO" | "MCX"
	InstrumentType string   `json:"instrument_type"` // "CE" | "PE" | "FUT"
	Strike         float64  `json:"strike"`
	Expiry         string   `json:"expiry"`
	Action         string   `json:"action"`     // "BUY" | "SELL"
	Quantity       int      `json:"quantity"`    // Number of lots
	LotSize        int      `json:"lot_size"`
	OrderType      string   `json:"order_type"` // "LIMIT" | "MARKET"
	LimitPrice     *float64 `json:"limit_price,omitempty"`
	Product        string   `json:"product"` // "MIS" | "NRML"
}

// OrderStatus represents the current state of an order at the broker.
type OrderStatus struct {
	BrokerOrderID string  `json:"broker_order_id"`
	Status        string  `json:"status"` // "OPEN" | "COMPLETE" | "REJECTED" | "CANCELLED"
	FilledQty     int     `json:"filled_qty"`
	AveragePrice  float64 `json:"average_price"`
	StatusMessage string  `json:"status_message"`
}

// BrokerPosition represents a position reported by the broker.
type BrokerPosition struct {
	Symbol         string  `json:"symbol"`
	Exchange       string  `json:"exchange"`
	InstrumentType string  `json:"instrument_type"`
	Quantity       int     `json:"quantity"`
	AveragePrice   float64 `json:"average_price"`
	LastPrice      float64 `json:"last_price"`
	PnL            float64 `json:"pnl"`
	Product        string  `json:"product"`
}

// Margins represents the margin information from the broker.
type Margins struct {
	AvailableCash    float64 `json:"available_cash"`
	UsedMargin       float64 `json:"used_margin"`
	AvailableMargin  float64 `json:"available_margin"`
	CollateralValue  float64 `json:"collateral_value"`
	TotalMarginUsed  float64 `json:"total_margin_used"`
}

// BrokerCredentials holds the decrypted broker credentials for a user.
type BrokerCredentials struct {
	TenantID    string `json:"tenant_id"`
	Broker      string `json:"broker"` // "dhan" | "zerodha" | "paper"
	APIKey      string `json:"api_key"`
	APISecret   string `json:"api_secret"`
	ClientID    string `json:"client_id"`
	TOTPSecret  string `json:"totp_secret"`
	AccessToken string `json:"access_token"`
}

// Order represents the full order model used internally.
type Order struct {
	ID            string     `json:"id"`
	TenantID      string     `json:"tenant_id"`
	SignalID      string     `json:"signal_id"`
	BrokerOrderID *string    `json:"broker_order_id,omitempty"`
	Leg           OrderLeg   `json:"leg"`
	Status        string     `json:"status"` // "PENDING" | "OPEN" | "COMPLETE" | "REJECTED" | "CANCELLED"
	FillPrice     *float64   `json:"fill_price,omitempty"`
	FillTime      *time.Time `json:"fill_time,omitempty"`
	Error         *string    `json:"error,omitempty"`
}

// Position represents an in-memory position tracked per user.
type Position struct {
	ID              string         `json:"id"`
	TenantID        string         `json:"tenant_id"`
	StrategyName    string         `json:"strategy_name"`
	Underlying      string         `json:"underlying"`
	Legs            []Order        `json:"legs"`
	EntryTime       time.Time      `json:"entry_time"`
	EntryCostINR    float64        `json:"entry_cost_inr"`
	CurrentValueINR float64        `json:"current_value_inr"`
	UnrealisedPnL   float64        `json:"unrealised_pnl_inr"`
	RealisedPnL     float64        `json:"realised_pnl_inr"`
	StopLossPrice   float64        `json:"stop_loss_price"`
	TargetPrice     float64        `json:"target_price"`
	TimeStop        time.Time      `json:"time_stop"`
	Status          string         `json:"status"` // "OPEN" | "CLOSED" | "STOP_HIT" | "TIME_STOP" | "TARGET_HIT"
	Greeks          PositionGreeks `json:"greeks"`
}

// PositionGreeks contains the aggregate greeks for a position.
type PositionGreeks struct {
	Delta      float64 `json:"delta"`
	Gamma      float64 `json:"gamma"`
	Theta      float64 `json:"theta"`
	Vega       float64 `json:"vega"`
	NetPremium float64 `json:"net_premium"`
}

// ValidatedOrder is the message received from NATS on orders.new.validated.{tenant_id}.
type ValidatedOrder struct {
	ID           string     `json:"id"`
	TenantID     string     `json:"tenant_id"`
	SignalID     string     `json:"signal_id"`
	StrategyName string     `json:"strategy_name"`
	Underlying   string     `json:"underlying"`
	Legs         []OrderLeg `json:"legs"`
	MaxLossINR   float64    `json:"max_loss_inr"`
	TargetINR    float64    `json:"target_profit_inr"`
	StopLossPct  float64    `json:"stop_loss_pct"`
	TimeStop     time.Time  `json:"time_stop"`
}

// FillConfirmation is published to fills.{tenant_id}.{order_id}.
type FillConfirmation struct {
	OrderID       string    `json:"order_id"`
	TenantID      string    `json:"tenant_id"`
	SignalID      string    `json:"signal_id"`
	BrokerOrderID string    `json:"broker_order_id"`
	Symbol        string    `json:"symbol"`
	Action        string    `json:"action"`
	Quantity      int       `json:"quantity"`
	FillPrice     float64   `json:"fill_price"`
	FillTime      time.Time `json:"fill_time"`
	Status        string    `json:"status"`
}
