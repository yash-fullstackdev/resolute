package executor

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/nats-io/nats.go"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/order_router/internal/broker"
	"github.com/resolute/india-options-builder/services/order_router/internal/state"
)

var (
	ordersReceived = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "order_router_orders_received_total",
		Help: "Total number of orders received",
	}, []string{"tenant_id"})

	ordersExecuted = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "order_router_orders_executed_total",
		Help: "Total number of orders executed",
	}, []string{"tenant_id", "status"})

	orderLatency = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "order_router_order_latency_seconds",
		Help:    "Order execution latency in seconds",
		Buckets: prometheus.ExponentialBuckets(0.01, 2, 12),
	}, []string{"tenant_id"})

	legsExecuted = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "order_router_legs_executed_total",
		Help: "Total number of order legs executed",
	}, []string{"tenant_id", "status"})

	partialReversals = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "order_router_partial_reversals_total",
		Help: "Total number of partial fill reversals",
	}, []string{"tenant_id"})
)

// ClientGetter is an interface for looking up broker clients by tenant ID.
// This decouples the executor from the broker pool to avoid circular imports.
type ClientGetter interface {
	Get(tenantID string) (broker.BrokerClient, bool)
}

// OrderExecutor receives validated orders from NATS, executes them via the
// appropriate BrokerClient, and publishes fill confirmations.
type OrderExecutor struct {
	clientGetter ClientGetter
	nc           *nats.Conn
	db           *sql.DB
	positions    *state.PositionState
	log          zerolog.Logger
}

// NewOrderExecutor creates a new OrderExecutor.
func NewOrderExecutor(nc *nats.Conn, db *sql.DB, positions *state.PositionState, log zerolog.Logger) *OrderExecutor {
	return &OrderExecutor{
		nc:        nc,
		db:        db,
		positions: positions,
		log:       log.With().Str("component", "executor").Logger(),
	}
}

// SetClientGetter sets the client getter (called after pool is initialized to break circular dep).
func (e *OrderExecutor) SetClientGetter(cg ClientGetter) {
	e.clientGetter = cg
}

// legResult holds the result of executing a single order leg.
type legResult struct {
	Index         int
	BrokerOrderID string
	Status        broker.OrderStatus
	Err           error
}

