package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
	"gopkg.in/yaml.v3"

	"github.com/resolute/india-options-builder/services/feed_gateway/internal/feed"
	"github.com/resolute/india-options-builder/services/feed_gateway/internal/health"
	"github.com/resolute/india-options-builder/services/feed_gateway/internal/publisher"
)

// Config holds the feed gateway configuration loaded from YAML and env vars.
type Config struct {
	FeedGateway struct {
		Broker           string   `yaml:"broker"`
		APIKey           string   `yaml:"api_key"`
		AccessToken      string   `yaml:"access_token"`
		NATSUrl          string   `yaml:"nats_url"`
		ReconnectMaxWait string   `yaml:"reconnect_max_wait"`
		StaleFeedTimeout string   `yaml:"stale_feed_timeout"`
		Symbols          []string `yaml:"symbols"`
	} `yaml:"feed_gateway"`
}

// IST timezone.
var istLocation *time.Location

func init() {
	var err error
	istLocation, err = time.LoadLocation("Asia/Kolkata")
	if err != nil {
		// Fallback: IST is UTC+5:30.
		istLocation = time.FixedZone("IST", 5*3600+30*60)
	}
}

func main() {
	// Structured JSON logging via zerolog.
	zerolog.TimeFieldFormat = zerolog.TimeFormatUnixMs
	log.Logger = zerolog.New(os.Stdout).With().Timestamp().Str("service", "feed_gateway").Logger()

	log.Info().Msg("feed_gateway starting")

	// Load configuration.
	cfg, err := loadConfig()
	if err != nil {
		log.Fatal().Err(err).Msg("failed to load configuration")
	}

	// Build symbol configs with realistic base prices.
	symbolConfigs := buildSymbolConfigs(cfg.FeedGateway.Symbols)
	mapping := feed.NewSymbolMapping(symbolConfigs)

	// Set up health status tracker.
	healthStatus := health.NewStatus()

	// Start health check and metrics servers.
	healthServer := health.NewServer(healthStatus)
	healthServer.Start()

	// Connect to NATS.
	natsURL := cfg.FeedGateway.NATSUrl
	if natsURL == "" {
		natsURL = "nats://localhost:4222"
	}

	pub, err := publisher.NewNATSPublisher(natsURL)
	if err != nil {
		log.Fatal().Err(err).Str("nats_url", natsURL).Msg("failed to connect to NATS")
	}
	defer pub.Close()

	// Determine broker type from config, with FEED_BROKER env override.
	broker := cfg.FeedGateway.Broker
	if envBroker := os.Getenv("FEED_BROKER"); envBroker != "" {
		broker = envBroker
	}
	if broker == "" {
		broker = "paper"
	}

	log.Info().
		Str("broker", broker).
		Int("symbol_count", len(symbolConfigs)).
		Str("nats_url", natsURL).
		Msg("configuration loaded")

	// Create context for graceful shutdown.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Signal handling for graceful shutdown.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)

	// Create feed provider based on broker type.
	provider := createFeedProvider(broker, symbolConfigs, mapping, cfg)

	// Track total ticks for heartbeat.
	var totalTicks atomic.Int64

	// Register tick handler: publish to NATS.
	provider.OnTick(func(tick feed.Tick) {
		subject := feed.DeriveNATSSubject(tick)
		if err := pub.Publish(subject, tick); err != nil {
			log.Error().Err(err).Str("symbol", tick.Symbol).Str("subject", subject).Msg("publish failed")
			return
		}
		totalTicks.Add(1)
		healthStatus.IncrementTotalTicks()
		healthStatus.SetLastTickTime(time.Now())
	})

	// Register error handler.
	provider.OnError(func(err error) {
		log.Error().Err(err).Msg("feed provider error")
		healthStatus.IncrementReconnectCount()
	})

	// Market hours guard: wait until market is open before connecting.
	var wg sync.WaitGroup

	wg.Add(1)
	go func() {
		defer wg.Done()
		runFeedWithMarketGuard(ctx, provider, broker, healthStatus)
	}()

	// Heartbeat: publish every 10s.
	wg.Add(1)
	go func() {
		defer wg.Done()
		runHeartbeat(ctx, pub, len(symbolConfigs), &totalTicks, healthStatus)
	}()

	// Stale feed detector.
	staleFeedTimeout := 30 * time.Second
	if cfg.FeedGateway.StaleFeedTimeout != "" {
		if d, err := time.ParseDuration(cfg.FeedGateway.StaleFeedTimeout); err == nil {
			staleFeedTimeout = d
		}
	}

	wg.Add(1)
	go func() {
		defer wg.Done()
		runStaleFeedDetector(ctx, pub, symbolConfigs, staleFeedTimeout)
	}()

	// Wait for shutdown signal.
	sig := <-sigCh
	log.Info().Str("signal", sig.String()).Msg("received shutdown signal")
	cancel()

	// Give goroutines time to clean up.
	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	// Close feed provider.
	if err := provider.Close(); err != nil {
		log.Error().Err(err).Msg("error closing feed provider")
	}

	// Shutdown health servers.
	healthServer.Shutdown(shutdownCtx)

	// Wait for background goroutines.
	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
		log.Info().Msg("all goroutines stopped")
	case <-shutdownCtx.Done():
		log.Warn().Msg("shutdown timed out, forcing exit")
	}

	log.Info().Msg("feed_gateway stopped")
}

