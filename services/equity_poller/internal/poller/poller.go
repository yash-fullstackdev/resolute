package poller

import (
	"context"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/rs/zerolog"

	"github.com/resolute/india-options-builder/services/equity_poller/internal/dhan"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/derived"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/publisher"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/store"
	"github.com/resolute/india-options-builder/services/equity_poller/internal/types"
)

const (
	windowStartH = 9
	windowStartM = 14
	windowEndH   = 11
	windowEndM   = 1
	pollInterval = 60 * time.Second
	batchDelay   = 2 * time.Second
	maxBatchSize = 1000
)

// Poller runs the 60-second equity snapshot polling loop.
type Poller struct {
	dhanClient *dhan.Client
	pool       *pgxpool.Pool
	pub        *publisher.NATSPublisher
	state      *State
	logger     zerolog.Logger
	recovered  bool
}

// New creates a new Poller instance.
func New(client *dhan.Client, pool *pgxpool.Pool, pub *publisher.NATSPublisher, logger zerolog.Logger) *Poller {
	return &Poller{
		dhanClient: client,
		pool:       pool,
		pub:        pub,
		state:      NewState(),
		logger:     logger.With().Str("component", "poller").Logger(),
	}
}

// Run starts the main polling loop. Blocks until ctx is cancelled.
func (p *Poller) Run(ctx context.Context) {
	p.logger.Info().Msg("poller started, waiting for market window")

	lastTradingDate := ""
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			p.logger.Info().Msg("poller stopped")
			return
		case <-ticker.C:
		}

		now := types.NowIST()
		h := now.Hour()
		m := now.Minute()

		inWindow := (h > windowStartH || (h == windowStartH && m >= windowStartM)) &&
			(h < windowEndH || (h == windowEndH && m < windowEndM))

		today := types.TodayIST()

		// Reset daily state at date boundary.
		if today != lastTradingDate {
			p.state.Reset(today)
			p.recovered = false
			lastTradingDate = today

			// Sync scrip master on new day (before market open).
			go func() {
				count, err := dhan.SyncScripMaster(ctx, p.pool, p.logger)
				if err != nil {
					p.logger.Error().Err(err).Msg("scrip master sync failed")
				} else {
					p.logger.Info().Int("instruments", count).Msg("scrip master sync complete")
				}
			}()

			p.logger.Info().Str("trading_date", today).Msg("new trading day: state reset")
		}

		if !inWindow {
			continue
		}

		// Crash recovery: on first window entry, rebuild state from DB.
		if !p.recovered {
			p.recovered = true
			if err := RecoverFromDB(ctx, p.pool, p.state, p.logger); err != nil {
				p.logger.Error().Err(err).Msg("crash recovery failed")
			}

			// Load active instruments.
			if err := p.loadInstruments(ctx); err != nil {
				p.logger.Error().Err(err).Msg("failed to load instruments")
				continue
			}
		}

		// Execute one polling cycle.
		p.executeCycle(ctx, now)

		// Switch to 60s ticker after first cycle.
		ticker.Reset(pollInterval)
	}
}

// GetState returns the polling state (for health endpoint).
func (p *Poller) GetState() *State {
	return p.state
}

// loadInstruments loads active security IDs and symbol mapping from equity_instruments.
func (p *Poller) loadInstruments(ctx context.Context) error {
	rows, err := p.pool.Query(ctx,
		`SELECT security_id, symbol FROM equity_instruments
		 WHERE enabled = true AND tiers @> ARRAY['F&O']::text[]`,
	)
	if err != nil {
		return err
	}
	defer rows.Close()

	secIDs := make([]string, 0, 500)
	symMap := make(map[string]string)

	for rows.Next() {
		var secID, symbol string
		if err := rows.Scan(&secID, &symbol); err != nil {
			continue
		}
		secIDs = append(secIDs, secID)
		symMap[secID] = symbol
	}

	p.state.SecurityIDs = secIDs
	p.state.SymbolMap = symMap

	p.logger.Info().Int("instruments", len(secIDs)).Msg("loaded active instruments")
	return nil
}

