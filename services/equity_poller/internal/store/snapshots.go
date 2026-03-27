package store

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

// InsertSnapshots bulk inserts snapshots into equity_snapshots (TimescaleDB hypertable).
// Called from a goroutine — must not block the polling loop.
func InsertSnapshots(ctx context.Context, pool *pgxpool.Pool, snapshots []types.Snapshot) error {
	if len(snapshots) == 0 {
		return nil
	}

	tx, err := pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	for _, s := range snapshots {
		_, err := tx.Exec(ctx,
			`INSERT INTO equity_snapshots (
				trading_date, bucket, security_id, symbol,
				ltp, candle_open, candle_high, candle_low,
				volume_cum, volume_delta,
				bid, ask, bid_qty, ask_qty,
				vwap, spread_pct, price_velocity, volume_rate, candle_body_ratio,
				time
			) VALUES (
				$1, $2, $3, $4,
				$5, $6, $7, $8,
				$9, $10,
				$11, $12, $13, $14,
				$15, $16, $17, $18, $19,
				NOW()
			)`,
			s.TradingDate, s.Bucket, s.SecurityID, s.Symbol,
			s.LTP, s.CandleOpen, s.CandleHigh, s.CandleLow,
			s.VolumeCum, s.VolumeDelta,
			s.Bid, s.Ask, s.BidQty, s.AskQty,
			s.VWAP, s.SpreadPct, s.PriceVelocity, s.VolumeRate, s.CandleBodyRatio,
		)
		if err != nil {
			return fmt.Errorf("insert snapshot %s bucket %d: %w", s.SecurityID, s.Bucket, err)
		}
	}

	return tx.Commit(ctx)
}
