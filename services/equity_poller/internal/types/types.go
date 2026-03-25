package types

import (
	"time"
)

// IST timezone, initialised at package load.
var IST *time.Location

func init() {
	var err error
	IST, err = time.LoadLocation("Asia/Kolkata")
	if err != nil {
		// Fallback: IST is UTC+5:30.
		IST = time.FixedZone("IST", 5*3600+30*60)
	}
}

// NowIST returns current time in IST.
func NowIST() time.Time {
	return time.Now().In(IST)
}

// TodayIST returns today's date string in YYYY-MM-DD format (IST).
func TodayIST() string {
	return NowIST().Format("2006-01-02")
}

// BucketFromTime computes the 1-minute bucket number from an IST time.
// bucket 1 = 9:15, bucket 2 = 9:16, ..., bucket 46 = 10:00.
// Returns 0 if outside market hours (before 9:15 or after 15:29).
func BucketFromTime(t time.Time) uint16 {
	ist := t.In(IST)
	h := ist.Hour()
	m := ist.Minute()

	totalMins := uint32(h)*60 + uint32(m)
	openMins := uint32(9)*60 + uint32(15)
	closeMins := uint32(15)*60 + uint32(30)

	if totalMins < openMins {
		return 0
	}
	if totalMins >= closeMins {
		return 0
	}
	return uint16(totalMins - openMins + 1)
}

// Snapshot holds enriched per-stock data for a single 1-minute bucket.
// Mirrors the Rust Snapshot struct from dhan-trader/engine/src/types.rs.
type Snapshot struct {
	Symbol          string  `json:"symbol"`
	SecurityID      string  `json:"security_id"`
	TradingDate     string  `json:"trading_date"`
	Bucket          uint16  `json:"bucket"`
	LTP             float32 `json:"ltp"`
	CandleOpen      float32 `json:"candle_open"`
	CandleHigh      float32 `json:"candle_high"`
	CandleLow       float32 `json:"candle_low"`
	VolumeCum       uint64  `json:"volume_cum"`
	VolumeDelta     uint32  `json:"volume_delta"`
	OITotal         uint64  `json:"oi_total"`
	OIDelta         int64   `json:"oi_delta"`
	Bid             float32 `json:"bid"`
	Ask             float32 `json:"ask"`
	BidQty          uint32  `json:"bid_qty"`
	AskQty          uint32  `json:"ask_qty"`
	VWAP            float32 `json:"vwap"`
	SpreadPct       float32 `json:"spread_pct"`
	PriceVelocity   float32 `json:"price_velocity"`
	VolumeRate      float32 `json:"volume_rate"`
	CandleBodyRatio float32 `json:"candle_body_ratio"`
}

// BatchMessage is the NATS message published each polling cycle.
type BatchMessage struct {
	Bucket      uint16     `json:"bucket"`
	TradingDate string     `json:"trading_date"`
	Timestamp   int64      `json:"timestamp"`
	Stocks      []Snapshot `json:"stocks"`
}

// DailyRef holds per-symbol reference data for the trading day.
type DailyRef struct {
	Symbol     string
	SecurityID string
	PrevClose  float32
	DayOpen    float32
}

// Instrument represents an NSE equity instrument from Dhan scrip master.
type Instrument struct {
	SecurityID  string
	Symbol      string
	CompanyName string
	Exchange    string
	Segment     string
	Series      string
	IsFnO       bool
}

// CycleMetrics tracks timing for a single polling cycle.
type CycleMetrics struct {
	QuoteFetchMs    int64
	DerivedComputeMs int64
	DBPersistMs     int64
	NATSPublishMs   int64
	CycleTotalMs    int64
	StocksProcessed int
	Errors          int
}