// HandleOrder processes a validated order message from NATS.
func (e *OrderExecutor) HandleOrder(msg *nats.Msg) {
	startTime := time.Now()

	var validatedOrder broker.ValidatedOrder
	if err := json.Unmarshal(msg.Data, &validatedOrder); err != nil {
		e.log.Error().Err(err).Msg("failed to unmarshal validated order")
		return
	}

	tenantID := validatedOrder.TenantID
	ordersReceived.WithLabelValues(tenantID).Inc()

	e.log.Info().
		Str("order_id", validatedOrder.ID).
		Str("tenant_id", tenantID).
		Str("strategy", validatedOrder.StrategyName).
		Int("legs", len(validatedOrder.Legs)).
		Msg("received validated order")

	// Get broker client for this tenant
	client, ok := e.clientGetter.Get(tenantID)
	if !ok {
		e.log.Error().Str("tenant_id", tenantID).Msg("no broker client found for tenant")
		ordersExecuted.WithLabelValues(tenantID, "REJECTED").Inc()
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	// Persist initial order state as PENDING
	orders := e.createOrders(validatedOrder)
	for i := range orders {
		e.persistOrderState(ctx, &orders[i], "PENDING")
	}

	// Execute all legs simultaneously
	results := e.executeLegsSimultaneously(ctx, client, validatedOrder.Legs)

	// Check results and handle partial failures
	allSuccess := true
	anyFailed := false
	filledLegs := make([]legResult, 0)
	failedLegs := make([]legResult, 0)

	for _, result := range results {
		if result.Err != nil {
			anyFailed = true
			allSuccess = false
			failedLegs = append(failedLegs, result)
		} else {
			filledLegs = append(filledLegs, result)
		}
	}

	if allSuccess {
		// All legs filled successfully — wait for final confirmation
		e.handleAllLegsSuccess(ctx, client, validatedOrder, orders, results)
	} else if anyFailed && len(filledLegs) > 0 {
		// Partial fill — rollback filled legs
		e.handlePartialFailure(ctx, client, tenantID, validatedOrder, orders, filledLegs, failedLegs)
	} else {
		// All legs failed
		e.handleAllLegsFailed(ctx, tenantID, validatedOrder, orders, failedLegs)
	}

	elapsed := time.Since(startTime).Seconds()
	orderLatency.WithLabelValues(tenantID).Observe(elapsed)
}

// executeLegsSimultaneously executes all order legs in parallel via goroutines.
func (e *OrderExecutor) executeLegsSimultaneously(ctx context.Context, client broker.BrokerClient, legs []broker.OrderLeg) []legResult {
	results := make([]legResult, len(legs))
	var wg sync.WaitGroup

	for i, leg := range legs {
		wg.Add(1)
		go func(idx int, l broker.OrderLeg) {
			defer wg.Done()

			brokerOrderID, err := client.PlaceOrder(ctx, l)
			if err != nil {
				results[idx] = legResult{
					Index: idx,
					Err:   err,
				}
				e.log.Error().Err(err).
					Int("leg_index", idx).
					Str("symbol", l.Symbol).
					Msg("failed to place order leg")
				return
			}

			// Wait for fill confirmation
			status, waitErr := e.waitForCompletion(ctx, client, brokerOrderID, 30*time.Second)
			if waitErr != nil {
				results[idx] = legResult{
					Index:         idx,
					BrokerOrderID: brokerOrderID,
					Err:           waitErr,
				}
				return
			}

			results[idx] = legResult{
				Index:         idx,
				BrokerOrderID: brokerOrderID,
				Status:        status,
			}
		}(i, leg)
	}

	wg.Wait()
	return results
}

// waitForCompletion polls for order completion with timeout.
func (e *OrderExecutor) waitForCompletion(ctx context.Context, client broker.BrokerClient, brokerOrderID string, timeout time.Duration) (broker.OrderStatus, error) {
	deadline := time.After(timeout)
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return broker.OrderStatus{}, ctx.Err()
		case <-deadline:
			return broker.OrderStatus{}, fmt.Errorf("order %s timed out waiting for completion", brokerOrderID)
		case <-ticker.C:
			status, err := client.GetOrderStatus(ctx, brokerOrderID)
			if err != nil {
				e.log.Warn().Err(err).Str("broker_order_id", brokerOrderID).Msg("error polling order status")
				continue
			}

			switch status.Status {
			case "COMPLETE":
				return status, nil
			case "REJECTED":
				return status, fmt.Errorf("order rejected: %s", status.StatusMessage)
			case "CANCELLED":
				return status, fmt.Errorf("order cancelled: %s", status.StatusMessage)
			}
		}
	}
}

// handleAllLegsSuccess processes a fully successful multi-leg order.
func (e *OrderExecutor) handleAllLegsSuccess(ctx context.Context, client broker.BrokerClient, vo broker.ValidatedOrder, orders []broker.Order, results []legResult) {
	tenantID := vo.TenantID
	now := time.Now()

	for i, result := range results {
		orders[i].BrokerOrderID = &result.BrokerOrderID
		orders[i].Status = "COMPLETE"
		orders[i].FillPrice = &result.Status.AveragePrice
		orders[i].FillTime = &now
		e.persistOrderState(ctx, &orders[i], "COMPLETE")

		// Publish fill confirmation
		fill := broker.FillConfirmation{
			OrderID:       orders[i].ID,
			TenantID:      tenantID,
			SignalID:      vo.SignalID,
			BrokerOrderID: result.BrokerOrderID,
			Symbol:        vo.Legs[i].Symbol,
			Action:        vo.Legs[i].Action,
			Quantity:      vo.Legs[i].Quantity * vo.Legs[i].LotSize,
			FillPrice:     result.Status.AveragePrice,
			FillTime:      now,
			Status:        "COMPLETE",
		}

		e.publishFill(tenantID, orders[i].ID, fill)
		legsExecuted.WithLabelValues(tenantID, "COMPLETE").Inc()
	}

	// Update position state
	position := broker.Position{
		ID:           uuid.New().String(),
		TenantID:     tenantID,
		StrategyName: vo.StrategyName,
		Underlying:   vo.Underlying,
		Legs:         orders,
		EntryTime:    now,
		StopLossPrice: vo.StopLossPct,
		TargetPrice:   vo.TargetINR,
		TimeStop:      vo.TimeStop,
		Status:        "OPEN",
	}

	// Calculate entry cost
	entryCost := 0.0
	for _, result := range results {
		entryCost += result.Status.AveragePrice
	}
	position.EntryCostINR = entryCost

	e.positions.AddPosition(tenantID, position)

	ordersExecuted.WithLabelValues(tenantID, "COMPLETE").Inc()

	e.log.Info().
		Str("order_id", vo.ID).
		Str("tenant_id", tenantID).
		Int("legs", len(results)).
		Msg("all order legs filled successfully")
}

