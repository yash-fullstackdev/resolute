package pool

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/order_router/internal/broker"
	"github.com/resolute/india-options-builder/services/order_router/internal/executor"
)

// BrokerPool manages the lifecycle of all per-user BrokerClient instances.
// It listens for worker lifecycle events on NATS to add/remove clients dynamically.
type BrokerPool struct {
	clients map[string]broker.BrokerClient // keyed by tenant_id
	subs    map[string]*nats.Subscription  // order subscriptions keyed by tenant_id
	mu      sync.RWMutex
	nc      *nats.Conn
	exec    *executor.OrderExecutor
	log     zerolog.Logger

	authServiceURL string
	httpClient     *http.Client
}

// NewBrokerPool creates a new BrokerPool.
func NewBrokerPool(nc *nats.Conn, exec *executor.OrderExecutor, log zerolog.Logger) *BrokerPool {
	authURL := os.Getenv("AUTH_SERVICE_URL")
	if authURL == "" {
		authURL = "http://auth-service:8000"
	}

	return &BrokerPool{
		clients:        make(map[string]broker.BrokerClient),
		subs:           make(map[string]*nats.Subscription),
		nc:             nc,
		exec:           exec,
		log:            log.With().Str("component", "broker_pool").Logger(),
		authServiceURL: authURL,
		httpClient: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// SubscribeLifecycleEvents subscribes to worker.started.* and worker.stopped.* events.
func (p *BrokerPool) SubscribeLifecycleEvents() error {
	// Subscribe to worker.started.*
	_, err := p.nc.Subscribe("worker.started.*", func(msg *nats.Msg) {
		tenantID := extractTenantID(msg.Subject, "worker.started.")
		if tenantID == "" {
			p.log.Error().Str("subject", msg.Subject).Msg("could not extract tenant_id from worker.started subject")
			return
		}

		p.log.Info().Str("tenant_id", tenantID).Msg("worker started event received")

		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()

		if err := p.Add(ctx, tenantID); err != nil {
			p.log.Error().Err(err).Str("tenant_id", tenantID).Msg("failed to add broker client")
			return
		}
	})
	if err != nil {
		return fmt.Errorf("subscribe worker.started.*: %w", err)
	}

	// Subscribe to worker.stopped.*
	_, err = p.nc.Subscribe("worker.stopped.*", func(msg *nats.Msg) {
		tenantID := extractTenantID(msg.Subject, "worker.stopped.")
		if tenantID == "" {
			p.log.Error().Str("subject", msg.Subject).Msg("could not extract tenant_id from worker.stopped subject")
			return
		}

		p.log.Info().Str("tenant_id", tenantID).Msg("worker stopped event received")
		p.Remove(tenantID)
	})
	if err != nil {
		return fmt.Errorf("subscribe worker.stopped.*: %w", err)
	}

	p.log.Info().Msg("subscribed to worker lifecycle events")
	return nil
}

// Add fetches credentials from auth_service, creates a BrokerClient, and subscribes
// to the user's validated order subject.
func (p *BrokerPool) Add(ctx context.Context, tenantID string) error {
	p.mu.Lock()
	defer p.mu.Unlock()

	// Check if already exists
	if _, exists := p.clients[tenantID]; exists {
		p.log.Warn().Str("tenant_id", tenantID).Msg("broker client already exists, replacing")
		p.removeUnlocked(tenantID)
	}

	// Fetch credentials from auth_service
	creds, err := p.fetchCredentials(ctx, tenantID)
	if err != nil {
		return fmt.Errorf("fetch credentials for %s: %w", tenantID, err)
	}

	// Create appropriate broker client
	client, err := p.createClient(creds)
	if err != nil {
		return fmt.Errorf("create broker client for %s: %w", tenantID, err)
	}

	p.clients[tenantID] = client

	// Subscribe to validated orders for this user
	subject := fmt.Sprintf("orders.new.validated.%s", tenantID)
	sub, err := p.nc.Subscribe(subject, func(msg *nats.Msg) {
		p.exec.HandleOrder(msg)
	})
	if err != nil {
		delete(p.clients, tenantID)
		return fmt.Errorf("subscribe to %s: %w", subject, err)
	}

	p.subs[tenantID] = sub

	p.log.Info().
		Str("tenant_id", tenantID).
		Str("broker", creds.Broker).
		Str("subject", subject).
		Msg("broker client added and subscribed")

	return nil
}

// Remove tears down the broker client and unsubscribes from the user's order subject.
func (p *BrokerPool) Remove(tenantID string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.removeUnlocked(tenantID)
}

func (p *BrokerPool) removeUnlocked(tenantID string) {
	if sub, exists := p.subs[tenantID]; exists {
		if err := sub.Unsubscribe(); err != nil {
			p.log.Warn().Err(err).Str("tenant_id", tenantID).Msg("error unsubscribing")
		}
		delete(p.subs, tenantID)
	}

	delete(p.clients, tenantID)
	p.log.Info().Str("tenant_id", tenantID).Msg("broker client removed")
}

// Get returns the BrokerClient for a tenant, if it exists.
func (p *BrokerPool) Get(tenantID string) (broker.BrokerClient, bool) {
	p.mu.RLock()
	defer p.mu.RUnlock()
	client, exists := p.clients[tenantID]
	return client, exists
}

// RefreshToken refreshes the broker token for a specific tenant.
func (p *BrokerPool) RefreshToken(ctx context.Context, tenantID string) error {
	p.mu.RLock()
	client, exists := p.clients[tenantID]
	p.mu.RUnlock()

	if !exists {
		return fmt.Errorf("no broker client for tenant %s", tenantID)
	}

	// Fetch fresh credentials
	creds, err := p.fetchCredentials(ctx, tenantID)
	if err != nil {
		return fmt.Errorf("fetch fresh credentials: %w", err)
	}

	// Update the access token on the existing client
	switch c := client.(type) {
	case *broker.DhanClient:
		c.UpdateAccessToken(creds.AccessToken)
	case *broker.ZerodhaClient:
		c.UpdateAccessToken(creds.AccessToken)
	case *broker.PaperBroker:
		// Paper broker doesn't need token refresh
	default:
		return fmt.Errorf("unknown broker client type for tenant %s", tenantID)
	}

	p.log.Info().Str("tenant_id", tenantID).Msg("broker token refreshed")
	return nil
}

// GetAllTenantIDs returns all active tenant IDs.
func (p *BrokerPool) GetAllTenantIDs() []string {
	p.mu.RLock()
	defer p.mu.RUnlock()

	ids := make([]string, 0, len(p.clients))
	for id := range p.clients {
		ids = append(ids, id)
	}
	return ids
}

// Shutdown cleans up all broker clients and subscriptions.
func (p *BrokerPool) Shutdown() {
	p.mu.Lock()
	defer p.mu.Unlock()

	for tenantID := range p.clients {
		p.removeUnlocked(tenantID)
	}
	p.log.Info().Msg("broker pool shut down")
}

// fetchCredentials retrieves decrypted broker credentials from the auth_service internal API.
func (p *BrokerPool) fetchCredentials(ctx context.Context, tenantID string) (broker.BrokerCredentials, error) {
	url := fmt.Sprintf("%s/internal/broker-credentials/%s", p.authServiceURL, tenantID)

	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return broker.BrokerCredentials{}, fmt.Errorf("create request: %w", err)
	}

	// Internal service auth header
	internalKey := os.Getenv("INTERNAL_API_KEY")
	if internalKey != "" {
		req.Header.Set("X-Internal-API-Key", internalKey)
	}

	resp, err := p.httpClient.Do(req)
	if err != nil {
		return broker.BrokerCredentials{}, fmt.Errorf("request auth service: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return broker.BrokerCredentials{}, fmt.Errorf("auth service returned %d: %s", resp.StatusCode, string(body))
	}

	var creds broker.BrokerCredentials
	if err := json.NewDecoder(resp.Body).Decode(&creds); err != nil {
		return broker.BrokerCredentials{}, fmt.Errorf("decode credentials: %w", err)
	}

	creds.TenantID = tenantID
	return creds, nil
}

// createClient instantiates the appropriate BrokerClient based on broker type.
func (p *BrokerPool) createClient(creds broker.BrokerCredentials) (broker.BrokerClient, error) {
	switch creds.Broker {
	case "dhan":
		return broker.NewDhanClient(creds, p.log), nil
	case "zerodha":
		return broker.NewZerodhaClient(creds, p.log), nil
	case "paper":
		slippage := 0.001 // 0.1%
		initialCash := 1_000_000.0
		return broker.NewPaperBroker(creds.TenantID, slippage, initialCash, p.log), nil
	default:
		return nil, fmt.Errorf("unsupported broker type: %s", creds.Broker)
	}
}

// extractTenantID extracts the tenant_id from a NATS subject with a given prefix.
func extractTenantID(subject, prefix string) string {
	if len(subject) <= len(prefix) {
		return ""
	}
	return subject[len(prefix):]
}