// loadConfig loads configuration from YAML file and environment variable overrides.
func loadConfig() (*Config, error) {
	cfg := &Config{}

	// Try loading from YAML file.
	configPath := os.Getenv("CONFIG_PATH")
	if configPath == "" {
		configPath = "config.yaml"
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		log.Warn().Str("path", configPath).Msg("config file not found, using env vars and defaults")
	} else {
		if err := yaml.Unmarshal(data, cfg); err != nil {
			return nil, fmt.Errorf("failed to parse config YAML: %w", err)
		}
	}

	// Environment variable overrides.
	if v := os.Getenv("FEED_BROKER"); v != "" {
		cfg.FeedGateway.Broker = v
	}
	if v := os.Getenv("BROKER_API_KEY"); v != "" {
		cfg.FeedGateway.APIKey = v
	}
	if v := os.Getenv("BROKER_ACCESS_TOKEN"); v != "" {
		cfg.FeedGateway.AccessToken = v
	}
	if v := os.Getenv("NATS_URL"); v != "" {
		cfg.FeedGateway.NATSUrl = v
	}

	// Default symbols if none configured.
	if len(cfg.FeedGateway.Symbols) == 0 {
		cfg.FeedGateway.Symbols = []string{
			"NIFTY",
			"BANKNIFTY",
			"FINNIFTY",
			"NSE:RELIANCE",
			"NSE:HDFCBANK",
			"NSE:INFY",
			"NSE:TCS",
			"NSE:ICICIBANK",
			"NSE:SBIN",
		}
	}

	return cfg, nil
}

// buildSymbolConfigs creates SymbolConfig entries with realistic base prices.
func buildSymbolConfigs(symbols []string) []feed.SymbolConfig {
	// Base prices for common Indian market instruments (approximate).
	basePrices := map[string]float64{
		"NIFTY":         24500.0,
		"BANKNIFTY":     51800.0,
		"FINNIFTY":      23200.0,
		"MIDCPNIFTY":    12500.0,
		"MCX:GOLD":      72500.0,
		"MCX:CRUDEOIL":  6800.0,
		"MCX:SILVER":    87000.0,
		"MCX:NATURALGAS": 250.0,
		"NSE:RELIANCE":  2900.0,
		"NSE:TCS":       4100.0,
		"NSE:INFY":      1850.0,
		"NSE:HDFCBANK":  1750.0,
		"NSE:ICICIBANK": 1280.0,
		"NSE:SBIN":      780.0,
	}

	// Lot sizes for common instruments.
	lotSizes := map[string]int{
		"NIFTY":         25,
		"BANKNIFTY":     15,
		"FINNIFTY":      25,
		"MIDCPNIFTY":    50,
		"MCX:GOLD":      1,
		"MCX:CRUDEOIL":  100,
		"MCX:SILVER":    30,
		"MCX:NATURALGAS": 1250,
	}

	var configs []feed.SymbolConfig
	tokenCounter := int32(1000)

	for _, sym := range symbols {
		segment := classifySegment(sym)
		base, ok := basePrices[sym]
		if !ok {
			base = 1000.0 // Default base price.
		}
		lotSize, ok := lotSizes[sym]
		if !ok {
			lotSize = 1
		}

		configs = append(configs, feed.SymbolConfig{
			Symbol:    sym,
			Segment:   segment,
			Token:     tokenCounter,
			BasePrice: base,
			LotSize:   lotSize,
		})
		tokenCounter++
	}

	return configs
}

