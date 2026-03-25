package dhan

import (
	"context"
	"encoding/csv"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

const scripMasterURL = "https://images.dhan.co/api-data/api-scrip-master.csv"

// SyncScripMaster downloads the Dhan scrip master CSV and upserts NSE EQ
// instruments into the equity_instruments table.
func SyncScripMaster(ctx context.Context, pool *pgxpool.Pool, logger zerolog.Logger) (int, error) {
	logger.Info().Msg("downloading scrip master from Dhan")

	client := &http.Client{Timeout: 120 * time.Second}
	resp, err := client.Get(scripMasterURL)
	if err != nil {
		return 0, fmt.Errorf("scrip master download: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("scrip master HTTP %d", resp.StatusCode)
	}

	reader := csv.NewReader(resp.Body)
	reader.LazyQuotes = true
	reader.FieldsPerRecord = -1 // Variable field count.

	// Read header to find column indices.
	header, err := reader.Read()
	if err != nil {
		return 0, fmt.Errorf("read CSV header: %w", err)
	}

	colIdx := make(map[string]int)
	for i, col := range header {
		colIdx[strings.TrimSpace(col)] = i
	}

	// Required columns.
	requiredCols := []string{
		"SEM_EXM_EXCH_ID", "SEM_SMST_SECURITY_ID", "SEM_INSTRUMENT_NAME",
		"SEM_TRADING_SYMBOL", "SEM_SERIES", "SM_SYMBOL_NAME",
	}
	for _, c := range requiredCols {
		if _, ok := colIdx[c]; !ok {
			return 0, fmt.Errorf("missing column %s in scrip master CSV", c)
		}
	}

	// Pass 1: collect F&O symbols from FUTSTK rows.
	var allRecords [][]string
	foSymbols := make(map[string]bool)

	for {
		record, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			continue
		}
		allRecords = append(allRecords, record)

		exchange := getField(record, colIdx, "SEM_EXM_EXCH_ID")
		instrument := getField(record, colIdx, "SEM_INSTRUMENT_NAME")
		rawSymbol := getField(record, colIdx, "SEM_TRADING_SYMBOL")

		if exchange == "NSE" && instrument == "FUTSTK" && rawSymbol != "" {
			base := strings.Split(rawSymbol, "-")[0]
			foSymbols[base] = true
		}
	}

	// Pass 2: collect NSE EQ instruments.
	var instruments []types.Instrument
	for _, record := range allRecords {
		exchange := getField(record, colIdx, "SEM_EXM_EXCH_ID")
		series := getField(record, colIdx, "SEM_SERIES")
		securityID := getField(record, colIdx, "SEM_SMST_SECURITY_ID")
		symbol := getField(record, colIdx, "SEM_TRADING_SYMBOL")
		company := getField(record, colIdx, "SM_SYMBOL_NAME")

		if exchange != "NSE" || series != "EQ" {
			continue
		}
		if securityID == "" || symbol == "" {
			continue
		}

		instruments = append(instruments, types.Instrument{
			SecurityID:  securityID,
			Symbol:      symbol,
			CompanyName: company,
			Exchange:    exchange,
			Segment:     "NSE_EQ",
			Series:      series,
			IsFnO:       foSymbols[symbol],
		})
	}

	logger.Info().Int("count", len(instruments)).Msg("parsed NSE EQ instruments from scrip master")

	// Bulk upsert to equity_instruments.
	if len(instruments) == 0 {
		return 0, nil
	}

	tx, err := pool.Begin(ctx)
	if err != nil {
		return 0, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback(ctx)

	for _, inst := range instruments {
		_, err := tx.Exec(ctx,
			`INSERT INTO equity_instruments (security_id, symbol, company_name, exchange, segment, series, is_fno, updated_at)
			 VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
			 ON CONFLICT (security_id) DO UPDATE SET
				symbol = EXCLUDED.symbol,
				company_name = EXCLUDED.company_name,
				is_fno = EXCLUDED.is_fno,
				updated_at = NOW()`,
			inst.SecurityID, inst.Symbol, inst.CompanyName,
			inst.Exchange, inst.Segment, inst.Series, inst.IsFnO,
		)
		if err != nil {
			logger.Warn().Str("security_id", inst.SecurityID).Err(err).Msg("upsert instrument failed")
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return 0, fmt.Errorf("commit tx: %w", err)
	}

	logger.Info().Int("upserted", len(instruments)).Msg("equity_instruments sync complete")
	return len(instruments), nil
}

func getField(record []string, colIdx map[string]int, col string) string {
	idx, ok := colIdx[col]
	if !ok || idx >= len(record) {
		return ""
	}
	return strings.TrimSpace(record[idx])
}