// executeCycle runs one polling iteration: fetch quotes, compute derived, publish, persist.
func (p *Poller) executeCycle(ctx context.Context, now time.Time) {
	cycleStart := time.Now()
	bucket := types.BucketFromTime(now)
	tradingDate := types.TodayIST()

	if len(p.state.SecurityIDs) == 0 {
		p.logger.Warn().Msg("no instruments loaded, skipping cycle")
		return
	}

	// -- Fetch quotes in batches of 1000 --
	fetchStart := time.Now()
	allQuotes := make(map[string]*dhan.QuoteItem)
	fetchOK := true

	chunks := chunkStrings(p.state.SecurityIDs, maxBatchSize)
	for i, chunk := range chunks {
		if i > 0 {
			time.Sleep(batchDelay)
		}

		quotes, statusCode, errBody, err := dhan.FetchQuotes(p.dhanClient, chunk, p.logger)
		if err != nil {
			p.logger.Warn().
				Err(err).
				Int("batch_index", i).
				Int("batch_size", len(chunk)).
				Int("status_code", statusCode).
				Msg("quote API error")

			// Log to audit_events asynchronously.
			go store.LogAPIError(ctx, p.pool, "/marketfeed/quote", statusCode, errBody, i, len(chunk), p.logger)

			fetchOK = false
			break
		}

		for k, v := range quotes {
			allQuotes[k] = v
		}
	}

	fetchMs := time.Since(fetchStart).Milliseconds()

	if !fetchOK {
		p.state.ConsecutiveFailures++
		p.state.SetLastMetrics(types.CycleMetrics{
			QuoteFetchMs:    fetchMs,
			CycleTotalMs:    time.Since(cycleStart).Milliseconds(),
			StocksProcessed: 0,
			Errors:          1,
		})
		p.logger.Warn().
			Int("consecutive_failures", p.state.ConsecutiveFailures).
			Msg("polling cycle failed")
		return
	}
	p.state.ConsecutiveFailures = 0

	// -- Compute derived fields --
	derivedStart := time.Now()
	snapshots := make([]types.Snapshot, 0, len(allQuotes))

	for secID, quote := range allQuotes {
		symbol, ok := p.state.SymbolMap[secID]
		if !ok {
			continue
		}

		rs := p.state.GetDerivedState(secID)
		d := derived.Compute(
			quote.LastPrice, quote.OHLC.Open, quote.OHLC.High, quote.OHLC.Low,
			quote.Volume, quote.OI, quote.BidPrice(), quote.AskPrice(), rs,
		)

		snapshots = append(snapshots, types.Snapshot{
			Symbol:          symbol,
			SecurityID:      secID,
			TradingDate:     tradingDate,
			Bucket:          bucket,
			LTP:             quote.LastPrice,
			CandleOpen:      quote.OHLC.Open,
			CandleHigh:      quote.OHLC.High,
			CandleLow:       quote.OHLC.Low,
			VolumeCum:       quote.Volume,
			VolumeDelta:     d.VolumeDelta,
			OITotal:         quote.OI,
			OIDelta:         d.OIDelta,
			Bid:             quote.BidPrice(),
			Ask:             quote.AskPrice(),
			BidQty:          quote.BidQty(),
			AskQty:          quote.AskQty(),
			VWAP:            d.VWAP,
			SpreadPct:       d.SpreadPct,
			PriceVelocity:   d.PriceVelocity,
			VolumeRate:      d.VolumeRate,
			CandleBodyRatio: d.CandleBodyRatio,
		})
	}

	derivedMs := time.Since(derivedStart).Milliseconds()

	// -- Publish to NATS --
	publishStart := time.Now()
	batch := types.BatchMessage{
		Bucket:      bucket,
		TradingDate: tradingDate,
		Timestamp:   now.Unix(),
		Stocks:      snapshots,
	}

	if err := p.pub.PublishBatch(batch); err != nil {
		p.logger.Error().Err(err).Msg("NATS publish failed")
	}
	publishMs := time.Since(publishStart).Milliseconds()

	// -- Persist to DB asynchronously (never block polling loop) --
	persistStart := time.Now()
	go func(snaps []types.Snapshot) {
		persistBegin := time.Now()
		if err := store.InsertSnapshots(ctx, p.pool, snaps); err != nil {
			p.logger.Error().Err(err).Msg("snapshot DB persist failed")
		}
		p.logger.Debug().
			Int64("db_persist_ms", time.Since(persistBegin).Milliseconds()).
			Int("rows", len(snaps)).
			Msg("snapshots persisted")
	}(snapshots)
	persistMs := time.Since(persistStart).Milliseconds()

	p.state.LastBucket = bucket

	metrics := types.CycleMetrics{
		QuoteFetchMs:     fetchMs,
		DerivedComputeMs: derivedMs,
		DBPersistMs:      persistMs,
		NATSPublishMs:    publishMs,
		CycleTotalMs:     time.Since(cycleStart).Milliseconds(),
		StocksProcessed:  len(snapshots),
		Errors:           0,
	}
	p.state.SetLastMetrics(metrics)

	p.logger.Info().
		Uint16("bucket", bucket).
		Int64("quote_fetch_ms", fetchMs).
		Int64("derived_compute_ms", derivedMs).
		Int64("db_persist_ms", persistMs).
		Int64("nats_publish_ms", publishMs).
		Int64("cycle_total_ms", metrics.CycleTotalMs).
		Int("stocks_processed", len(snapshots)).
		Msg("polling cycle complete")
}

// chunkStrings splits a slice into chunks of at most size n.
func chunkStrings(s []string, n int) [][]string {
	var chunks [][]string
	for i := 0; i < len(s); i += n {
		end := i + n
		if end > len(s) {
			end = len(s)
		}
		chunks = append(chunks, s[i:end])
	}
	return chunks
}