// classifySegment determines the market segment for a symbol.
func classifySegment(symbol string) string {
	if len(symbol) > 4 && symbol[:4] == "MCX:" {
		return feed.SegmentMCX
	}
	if len(symbol) > 4 && symbol[:4] == "NSE:" {
		// Individual equities are treated as NSE_INDEX for tick routing.
		return feed.SegmentNSEIndex
	}
	// Default: NSE indices.
	return feed.SegmentNSEIndex
}

// createFeedProvider instantiates the appropriate feed provider.
func createFeedProvider(broker string, symbols []feed.SymbolConfig, mapping *feed.SymbolMapping, cfg *Config) feed.FeedProvider {
	switch broker {
	case "dhan":
		accessToken := cfg.FeedGateway.AccessToken
		if accessToken == "" {
			accessToken = os.Getenv("FEED_ACCESS_TOKEN")
		}
		clientID := os.Getenv("FEED_CLIENT_ID")
		if accessToken == "" {
			log.Warn().Msg("dhan access token not configured, falling back to paper feed")
			return feed.NewPaperFeedProvider(symbols)
		}
		log.Info().Str("broker", "dhan").Msg("using Dhan WebSocket feed provider")
		return feed.NewDhanWSFeedProvider(accessToken, clientID, symbols)
	case "zerodha":
		// Zerodha integration not yet implemented.
		log.Warn().Str("broker", broker).Msg("broker integration not yet implemented, falling back to paper feed")
		return feed.NewPaperFeedProvider(symbols)
	case "paper":
		return feed.NewPaperFeedProvider(symbols)
	default:
		log.Warn().Str("broker", broker).Msg("unknown broker, using paper feed")
		return feed.NewPaperFeedProvider(symbols)
	}
}

// runFeedWithMarketGuard connects the feed provider only during market hours.
// NSE: 09:00-15:35 IST, MCX: 09:00-23:30 IST.
// In paper mode, we skip the market hours guard for development convenience
// but still log warnings outside market hours.
func runFeedWithMarketGuard(ctx context.Context, provider feed.FeedProvider, broker string, status *health.Status) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		isPaperMode := broker == "paper"

		if !isPaperMode {
			// Wait for market hours before connecting.
			if !isAnyMarketOpen() {
				log.Info().
					Str("nse_open", nextNSEOpen().In(istLocation).Format("15:04:05")).
					Msg("outside market hours, waiting...")
				// Sleep and re-check every 30 seconds.
				select {
				case <-ctx.Done():
					return
				case <-time.After(30 * time.Second):
					continue
				}
			}
		} else {
			if !isAnyMarketOpen() {
				log.Warn().Msg("outside market hours (paper mode continues anyway)")
			}
		}

		// Connect the feed provider.
		if err := provider.Connect(ctx); err != nil {
			log.Error().Err(err).Msg("feed provider connect failed, will retry")
			status.SetFeedConnected(false)
			// Exponential backoff is handled by the provider; simple retry here.
			select {
			case <-ctx.Done():
				return
			case <-time.After(5 * time.Second):
				continue
			}
		}

		status.SetFeedConnected(true)
		log.Info().Msg("feed provider connected and streaming")

		// In paper mode, just wait for context cancellation.
		// In live mode, monitor market close and disconnect.
		if isPaperMode {
			<-ctx.Done()
			return
		}

		// Monitor for market close.
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				if !isAnyMarketOpen() {
					log.Info().Msg("market hours ended, disconnecting feed")
					provider.Close()
					status.SetFeedConnected(false)
					// Break inner loop, outer loop will wait for next market open.
					goto waitForOpen
				}
			}
		}
	waitForOpen:
		continue
	}
}

