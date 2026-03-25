package derived

// RunningState holds per-symbol running state for VWAP computation.
// Mirrors dhan-trader/engine/src/derived.rs RunningState.
type RunningState struct {
	VWAPNum      float64 // sum(ltp * volume_delta)
	VWAPDen      float64 // sum(volume_delta)
	PrevLTP      float32
	PrevVolCum   uint64
	PrevOITotal  uint64
}

// Fields holds the derived fields computed for a single snapshot.
type Fields struct {
	VWAP            float32
	PriceVelocity   float32
	VolumeRate      float32
	CandleBodyRatio float32
	SpreadPct       float32
	VolumeDelta     uint32
	OIDelta         int64
}

// Compute calculates derived fields from raw quote data and updates running state.
// Formulas match dhan-trader/engine/src/derived.rs exactly.
func Compute(
	ltp float32,
	candleOpen float32,
	candleHigh float32,
	candleLow float32,
	volumeCum uint64,
	oiTotal uint64,
	bid float32,
	ask float32,
	state *RunningState,
) Fields {
	// Volume delta: cumulative volume minus previous cumulative volume.
	var volumeDelta uint32
	if volumeCum > state.PrevVolCum {
		volumeDelta = uint32(volumeCum - state.PrevVolCum)
	}

	// OI delta: signed difference.
	oiDelta := int64(oiTotal) - int64(state.PrevOITotal)

	// VWAP: running sum(ltp * vol) / sum(vol).
	// First call (prev_volume_cum == 0): use full volume_cum.
	volForVWAP := uint64(volumeDelta)
	if state.PrevVolCum == 0 {
		volForVWAP = volumeCum
	}
	state.VWAPNum += float64(ltp) * float64(volForVWAP)
	state.VWAPDen += float64(volForVWAP)

	var vwap float32
	if state.VWAPDen > 0 {
		vwap = float32(state.VWAPNum / state.VWAPDen)
	} else {
		vwap = ltp
	}

	// Price velocity: change in LTP per second (60s bucket).
	priceVelocity := (ltp - state.PrevLTP) / 60.0

	// Volume rate: shares per second in this bucket.
	volumeRate := float32(volumeDelta) / 60.0

	// Candle body ratio: |ltp - open| / (high - low).
	var candleBodyRatio float32
	rangeHL := candleHigh - candleLow
	if rangeHL > 0 {
		body := ltp - candleOpen
		if body < 0 {
			body = -body
		}
		candleBodyRatio = body / rangeHL
	}

	// Spread percentage: (ask - bid) / ltp * 100.
	var spreadPct float32
	if ltp > 0 {
		spreadPct = (ask - bid) / ltp * 100.0
	}

	// Update state for next bucket.
	state.PrevLTP = ltp
	state.PrevVolCum = volumeCum
	state.PrevOITotal = oiTotal

	return Fields{
		VWAP:            vwap,
		PriceVelocity:   priceVelocity,
		VolumeRate:      volumeRate,
		CandleBodyRatio: candleBodyRatio,
		SpreadPct:       spreadPct,
		VolumeDelta:     volumeDelta,
		OIDelta:         oiDelta,
	}
}
