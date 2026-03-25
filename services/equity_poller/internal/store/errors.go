package store

import (
	"context"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/rs/zerolog"
)

// LogAPIError persists a Dhan API error to the audit_events table.
// Called from a goroutine — must not block the polling loop.
func LogAPIError(
	ctx context.Context,
	pool *pgxpool.Pool,
	endpoint string,
	statusCode int,
	errorBody string,
	batchIndex int,
	symbolsInBatch int,
	logger zerolog.Logger,
) {
	// Truncate error body to prevent oversized inserts.
	if len(errorBody) > 2000 {
		errorBody = errorBody[:2000]
	}

	_, err := pool.Exec(ctx,
		`INSERT INTO audit_events (event_type, service, endpoint, status_code, error_body, batch_index, symbols_in_batch, created_at)
		 VALUES ('api_error', 'equity_poller', $1, $2, $3, $4, $5, NOW())`,
		endpoint, statusCode, errorBody, batchIndex, symbolsInBatch,
	)
	if err != nil {
		logger.Error().
			Err(err).
			Str("endpoint", endpoint).
			Int("status_code", statusCode).
			Msg("failed to persist API error to audit_events")
	}
}