// handlePartialFailure handles the case where some legs filled and others failed.
// It cancels/reverses the filled legs and publishes a reversal alert.
func (e *OrderExecutor) handlePartialFailure(ctx context.Context, client broker.BrokerClient, tenantID string, vo broker.ValidatedOrder, orders []broker.Order, filledLegs, failedLegs []legResult) {
	e.log.Warn().
		Str("order_id", vo.ID).
		Str("tenant_id", tenantID).
		Int("filled", len(filledLegs)).
		Int("failed", len(failedLegs)).
		Msg("partial fill detected, initiating reversal")

	partialReversals.WithLabelValues(tenantID).Inc()

	// Cancel all filled legs
	for _, filled := range filledLegs {
		// Try to cancel first; if already complete, place a reverse order
		cancelErr := client.CancelOrder(ctx, filled.BrokerOrderID)
		if cancelErr != nil {
			// Order already filled — place reverse order
			reverseLeg := vo.Legs[filled.Index]
			if reverseLeg.Action == "BUY" {
				reverseLeg.Action = "SELL"
			} else {
				reverseLeg.Action = "BUY"
			}
			reverseLeg.OrderType = "MARKET"

			reverseID, reverseErr := client.PlaceOrder(ctx, reverseLeg)
			if reverseErr != nil {
				e.log.Error().Err(reverseErr).
					Str("broker_order_id", filled.BrokerOrderID).
					Msg("CRITICAL: failed to reverse filled leg")
			} else {
				e.log.Info().
					Str("original_order_id", filled.BrokerOrderID).
					Str("reverse_order_id", reverseID).
					Msg("reversed filled leg")
			}
		}

		orders[filled.Index].Status = "CANCELLED"
		e.persistOrderState(ctx, &orders[filled.Index], "CANCELLED")
		legsExecuted.WithLabelValues(tenantID, "CANCELLED").Inc()
	}

	// Mark failed legs
	for _, failed := range failedLegs {
		errStr := failed.Err.Error()
		orders[failed.Index].Status = "REJECTED"
		orders[failed.Index].Error = &errStr
		e.persistOrderState(ctx, &orders[failed.Index], "REJECTED")
		legsExecuted.WithLabelValues(tenantID, "REJECTED").Inc()
	}

	// Publish ORDER_PARTIAL_FILL_REVERSAL alert
	alertSubject := fmt.Sprintf("alerts.order_failure.%s", tenantID)
	alertPayload := fmt.Sprintf(
		`{"tenant_id":"%s","order_id":"%s","type":"ORDER_PARTIAL_FILL_REVERSAL","filled_legs":%d,"failed_legs":%d,"timestamp":"%s"}`,
		tenantID, vo.ID, len(filledLegs), len(failedLegs), time.Now().UTC().Format(time.RFC3339),
	)
	if err := e.nc.Publish(alertSubject, []byte(alertPayload)); err != nil {
		e.log.Error().Err(err).Msg("failed to publish partial fill reversal alert")
	}

	ordersExecuted.WithLabelValues(tenantID, "PARTIAL_REVERSAL").Inc()
}

// handleAllLegsFailed handles the case where all legs of an order failed.
func (e *OrderExecutor) handleAllLegsFailed(ctx context.Context, tenantID string, vo broker.ValidatedOrder, orders []broker.Order, failedLegs []legResult) {
	e.log.Error().
		Str("order_id", vo.ID).
		Str("tenant_id", tenantID).
		Int("failed_legs", len(failedLegs)).
		Msg("all order legs failed")

	for _, failed := range failedLegs {
		errStr := failed.Err.Error()
		orders[failed.Index].Status = "REJECTED"
		orders[failed.Index].Error = &errStr
		e.persistOrderState(ctx, &orders[failed.Index], "REJECTED")
		legsExecuted.WithLabelValues(tenantID, "REJECTED").Inc()

		// Handle specific error types per spec
		e.handleRejectionReason(tenantID, errStr)
	}

	// Publish alert
	alertSubject := fmt.Sprintf("alerts.order_failure.%s", tenantID)
	alertPayload := fmt.Sprintf(
		`{"tenant_id":"%s","order_id":"%s","type":"ALL_LEGS_FAILED","failed_legs":%d,"timestamp":"%s"}`,
		tenantID, vo.ID, len(failedLegs), time.Now().UTC().Format(time.RFC3339),
	)
	_ = e.nc.Publish(alertSubject, []byte(alertPayload))

	ordersExecuted.WithLabelValues(tenantID, "REJECTED").Inc()
}

