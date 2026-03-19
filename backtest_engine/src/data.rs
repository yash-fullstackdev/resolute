/// Data loading and candle aggregation.
/// Reads columnar JSON files in parallel using rayon.

use std::fs;
use std::path::{Path, PathBuf};

use rayon::prelude::*;
use serde::Deserialize;

use crate::types::CandleArray;

#[derive(Deserialize)]
struct RawDayFile {
    open: Vec<f64>,
    high: Vec<f64>,
    low: Vec<f64>,
    close: Vec<f64>,
    #[serde(default)]
    volume: Vec<f64>,
    timestamp: Vec<f64>,
}

/// Load 1m candle data for an instrument over a date range.
/// Returns a single concatenated CandleArray sorted by timestamp.
pub fn load_instrument(
    data_dir: &Path,
    instrument: &str,
    start_ts: f64,  // Unix epoch; load files whose date >= start_ts
    end_ts: f64,    // inclusive upper bound
) -> CandleArray {
    let dir = data_dir.join(instrument);
    if !dir.exists() {
        return CandleArray::default();
    }

    // Collect only _1m.json files whose filename date falls within [start_ts, end_ts]
    // File names are YYYY-MM-DD_1m.json — parse date to skip files outside range.
    // Add 1-day buffer on each side to handle timezone edge cases.
    let start_day = (start_ts as i64 - 86400) / 86400 * 86400;
    let end_day = (end_ts as i64 + 86400) / 86400 * 86400;

    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .expect("read data dir")
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            let name = match p.file_name().and_then(|n| n.to_str()) {
                Some(n) => n,
                None => return false,
            };
            if !name.ends_with("_1m.json") {
                return false;
            }
            // Parse YYYY-MM-DD from filename
            let date_str = &name[..10]; // "2024-03-15"
            if let Ok(file_ts) = date_str_to_ts(date_str) {
                file_ts >= start_day && file_ts <= end_day
            } else {
                true // include if can't parse
            }
        })
        .collect();

    files.sort();

    // Parse in parallel using rayon
    let day_arrays: Vec<Option<CandleArray>> = files
        .par_iter()
        .map(|path| load_single_day(path, start_ts, end_ts))
        .collect();

    // Concatenate in order
    let mut out = CandleArray::default();
    for arr_opt in day_arrays {
        if let Some(arr) = arr_opt {
            out.open.extend_from_slice(&arr.open);
            out.high.extend_from_slice(&arr.high);
            out.low.extend_from_slice(&arr.low);
            out.close.extend_from_slice(&arr.close);
            out.volume.extend_from_slice(&arr.volume);
            out.timestamp.extend_from_slice(&arr.timestamp);
        }
    }

    out
}

fn load_single_day(path: &Path, start_ts: f64, end_ts: f64) -> Option<CandleArray> {
    let content = fs::read_to_string(path).ok()?;
    let mut raw: RawDayFile = serde_json::from_str(&content).ok()?;

    if raw.close.is_empty() || raw.timestamp.is_empty() {
        return None;
    }

    // Fill missing volume with zeros (pre-Nov 2025 data has no volume)
    if raw.volume.is_empty() {
        raw.volume = vec![0.0; raw.close.len()];
    }

    // Filter to timestamp range
    let n = raw.close.len().min(raw.timestamp.len());
    let mut open = Vec::with_capacity(n);
    let mut high = Vec::with_capacity(n);
    let mut low = Vec::with_capacity(n);
    let mut close = Vec::with_capacity(n);
    let mut volume = Vec::with_capacity(n);
    let mut timestamp = Vec::with_capacity(n);

    for i in 0..n {
        let ts = raw.timestamp[i];
        if ts < start_ts || ts > end_ts {
            continue;
        }
        // Basic OHLC sanity
        let o = raw.open[i];
        let h = raw.high[i];
        let l = raw.low[i];
        let c = raw.close[i];
        if h < o.max(c) || l > o.min(c) || o <= 0.0 || c <= 0.0 {
            continue;
        }
        open.push(o);
        high.push(h);
        low.push(l);
        close.push(c);
        volume.push(raw.volume.get(i).copied().unwrap_or(0.0).max(0.0));
        timestamp.push(ts);
    }

    if close.is_empty() {
        return None;
    }

    Some(CandleArray { open, high, low, close, volume, timestamp })
}

