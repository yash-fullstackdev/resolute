package store

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

// InstrumentRow holds a security_id -> symbol mapping.
type InstrumentRow struct {
	SecurityID string
	Symbol     string
}

// LoadActiveInstruments loads all enabled instruments from equity_instruments.
func LoadActiveInstruments(ctx context.Context, pool *pgxpool.Pool) ([]InstrumentRow, error) {
	rows, err := pool.Query(ctx,
		`SELECT security_id, symbol FROM equity_instruments
		 WHERE enabled = true
		 ORDER BY symbol`,
	)
	if err != nil {
		return nil, fmt.Errorf("query equity_instruments: %w", err)
	}
	defer rows.Close()

	var instruments []InstrumentRow
	for rows.Next() {
		var r InstrumentRow
		if err := rows.Scan(&r.SecurityID, &r.Symbol); err != nil {
			continue
		}
		instruments = append(instruments, r)
	}

	return instruments, rows.Err()
}

// GetInstrumentCount returns the number of enabled equity instruments in the DB.
func GetInstrumentCount(ctx context.Context, pool *pgxpool.Pool) (int, error) {
	var count int
	err := pool.QueryRow(ctx,
		`SELECT COUNT(*) FROM equity_instruments WHERE enabled = true`,
	).Scan(&count)
	if err != nil {
		return 0, fmt.Errorf("count equity_instruments: %w", err)
	}
	return count, nil
}