// handleRejectionReason handles specific rejection reasons per the spec.
func (e *OrderExecutor) handleRejectionReason(tenantID, errMsg string) {
	lowerErr := strings.ToLower(errMsg)

	if strings.Contains(lowerErr, "insufficient funds") || strings.Contains(lowerErr, "insufficient margin") {
		// Publish risk breach - do not retry
		subject := fmt.Sprintf("risk.breach.%s.margin", tenantID)
		payload := fmt.Sprintf(`{"tenant_id":"%s","type":"margin","message":"%s","timestamp":"%s"}`,
			tenantID, errMsg, time.Now().UTC().Format(time.RFC3339))
		_ = e.nc.Publish(subject, []byte(payload))
		e.log.Warn().Str("tenant_id", tenantID).Msg("insufficient funds - published risk breach, no retry")
	} else if strings.Contains(lowerErr, "outside trading hours") || strings.Contains(lowerErr, "market closed") {
		e.log.Warn().Str("tenant_id", tenantID).Msg("outside trading hours - no retry")
	} else {
		// Unhandled rejection - alert
		subject := fmt.Sprintf("alerts.order_failure.%s", tenantID)
		payload := fmt.Sprintf(`{"tenant_id":"%s","type":"UNHANDLED_REJECTION","message":"%s","timestamp":"%s"}`,
			tenantID, errMsg, time.Now().UTC().Format(time.RFC3339))
		_ = e.nc.Publish(subject, []byte(payload))
	}
}

// createOrders creates Order structs from a ValidatedOrder.
func (e *OrderExecutor) createOrders(vo broker.ValidatedOrder) []broker.Order {
	orders := make([]broker.Order, len(vo.Legs))
	for i, leg := range vo.Legs {
		orders[i] = broker.Order{
			ID:       uuid.New().String(),
			TenantID: vo.TenantID,
			SignalID: vo.SignalID,
			Leg:      leg,
			Status:   "PENDING",
		}
	}
	return orders
}

// persistOrderState persists an order state transition to TimescaleDB.
func (e *OrderExecutor) persistOrderState(ctx context.Context, order *broker.Order, status string) {
	order.Status = status

	if e.db == nil {
		e.log.Debug().
			Str("order_id", order.ID).
			Str("status", status).
			Msg("skipping DB persist (no database connection)")
		return
	}

	query := `
		INSERT INTO order_state_transitions (
			id, order_id, tenant_id, signal_id, broker_order_id,
			symbol, action, quantity, status, fill_price, fill_time, error, created_at
		) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
	`

	transitionID := uuid.New().String()
	_, err := e.db.ExecContext(ctx, query,
		transitionID,
		order.ID,
		order.TenantID,
		order.SignalID,
		order.BrokerOrderID,
		order.Leg.Symbol,
		order.Leg.Action,
		order.Leg.Quantity*order.Leg.LotSize,
		status,
		order.FillPrice,
		order.FillTime,
		order.Error,
		time.Now().UTC(),
	)
	if err != nil {
		e.log.Error().Err(err).
			Str("order_id", order.ID).
			Str("status", status).
			Msg("failed to persist order state transition")
	}
}

// publishFill publishes a fill confirmation to fills.{tenant_id}.{order_id}.
func (e *OrderExecutor) publishFill(tenantID, orderID string, fill broker.FillConfirmation) {
	subject := fmt.Sprintf("fills.%s.%s", tenantID, orderID)

	data, err := json.Marshal(fill)
	if err != nil {
		e.log.Error().Err(err).Str("order_id", orderID).Msg("failed to marshal fill confirmation")
		return
	}

	if err := e.nc.Publish(subject, data); err != nil {
		e.log.Error().Err(err).Str("subject", subject).Msg("failed to publish fill confirmation")
		return
	}

	e.log.Info().
		Str("subject", subject).
		Str("order_id", orderID).
		Float64("fill_price", fill.FillPrice).
		Msg("fill confirmation published")
}