/// Aggregate 1m CandleArray into N-minute bars.
/// Aligned to IST session start (09:15 = 555 minutes since midnight IST).
/// Session start offset: 09:15 IST = 03:45 UTC = 225 minutes since UTC midnight.
/// We group by: (date, minute_of_day // tf_minutes) where minute_of_day is IST.
pub fn aggregate(candles: &CandleArray, tf_minutes: u32) -> CandleArray {
    if candles.is_empty() || tf_minutes <= 1 {
        return candles.clone();
    }

    let n = candles.len();
    let mut out = CandleArray {
        open: Vec::with_capacity(n / tf_minutes as usize + 1),
        high: Vec::with_capacity(n / tf_minutes as usize + 1),
        low: Vec::with_capacity(n / tf_minutes as usize + 1),
        close: Vec::with_capacity(n / tf_minutes as usize + 1),
        volume: Vec::with_capacity(n / tf_minutes as usize + 1),
        timestamp: Vec::with_capacity(n / tf_minutes as usize + 1),
    };

    // IST = UTC + 5:30 = UTC + 330 minutes
    const IST_OFFSET_SECS: i64 = 330 * 60;
    const SESSION_START_MIN: i64 = 9 * 60 + 15; // 09:15 IST in minutes

    let mut bar_open = 0.0_f64;
    let mut bar_high = f64::NEG_INFINITY;
    let mut bar_low = f64::INFINITY;
    let mut bar_close = 0.0_f64;
    let mut bar_volume = 0.0_f64;
    let mut bar_ts = 0.0_f64;
    let mut current_group: i64 = -1;

    for i in 0..n {
        let ts = candles.timestamp[i] as i64;
        let ist_ts = ts + IST_OFFSET_SECS;
        let day = ist_ts / 86400;
        let min_of_day = (ist_ts % 86400) / 60;
        let session_min = min_of_day - SESSION_START_MIN;
        let group = day * 10000 + (session_min / tf_minutes as i64);

        if group != current_group {
            // Flush previous bar
            if current_group >= 0 && bar_high > f64::NEG_INFINITY {
                out.open.push(bar_open);
                out.high.push(bar_high);
                out.low.push(bar_low);
                out.close.push(bar_close);
                out.volume.push(bar_volume);
                out.timestamp.push(bar_ts);
            }
            // Start new bar
            current_group = group;
            bar_open = candles.open[i];
            bar_high = candles.high[i];
            bar_low = candles.low[i];
            bar_close = candles.close[i];
            bar_volume = candles.volume[i];
            bar_ts = candles.timestamp[i];
        } else {
            bar_high = bar_high.max(candles.high[i]);
            bar_low = bar_low.min(candles.low[i]);
            bar_close = candles.close[i];
            bar_volume += candles.volume[i];
        }
    }

    // Flush last bar
    if current_group >= 0 && bar_high > f64::NEG_INFINITY {
        out.open.push(bar_open);
        out.high.push(bar_high);
        out.low.push(bar_low);
        out.close.push(bar_close);
        out.volume.push(bar_volume);
        out.timestamp.push(bar_ts);
    }

    out
}

/// Build a mapping: for each 1m bar index, return the index of the most recently
/// closed higher-TF bar (-1 if none closed yet).
/// Returns (tf_close_at_1m: Vec<i64>) — the 1m bar index where each TF bar closes.
pub fn build_tf_close_set(candles_1m: &CandleArray, tf_minutes: u32) -> Vec<bool> {
    // Returns a boolean Vec of length = candles_1m.len()
    // true at index i means: a tf_minutes bar CLOSES at this 1m bar
    if candles_1m.is_empty() {
        return vec![];
    }

    const IST_OFFSET_SECS: i64 = 330 * 60;
    const SESSION_START_MIN: i64 = 9 * 60 + 15;

    let n = candles_1m.len();
    let mut result = vec![false; n];
    let mut current_group: i64 = -1;

    for i in 0..n {
        let ts = candles_1m.timestamp[i] as i64;
        let ist_ts = ts + IST_OFFSET_SECS;
        let day = ist_ts / 86400;
        let min_of_day = (ist_ts % 86400) / 60;
        let session_min = min_of_day - SESSION_START_MIN;
        let group = day * 10000 + (session_min / tf_minutes as i64);

        if group != current_group && current_group >= 0 {
            // The PREVIOUS bar (i-1) was the last in that group
            if i > 0 {
                result[i - 1] = true;
            }
        }
        current_group = group;
    }
    // Last bar of entire dataset closes its group
    if n > 0 {
        result[n - 1] = true;
    }

    result
}

/// Get time-of-day in minutes since midnight IST for a Unix timestamp.
pub fn ts_to_ist_minutes(ts: f64) -> u32 {
    const IST_OFFSET_SECS: i64 = 330 * 60;
    let ist = ts as i64 + IST_OFFSET_SECS;
    let hour = (ist % 86400) / 3600;
    let minute = (ist % 3600) / 60;
    (hour * 60 + minute) as u32
}

/// Build a per-1m-bar map to corresponding higher-TF bar index.
/// Returns Vec<usize> of length = candles_1m.len().
pub fn build_1m_to_tf_index(candles_1m: &CandleArray, tf_minutes: u32) -> Vec<usize> {
    if candles_1m.is_empty() {
        return vec![];
    }

    const IST_OFFSET_SECS: i64 = 330 * 60;
    const SESSION_START_MIN: i64 = 9 * 60 + 15;

    let n = candles_1m.len();
    let mut result = vec![0usize; n];
    let mut current_group: i64 = -1;
    let mut tf_idx: usize = 0;
    let mut first = true;

    for i in 0..n {
        let ts = candles_1m.timestamp[i] as i64;
        let ist_ts = ts + IST_OFFSET_SECS;
        let day = ist_ts / 86400;
        let min_of_day = (ist_ts % 86400) / 60;
        let session_min = min_of_day - SESSION_START_MIN;
        let group = day * 10000 + (session_min / tf_minutes as i64);

        if group != current_group {
            if !first {
                tf_idx += 1;
            }
            current_group = group;
            first = false;
        }
        result[i] = tf_idx;
    }

    result
}

/// Parse "YYYY-MM-DD" → Unix timestamp (seconds) for midnight UTC.
fn date_str_to_ts(s: &str) -> Result<i64, ()> {
    let parts: Vec<&str> = s.split('-').collect();
    if parts.len() != 3 { return Err(()); }
    let y: i64 = parts[0].parse().map_err(|_| ())?;
    let m: i64 = parts[1].parse().map_err(|_| ())?;
    let d: i64 = parts[2].parse().map_err(|_| ())?;
    let days = days_from_civil(y, m, d);
    Ok(days * 86400)
}

/// Days since Unix epoch for a Gregorian date.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}
