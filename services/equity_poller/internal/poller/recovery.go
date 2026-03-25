package poller

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/derived"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

// RecoverFromDB reloads today's snapshots from equity_snapshots and rebuilds
// the in-memory derived state. This allows the poller to resume correctly
// after a crash mid-session.
func RecoverFromDB(ctx context.Context, pool *pgxpool.Pool, state *State, logger zerolog.Logger) error {
	tradingDate := types.TodayIST()
	state.TradingDate = tradingDate

	rows, err := pool.Query(ctx,
		`SELECT security_id, symbol, bucket, ltp, candle_open, candle_high, candle_low,
		        volume_cum, oi_total, bid, ask
		 FROM equity_snapshots
		 WHERE trading_date = $1
		 ORDER BY security_id, bucket ASC`,
		tradingDate,
	)
	if err != nil {
		return fmt.Errorf("query equity_snapshots for recovery: %w", err)
	}
	defer rows.Close()

	recovered := 0
	maxBucket := uint16(0)

	for rows.Next() {
		var (
			secID, symbol                           string
			bucket                                  uint16
			ltp, candleOpen, candleHigh, candleLow  float32
			volumeCum, oiTotal                      uint64
			bid, ask                                float32
		)
		if err := rows.Scan(&secID, &symbol, &bucket, &ltp, &candleOpen, &candleHigh,
			&candleLow, &volumeCum, &oiTotal, &bid, &ask); err != nil {
			logger.Warn().Err(err).Msg("skip malformed recovery row")
			continue
		}

		state.SymbolMap[secID] = symbol

		// Replay through the derived compute to rebuild running VWAP state.
		rs := state.GetDerivedState(secID)
		derived.Compute(ltp, candleOpen, candleHigh, candleLow, volumeCum, oiTotal, bid, ask, rs)

		if bucket > maxBucket {
			maxBucket = bucket
		}
		recovered++
	}

	if err := rows.Err(); err != nil {
		return fmt.Errorf("row iteration error: %w", err)
	}

	state.LastBucket = maxBucket

	if recovered > 0 {
		uniqueSymbols := len(state.DerivedState)
		logger.Warn().
			Int("rows_replayed", recovered).
			Int("unique_symbols", uniqueSymbols).
			Uint16("max_bucket", maxBucket).
			Msg("crash recovery: rebuilt derived state from DB")
	} else {
		logger.Info().Msg("crash recovery: no snapshots found for today, starting fresh")
	}

	return nil
}