// isNSEMarketOpen checks if NSE is currently in trading hours: 09:00-15:35 IST.
func isNSEMarketOpen() bool {
	now := time.Now().In(istLocation)
	weekday := now.Weekday()
	if weekday == time.Saturday || weekday == time.Sunday {
		return false
	}

	hour, min, _ := now.Clock()
	minutes := hour*60 + min

	// NSE: 09:00 (540 min) to 15:35 (935 min).
	return minutes >= 540 && minutes <= 935
}

// isMCXMarketOpen checks if MCX is currently in trading hours: 09:00-23:30 IST.
func isMCXMarketOpen() bool {
	now := time.Now().In(istLocation)
	weekday := now.Weekday()
	if weekday == time.Saturday || weekday == time.Sunday {
		return false
	}

	hour, min, _ := now.Clock()
	minutes := hour*60 + min

	// MCX: 09:00 (540 min) to 23:30 (1410 min).
	return minutes >= 540 && minutes <= 1410
}

// isAnyMarketOpen returns true if either NSE or MCX is in trading hours.
func isAnyMarketOpen() bool {
	return isNSEMarketOpen() || isMCXMarketOpen()
}

// nextNSEOpen returns the next NSE opening time.
func nextNSEOpen() time.Time {
	now := time.Now().In(istLocation)
	today9am := time.Date(now.Year(), now.Month(), now.Day(), 9, 0, 0, 0, istLocation)

	if now.Before(today9am) && now.Weekday() != time.Saturday && now.Weekday() != time.Sunday {
		return today9am
	}

	// Find next weekday.
	next := now.AddDate(0, 0, 1)
	for next.Weekday() == time.Saturday || next.Weekday() == time.Sunday {
		next = next.AddDate(0, 0, 1)
	}
	return time.Date(next.Year(), next.Month(), next.Day(), 9, 0, 0, 0, istLocation)
}

// runHeartbeat publishes a heartbeat message every 10 seconds.
func runHeartbeat(ctx context.Context, pub *publisher.NATSPublisher, symbolCount int, totalTicks *atomic.Int64, status *health.Status) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := pub.PublishHeartbeat(symbolCount, totalTicks.Load(), status.Uptime()); err != nil {
				log.Error().Err(err).Msg("failed to publish heartbeat")
			}
		}
	}
}

// runStaleFeedDetector checks for symbols that haven't received ticks within the timeout
// during market hours, and publishes STALE_FEED alerts.
func runStaleFeedDetector(ctx context.Context, pub *publisher.NATSPublisher, symbols []feed.SymbolConfig, timeout time.Duration) {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if !isAnyMarketOpen() {
				continue
			}

			now := time.Now()
			for _, sym := range symbols {
				// Check market-specific hours.
				if sym.Segment == feed.SegmentMCX && !isMCXMarketOpen() {
					continue
				}
				if (sym.Segment == feed.SegmentNSEIndex || sym.Segment == feed.SegmentNSEFO) && !isNSEMarketOpen() {
					continue
				}

				lastTick, ok := pub.GetLastTickTime(sym.Symbol)
				if !ok {
					// No tick received yet for this symbol; might still be initialising.
					continue
				}

				if now.Sub(lastTick) > timeout {
					alert := feed.StaleFeedAlert{
						Symbol:    sym.Symbol,
						Segment:   sym.Segment,
						LastTick:  lastTick,
						AlertTime: now,
						Message:   fmt.Sprintf("STALE_FEED: no tick for %s in %s", sym.Symbol, now.Sub(lastTick).Round(time.Second)),
					}

					if err := pub.PublishStaleFeedAlert(alert); err != nil {
						log.Error().Err(err).Str("symbol", sym.Symbol).Msg("failed to publish stale feed alert")
					} else {
						log.Warn().
							Str("symbol", sym.Symbol).
							Time("last_tick", lastTick).
							Dur("gap", now.Sub(lastTick)).
							Msg("STALE_FEED alert published")
					}
				}
			}
		}
	}
}
