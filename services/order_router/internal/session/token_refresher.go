package session

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/pquerna/otp/totp"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/order_router/internal/pool"
)

const (
	// refreshHour is the hour in IST (07:00) when daily token refresh runs.
	refreshHour   = 7
	refreshMinute = 0
)

// TokenRefresher manages daily automated token refresh for all active users.
// At 07:00 IST each morning (before market open at 09:15), it refreshes all
// active broker tokens using stored TOTP secrets.
type TokenRefresher struct {
	pool   *pool.BrokerPool
	nc     *nats.Conn
	log    zerolog.Logger
	cancel context.CancelFunc
	done   chan struct{}

	istLocation *time.Location
}

// NewTokenRefresher creates a new daily token refresh manager.
func NewTokenRefresher(bp *pool.BrokerPool, nc *nats.Conn, log zerolog.Logger) (*TokenRefresher, error) {
	ist, err := time.LoadLocation("Asia/Kolkata")
	if err != nil {
		return nil, fmt.Errorf("load IST timezone: %w", err)
	}

	return &TokenRefresher{
		pool:        bp,
		nc:          nc,
		log:         log.With().Str("component", "token_refresher").Logger(),
		done:        make(chan struct{}),
		istLocation: ist,
	}, nil
}

// Start begins the daily token refresh scheduler. It calculates the duration
// until the next 07:00 IST and schedules the refresh cycle.
func (tr *TokenRefresher) Start() {
	ctx, cancel := context.WithCancel(context.Background())
	tr.cancel = cancel

	go tr.run(ctx)
	tr.log.Info().Msg("token refresher started")
}

// Stop halts the token refresher.
func (tr *TokenRefresher) Stop() {
	if tr.cancel != nil {
		tr.cancel()
	}
	<-tr.done
	tr.log.Info().Msg("token refresher stopped")
}

func (tr *TokenRefresher) run(ctx context.Context) {
	defer close(tr.done)

	for {
		// Calculate time until next 07:00 IST
		now := time.Now().In(tr.istLocation)
		next := time.Date(now.Year(), now.Month(), now.Day(), refreshHour, refreshMinute, 0, 0, tr.istLocation)
		if now.After(next) {
			next = next.Add(24 * time.Hour)
		}

		waitDuration := time.Until(next)
		tr.log.Info().
			Time("next_refresh", next).
			Dur("wait_duration", waitDuration).
			Msg("scheduled next token refresh")

		select {
		case <-ctx.Done():
			return
		case <-time.After(waitDuration):
			tr.refreshAllTokens(ctx)
		}
	}
}

// refreshAllTokens iterates over all active tenants and refreshes their tokens.
func (tr *TokenRefresher) refreshAllTokens(ctx context.Context) {
	tenantIDs := tr.pool.GetAllTenantIDs()
	tr.log.Info().Int("tenant_count", len(tenantIDs)).Msg("starting daily token refresh")

	successCount := 0
	failCount := 0

	for _, tenantID := range tenantIDs {
		select {
		case <-ctx.Done():
			tr.log.Warn().Msg("token refresh interrupted by shutdown")
			return
		default:
		}

		if err := tr.refreshTenantToken(ctx, tenantID); err != nil {
			failCount++
			tr.log.Error().Err(err).Str("tenant_id", tenantID).Msg("token refresh failed")

			// Publish failure event
			subject := fmt.Sprintf("broker.token_refresh_failed.%s", tenantID)
			payload := fmt.Sprintf(`{"tenant_id":"%s","error":"%s","timestamp":"%s"}`,
				tenantID, err.Error(), time.Now().UTC().Format(time.RFC3339))
			if pubErr := tr.nc.Publish(subject, []byte(payload)); pubErr != nil {
				tr.log.Error().Err(pubErr).Str("subject", subject).Msg("failed to publish token_refresh_failed")
			}

			// Publish alert
			alertSubject := fmt.Sprintf("alerts.order_failure.%s", tenantID)
			alertPayload := fmt.Sprintf(`{"tenant_id":"%s","type":"TOKEN_REFRESH_FAILED","message":"%s","timestamp":"%s"}`,
				tenantID, err.Error(), time.Now().UTC().Format(time.RFC3339))
			_ = tr.nc.Publish(alertSubject, []byte(alertPayload))
		} else {
			successCount++

			// Publish success event
			subject := fmt.Sprintf("broker.token_refreshed.%s", tenantID)
			payload := fmt.Sprintf(`{"tenant_id":"%s","timestamp":"%s"}`,
				tenantID, time.Now().UTC().Format(time.RFC3339))
			if pubErr := tr.nc.Publish(subject, []byte(payload)); pubErr != nil {
				tr.log.Error().Err(pubErr).Str("subject", subject).Msg("failed to publish token_refreshed")
			}
		}
	}

	tr.log.Info().
		Int("success", successCount).
		Int("failed", failCount).
		Int("total", len(tenantIDs)).
		Msg("daily token refresh complete")
}

// refreshTenantToken refreshes a single tenant's broker token.
// For Dhan: generates TOTP code from stored secret and authenticates.
func (tr *TokenRefresher) refreshTenantToken(ctx context.Context, tenantID string) error {
	// Delegate to broker pool which handles the actual credential fetch and update
	if err := tr.pool.RefreshToken(ctx, tenantID); err != nil {
		return fmt.Errorf("refresh token for %s: %w", tenantID, err)
	}

	return nil
}

// GenerateTOTP generates a TOTP code from a base32-encoded secret.
// Used by Dhan for automated login token generation.
func GenerateTOTP(secret string) (string, error) {
	if secret == "" {
		return "", fmt.Errorf("TOTP secret is empty")
	}

	code, err := totp.GenerateCode(secret, time.Now())
	if err != nil {
		return "", fmt.Errorf("generate TOTP code: %w", err)
	}

	return code, nil
}

// GetTOTPSecret returns the TOTP secret from environment (for testing) or empty string.
func GetTOTPSecret() string {
	return os.Getenv("DHAN_TOTP_SECRET")
}
