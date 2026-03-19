/// Vectorised indicator computation for native backtest strategies.
/// All functions take slices and return Vec<f64>.
/// Indices: output[i] corresponds to input bar [period + i] or similar.

/// Simple Moving Average over period bars.
pub fn sma(closes: &[f64], period: usize) -> Vec<f64> {
    if closes.len() < period {
        return vec![];
    }
    let n = closes.len();
    let mut result = Vec::with_capacity(n - period + 1);
    let mut sum: f64 = closes[..period].iter().sum();
    result.push(sum / period as f64);
    for i in period..n {
        sum += closes[i] - closes[i - period];
        result.push(sum / period as f64);
    }
    result
}

/// Exponential Moving Average.
pub fn ema(closes: &[f64], period: usize) -> Vec<f64> {
    if closes.len() < period || period < 1 {
        return vec![];
    }
    let k = 2.0 / (period as f64 + 1.0);
    let seed: f64 = closes[..period].iter().sum::<f64>() / period as f64;
    let mut result = vec![seed];
    for price in &closes[period..] {
        let prev = *result.last().unwrap();
        result.push(price * k + prev * (1.0 - k));
    }
    result
}

/// Wilder ATR (Average True Range).
pub fn atr_wilder(highs: &[f64], lows: &[f64], closes: &[f64], period: usize) -> Vec<f64> {
    let n = closes.len().min(highs.len()).min(lows.len());
    if n < period + 1 {
        return vec![];
    }

    let mut tr = Vec::with_capacity(n - 1);
    for i in 1..n {
        let hl = highs[i] - lows[i];
        let hc = (highs[i] - closes[i - 1]).abs();
        let lc = (lows[i] - closes[i - 1]).abs();
        tr.push(hl.max(hc).max(lc));
    }

    if tr.len() < period {
        return vec![];
    }

    let seed: f64 = tr[..period].iter().sum::<f64>() / period as f64;
    let mut result = vec![seed];
    for val in &tr[period..] {
        let prev = *result.last().unwrap();
        result.push((prev * (period as f64 - 1.0) + val) / period as f64);
    }
    result
}

/// Wilder RSI (Relative Strength Index).
pub fn rsi_wilder(closes: &[f64], period: usize) -> Vec<f64> {
    let n = closes.len();
    if n < period + 1 {
        return vec![];
    }

    let deltas: Vec<f64> = (1..n).map(|i| closes[i] - closes[i - 1]).collect();
    let gains: Vec<f64> = deltas.iter().map(|&d| d.max(0.0)).collect();
    let losses: Vec<f64> = deltas.iter().map(|&d| (-d).max(0.0)).collect();

    let mut avg_gain: f64 = gains[..period].iter().sum::<f64>() / period as f64;
    let mut avg_loss: f64 = losses[..period].iter().sum::<f64>() / period as f64;

    let rsi_val = |ag: f64, al: f64| -> f64 {
        if al == 0.0 { 100.0 } else { 100.0 - 100.0 / (1.0 + ag / al) }
    };

    let mut result = vec![rsi_val(avg_gain, avg_loss)];
    for i in period..deltas.len() {
        avg_gain = (avg_gain * (period as f64 - 1.0) + gains[i]) / period as f64;
        avg_loss = (avg_loss * (period as f64 - 1.0) + losses[i]) / period as f64;
        result.push(rsi_val(avg_gain, avg_loss));
    }
    result
}

/// Bollinger Bands. Returns (upper, mid, lower) each of length n-period+1.
pub fn bollinger_bands(closes: &[f64], period: usize, std_mult: f64) -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let n = closes.len();
    if n < period {
        return (vec![], vec![], vec![]);
    }
    let mut upper = Vec::with_capacity(n - period + 1);
    let mut mid = Vec::with_capacity(n - period + 1);
    let mut lower = Vec::with_capacity(n - period + 1);

    for i in (period - 1)..n {
        let window = &closes[(i + 1 - period)..=i];
        let m: f64 = window.iter().sum::<f64>() / period as f64;
        let variance: f64 = window.iter().map(|&x| (x - m).powi(2)).sum::<f64>() / period as f64;
        let std = variance.sqrt();
        upper.push(m + std_mult * std);
        mid.push(m);
        lower.push(m - std_mult * std);
    }
    (upper, mid, lower)
}

/// Supertrend direction. Returns Vec<i8>: 1=bullish, -1=bearish.
pub fn supertrend_direction(
    highs: &[f64],
    lows: &[f64],
    closes: &[f64],
    period: usize,
    multiplier: f64,
) -> Vec<i8> {
    let atr = atr_wilder(highs, lows, closes, period);
    if atr.is_empty() {
        return vec![];
    }

    let offset = period;
    let n = atr.len();
    let mut upper_band = vec![0.0f64; n];
    let mut lower_band = vec![0.0f64; n];
    let mut supertrend = vec![0.0f64; n];
    let mut direction = vec![0i8; n];

    for i in 0..n {
        let ci = offset + i;
        let hl2 = (highs[ci] + lows[ci]) / 2.0;
        upper_band[i] = hl2 + multiplier * atr[i];
        lower_band[i] = hl2 - multiplier * atr[i];

        if i == 0 {
            supertrend[i] = upper_band[i];
            direction[i] = if closes[ci] < supertrend[i] { -1 } else { 1 };
            continue;
        }

        let prev_ci = ci - 1;
        if closes[prev_ci] > lower_band[i - 1] {
            lower_band[i] = lower_band[i].max(lower_band[i - 1]);
        }
        if closes[prev_ci] < upper_band[i - 1] {
            upper_band[i] = upper_band[i].min(upper_band[i - 1]);
        }

        let prev_st = supertrend[i - 1];
        if (prev_st - upper_band[i - 1]).abs() < 1e-9 {
            if closes[ci] <= upper_band[i] {
                supertrend[i] = upper_band[i];
                direction[i] = -1;
            } else {
                supertrend[i] = lower_band[i];
                direction[i] = 1;
            }
        } else {
            if closes[ci] >= lower_band[i] {
                supertrend[i] = lower_band[i];
                direction[i] = 1;
            } else {
                supertrend[i] = upper_band[i];
                direction[i] = -1;
            }
        }
    }
    direction
}
