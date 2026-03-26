#!/usr/bin/env python3
"""
simulate_equity_market.py

Downloads historical 1-min candle data from Dhan API and replays it through NATS
as equity.snapshots.batch messages, exactly matching the equity_poller's BatchMessage
format. This triggers the real user_worker_pool to evaluate early_momentum signals.

Usage — single day:
  python scripts/simulate_equity_market.py --date 2026-03-24 --symbols RELIANCE,INFY,TCS

Usage — date range:
  python scripts/simulate_equity_market.py --from 2026-03-20 --to 2026-03-24 --symbols RELIANCE,INFY

Environment vars (reads from .env automatically):
  DHAN_ACCESS_TOKEN   — Dhan API access token
  DHAN_CLIENT_ID      — Dhan client ID
  NATS_URL            — default nats://localhost:4222
  DATABASE_URL        — default postgresql://options:admin@localhost:5432/options_db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import httpx
import nats
import psycopg2

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECURITY_IDS: dict[str, str] = {}  # populated at startup from Dhan scrip master

_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

def _load_security_ids() -> dict[str, str]:
    """Download Dhan scrip master CSV and build symbol→security_id lookup for NSE EQ."""
    import csv, io
    global SECURITY_IDS
    if SECURITY_IDS:
        return SECURITY_IDS
    print("  Downloading Dhan scrip master CSV...")
    import urllib.request
    with urllib.request.urlopen(_SCRIP_MASTER_URL) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for row in reader:
        # NSE equity segment: SEM_EXM_EXCH_ID == "NSE" and SEM_INSTRUMENT_NAME == "EQUITY"
        if row.get("SEM_EXM_EXCH_ID") == "NSE" and row.get("SEM_INSTRUMENT_NAME") == "EQUITY":
            symbol = row.get("SEM_TRADING_SYMBOL", "").strip()
            sec_id = row.get("SEM_SMST_SECURITY_ID", "").strip()
            if symbol and sec_id:
                SECURITY_IDS[symbol] = sec_id
                count += 1
    print(f"  Loaded {count} NSE equity symbols")
    return SECURITY_IDS

NATS_SUBJECT = "equity.snapshots.batch"
IST_OFFSET = timedelta(hours=5, minutes=30)
MARKET_OPEN_MINUTE = 9 * 60 + 15   # 9:15 IST
MAX_BUCKET = 76                     # up to 10:30 IST (same as backfill)
DHAN_BASE_URL = "https://api.dhan.co/v2"
INTER_BUCKET_SLEEP = 2.0            # seconds between bucket publishes


# ---------------------------------------------------------------------------
# .env loader (minimal, no dependency)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no overwrite)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def weekdays_between(from_date: date, to_date: date) -> list[date]:
    """Return all weekday (Mon-Fri) dates in [from_date, to_date]."""
    days: list[date] = []
    cur = from_date
    while cur <= to_date:
        if cur.weekday() < 5:  # Mon=0 .. Fri=4
            days.append(cur)
        cur += timedelta(days=1)
    return days


def chunk_list(lst: list, n: int) -> list[list]:
    """Split list into chunks of at most n elements."""
    return [lst[i : i + n] for i in range(0, len(lst), n)]


# ---------------------------------------------------------------------------
# Dhan API — fetch 1-min candles
# ---------------------------------------------------------------------------

async def fetch_candles(
    client: httpx.AsyncClient,
    security_id: str,
    from_date: str,
    to_date: str,
    access_token: str,
    *,
    retries: int = 4,
) -> list[dict]:
    """
    Fetch 1-min intraday candles from Dhan.

    Returns list of dicts: {ts, open, high, low, close, volume}
    ts is Unix epoch seconds.
    """
    headers = {
        "Content-Type": "application/json",
        "access-token": access_token,
    }
    body = {
        "securityId": str(security_id),
        "exchangeSegment": "NSE_EQ",
        "instrument": "EQUITY",
        "interval": "1",
        "fromDate": from_date,
        "toDate": to_date,
    }

    for attempt in range(1, retries + 1):
        try:
            resp = await client.post(
                f"{DHAN_BASE_URL}/charts/intraday",
                headers=headers,
                json=body,
                timeout=15.0,
            )
        except httpx.RequestError as exc:
            print(f"    [attempt {attempt}] request error: {exc}")
            await asyncio.sleep(attempt * 2)
            continue

        if resp.status_code == 429:
            wait = attempt * 3
            print(f"    [rate-limit] waiting {wait}s...", end="", flush=True)
            await asyncio.sleep(wait)
            print(" retrying")
            continue

        if resp.status_code != 200:
            print(f"    [attempt {attempt}] Dhan API {resp.status_code}: {resp.text[:200]}")
            await asyncio.sleep(attempt * 2)
            continue

        data = resp.json()
        if not data.get("open") or not data.get("timestamp"):
            return []

        return [
            {
                "ts": data["timestamp"][i],
                "open": data["open"][i],
                "high": data["high"][i],
                "low": data["low"][i],
                "close": data["close"][i],
                "volume": data["volume"][i] if data.get("volume") else 0,
            }
            for i in range(len(data["timestamp"]))
        ]

    raise RuntimeError(f"Dhan API failed after {retries} retries for {security_id}")


# ---------------------------------------------------------------------------
# Time / bucket helpers
# ---------------------------------------------------------------------------

def _to_ist(unix_sec: int) -> datetime:
    """Convert Unix timestamp to IST datetime (using UTC offset trick)."""
    return datetime.fromtimestamp(unix_sec, tz=timezone.utc) + IST_OFFSET


def get_bucket(unix_sec: int) -> int | None:
    """
    Compute 1-minute bucket from Unix timestamp.
    bucket 1 = 9:15 IST, bucket 46 = 10:00, bucket 76 = 10:30.
    Returns None if outside 9:15-15:29 IST.
    """
    dt = _to_ist(unix_sec)
    minute_of_day = dt.hour * 60 + dt.minute
    offset = minute_of_day - MARKET_OPEN_MINUTE
    if offset < 0 or offset > 374:
        return None
    return offset + 1


def trading_date_from_ts(unix_sec: int) -> str:
    """Return YYYY-MM-DD trading date string from Unix timestamp (IST)."""
    dt = _to_ist(unix_sec)
    return dt.strftime("%Y-%m-%d")


def fmt_ist(unix_sec: int) -> str:
    """Format Unix timestamp as IST datetime string."""
    dt = _to_ist(unix_sec)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Build enriched snapshots — port of backfill-history.mjs buildSnapshotRows
# ---------------------------------------------------------------------------

def build_snapshot_rows(
    all_candles: list[dict],
    security_id: str,
    symbol: str,
) -> dict[str, list[dict]]:
    """
    Group candles by trading date, compute derived fields, return
    {date_str: [snapshot_dict, ...]} with snapshots up to MAX_BUCKET.

    Each snapshot dict matches the Go Snapshot JSON struct plus extra
    fields (gap_pct, move_pct, open_price, prev_close) for strategy use.
    """
    # Group by date — keep ALL market candles for correct running VWAP
    by_date: dict[str, list[dict]] = {}
    for c in all_candles:
        bucket = get_bucket(c["ts"])
        if bucket is None:
            continue
        d = trading_date_from_ts(c["ts"])
        by_date.setdefault(d, []).append({**c, "bucket": bucket})

    result: dict[str, list[dict]] = {}

    for dt_str, candles in sorted(by_date.items()):
        candles.sort(key=lambda c: c["ts"])
        rows: list[dict] = []

        prev_ltp = 0.0
        prev_volume = 0
        vwap_num = 0.0
        vwap_den = 0.0
        first = True

        for c in candles:
            ltp = c["close"]
            vol_delta = c["volume"] if first else max(0, c["volume"] - prev_volume)
            velocity = 0.0 if first else (ltp - prev_ltp) / 60.0
            vol_rate = vol_delta / 60.0
            rng = c["high"] - c["low"]
            body_ratio = abs(ltp - c["open"]) / rng if rng > 0 else 0.0
            weight = c["volume"] if first else vol_delta
            vwap_num += ltp * weight
            vwap_den += weight
            vwap = vwap_num / vwap_den if vwap_den > 0 else ltp

            prev_ltp = ltp
            prev_volume = c["volume"]
            first = False

            # Only store up to MAX_BUCKET
            if c["bucket"] > MAX_BUCKET:
                continue

            rows.append({
                "symbol": symbol,
                "security_id": str(security_id),
                "trading_date": dt_str,
                "bucket": c["bucket"],
                "ltp": round(ltp, 2),
                "candle_open": round(c["open"], 2),
                "candle_high": round(c["high"], 2),
                "candle_low": round(c["low"], 2),
                "volume_cum": c["volume"],
                "volume_delta": vol_delta,
                "oi_total": 0,
                "oi_delta": 0,
                "bid": 0.0,
                "ask": 0.0,
                "bid_qty": 0,
                "ask_qty": 0,
                "vwap": round(vwap, 2),
                "spread_pct": 0.0,
                "price_velocity": round(velocity, 6),
                "volume_rate": round(vol_rate, 2),
                "candle_body_ratio": round(body_ratio, 4),
            })

        result[dt_str] = rows

    return result


def compute_daily_ref(
    snapshots_by_date: dict[str, list[dict]],
) -> dict[str, dict]:
    """
    Compute per-date daily reference: prev_close, open_price, gap_pct.

    Returns {date_str: {"prev_close": ..., "open_price": ..., "gap_pct": ...}}.
    prev_close for day N = last close of day N-1 in the loaded data.
    For the first day in range, prev_close = 0 (gap_pct will be 0).
    """
    sorted_dates = sorted(snapshots_by_date.keys())
    if not sorted_dates:
        return {}

    # Build EOD close per date (last snapshot's ltp)
    eod_close: dict[str, float] = {}
    day_open: dict[str, float] = {}
    for d in sorted_dates:
        rows = snapshots_by_date[d]
        if rows:
            eod_close[d] = rows[-1]["ltp"]
            day_open[d] = rows[0]["candle_open"]
        else:
            eod_close[d] = 0.0
            day_open[d] = 0.0

    refs: dict[str, dict] = {}
    for i, d in enumerate(sorted_dates):
        pc = eod_close[sorted_dates[i - 1]] if i > 0 else 0.0
        op = day_open[d]
        gap = (op - pc) / pc * 100.0 if pc > 0 else 0.0
        refs[d] = {
            "prev_close": round(pc, 2),
            "open_price": round(op, 2),
            "gap_pct": round(gap, 4),
        }

    return refs


def enrich_snapshots_with_daily_ref(
    snapshots_by_date: dict[str, list[dict]],
    daily_refs: dict[str, dict],
) -> None:
    """
    Inject gap_pct, move_pct, open_price, prev_close into each snapshot dict.
    Modifies in place.
    """
    for dt_str, rows in snapshots_by_date.items():
        ref = daily_refs.get(dt_str, {})
        open_price = ref.get("open_price", 0.0)
        prev_close = ref.get("prev_close", 0.0)
        gap_pct = ref.get("gap_pct", 0.0)

        for snap in rows:
            snap["gap_pct"] = gap_pct
            snap["prev_close"] = prev_close
            snap["open_price"] = open_price
            ltp = snap["ltp"]
            snap["move_pct"] = round(
                (ltp - open_price) / open_price * 100.0 if open_price > 0 else 0.0,
                4,
            )


# ---------------------------------------------------------------------------
# NATS replay
# ---------------------------------------------------------------------------

async def replay_via_nats(
    all_snapshots: dict[str, dict[str, list[dict]]],
    nats_url: str,
) -> None:
    """
    Replay snapshots bucket-by-bucket through NATS.

    all_snapshots: {symbol: {date: [snapshot_dicts]}}
    """
    nc = await nats.connect(nats_url)
    print(f"\nConnected to NATS at {nats_url}")

    # Collect all trading dates across symbols
    all_dates: set[str] = set()
    for sym_data in all_snapshots.values():
        all_dates.update(sym_data.keys())

    for trading_dt in sorted(all_dates):
        print(f"\n{'='*60}")
        print(f"  Replaying {trading_dt}")
        print(f"{'='*60}")

        # Gather all snapshots for this date, grouped by bucket
        bucket_map: dict[int, list[dict]] = {}
        for sym, sym_data in all_snapshots.items():
            for snap in sym_data.get(trading_dt, []):
                b = snap["bucket"]
                bucket_map.setdefault(b, []).append(snap)

        if not bucket_map:
            print(f"  No data for {trading_dt}, skipping")
            continue

        min_bucket = min(bucket_map.keys())
        max_bucket = max(bucket_map.keys())

        t_day_start = time.monotonic()
        buckets_published = 0

        for bucket in range(min_bucket, max_bucket + 1):
            stocks = bucket_map.get(bucket, [])
            if not stocks:
                continue

            # Build BatchMessage matching Go struct exactly
            batch_msg = {
                "bucket": bucket,
                "trading_date": trading_dt,
                "timestamp": time.time(),
                "stocks": stocks,
            }

            payload = json.dumps(batch_msg).encode()
            await nc.publish(NATS_SUBJECT, payload)
            buckets_published += 1

            print(
                f"  [bucket {bucket:>3}] Published {len(stocks)} stocks, "
                f"waiting {INTER_BUCKET_SLEEP}s..."
            )

            await asyncio.sleep(INTER_BUCKET_SLEEP)

        day_elapsed = time.monotonic() - t_day_start
        print(
            f"\n  Day complete: {buckets_published} buckets published "
            f"in {day_elapsed:.1f}s"
        )

    await nc.drain()
    print("\nNATS connection drained.")


# ---------------------------------------------------------------------------
# Query signals from TimescaleDB
# ---------------------------------------------------------------------------

def query_signals(database_url: str, dates: list[str]) -> None:
    """Query early_momentum signals from TimescaleDB and print summary."""
    print(f"\n{'='*60}")
    print("  Signal Query Results")
    print(f"{'='*60}")

    try:
        conn = psycopg2.connect(database_url)
    except Exception as exc:
        print(f"  Could not connect to database: {exc}")
        print("  Skipping signal query (DB may not be running).")
        return

    try:
        cur = conn.cursor()
        for dt_str in sorted(dates):
            cur.execute(
                """
                SELECT underlying, direction, strength, metadata
                FROM signals
                WHERE strategy = 'early_momentum'
                  AND DATE(time) = %s
                ORDER BY time
                """,
                (dt_str,),
            )
            rows = cur.fetchall()

            if not rows:
                print(f"\n  {dt_str}: No signals fired")
                continue

            buy_count = sum(1 for r in rows if r[1] == "BUY")
            sell_count = sum(1 for r in rows if r[1] == "SELL")

            print(f"\n  {dt_str}: {len(rows)} signal(s) — {buy_count} BUY, {sell_count} SELL")
            for underlying, direction, strength, metadata in rows:
                meta = metadata if isinstance(metadata, dict) else {}
                score = meta.get("score", "?")
                entry = meta.get("entry_price", "?")
                fired = meta.get("signals_fired", [])
                print(
                    f"    {underlying:<15} {direction:<5} "
                    f"score={score}  entry={entry}  "
                    f"indicators={','.join(fired) if fired else 'n/a'}"
                )
    except Exception as exc:
        print(f"  Query error: {exc}")
        print("  (The 'signals' table may not exist yet.)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    global INTER_BUCKET_SLEEP  # noqa: PLW0603
    parser = argparse.ArgumentParser(
        description="Simulate equity market by replaying Dhan historical candles via NATS"
    )
    parser.add_argument("--date", type=str, help="Single trading date (YYYY-MM-DD)")
    parser.add_argument("--from", dest="from_date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--symbols", type=str, required=True,
        help="Comma-separated stock symbols (e.g. RELIANCE,INFY,TCS)",
    )
    parser.add_argument("--sleep", type=float, default=INTER_BUCKET_SLEEP,
                        help="Seconds between bucket publishes (default 2)")
    parser.add_argument("--no-query", action="store_true",
                        help="Skip signal query from TimescaleDB after replay")

    args = parser.parse_args()

    # Load .env files
    project_root = Path(__file__).resolve().parent.parent
    _load_dotenv(str(project_root / ".env"))
    # Also try dhan-trader .env for DHAN_CLIENT_ID
    dhan_trader_env = project_root.parent / "dhan-trader" / ".env"
    _load_dotenv(str(dhan_trader_env))

    # Resolve dates
    if args.date:
        from_date = date.fromisoformat(args.date)
        to_date = from_date
    elif args.from_date:
        from_date = date.fromisoformat(args.from_date)
        to_date = date.fromisoformat(args.to_date) if args.to_date else from_date
    else:
        print("Error: provide --date or --from/--to")
        sys.exit(1)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        print("Error: no symbols provided")
        sys.exit(1)

    # Load security IDs from Dhan scrip master
    _load_security_ids()

    # Validate security IDs
    missing = [s for s in symbols if s not in SECURITY_IDS]
    if missing:
        print(f"Warning: unknown symbol(s) skipped: {', '.join(missing[:10])}")
        symbols = [s for s in symbols if s in SECURITY_IDS]
        if not symbols:
            print("Error: no valid symbols remaining")
            sys.exit(1)

    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not access_token:
        print("Error: DHAN_ACCESS_TOKEN not set (check .env)")
        sys.exit(1)

    nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
    database_url = os.environ.get("DATABASE_URL", "postgresql://options:admin@localhost:5432/options_db")

    INTER_BUCKET_SLEEP = args.sleep

    weekdays = weekdays_between(from_date, to_date)
    date_chunks = chunk_list(weekdays, 5)  # Dhan API: max 5 trading days per request

    print()
    print("=" * 60)
    print("  Resolute — Equity Market Simulator")
    print("=" * 60)
    print(f"  Range:    {from_date} -> {to_date}  ({len(weekdays)} weekdays, {len(date_chunks)} API chunks)")
    print(f"  Symbols:  {', '.join(symbols)}")
    print(f"  NATS:     {nats_url}")
    print(f"  DB:       {database_url.split('@')[1] if '@' in database_url else database_url}")
    print(f"  Sleep:    {INTER_BUCKET_SLEEP}s between buckets")
    print()

    # ── Phase 1: Download candles ──────────────────────────────────────

    print("Phase 1: Downloading historical candles from Dhan API...")
    print()

    # {symbol: {date_str: [snapshot_dict, ...]}}
    all_snapshots: dict[str, dict[str, list[dict]]] = {}
    total_candles = 0
    total_snapshots = 0

    async with httpx.AsyncClient() as client:
        for sym_idx, symbol in enumerate(symbols, 1):
            security_id = SECURITY_IDS[symbol]
            print(f"  [{sym_idx}/{len(symbols)}] {symbol} (secId={security_id})", end=" ", flush=True)

            sym_candles: list[dict] = []
            chunks_failed = 0

            for chunk in date_chunks:
                chunk_from = chunk[0].isoformat()
                chunk_to = chunk[-1].isoformat()

                # Throttle API calls
                await asyncio.sleep(0.6)

                try:
                    candles = await fetch_candles(
                        client, security_id, chunk_from, chunk_to, access_token,
                    )
                    sym_candles.extend(candles)
                except Exception as exc:
                    chunks_failed += 1
                    print(f"\n    chunk {chunk_from}..{chunk_to} failed: {exc}")

            if not sym_candles:
                print("-- no data")
                continue

            total_candles += len(sym_candles)

            # Build snapshots
            snap_by_date = build_snapshot_rows(sym_candles, security_id, symbol)
            daily_refs = compute_daily_ref(snap_by_date)
            enrich_snapshots_with_daily_ref(snap_by_date, daily_refs)

            snap_count = sum(len(v) for v in snap_by_date.values())
            total_snapshots += snap_count
            dates_loaded = len(snap_by_date)

            fail_note = f"  ({chunks_failed} chunk(s) failed)" if chunks_failed else ""
            print(f"-> {dates_loaded} days, {snap_count} snapshots{fail_note}")

            all_snapshots[symbol] = snap_by_date

    print(f"\n  Total: {total_candles} candles -> {total_snapshots} snapshots")

    if total_snapshots == 0:
        print("\nNo data to replay. Exiting.")
        sys.exit(0)

    # ── Phase 2: Replay via NATS ───────────────────────────────────────

    print(f"\nPhase 2: Replaying {total_snapshots} snapshots via NATS...")

    t_replay_start = time.monotonic()
    await replay_via_nats(all_snapshots, nats_url)
    replay_elapsed = time.monotonic() - t_replay_start

    print(f"\nReplay complete in {replay_elapsed:.1f}s")

    # ── Phase 3: Query signals ─────────────────────────────────────────

    if not args.no_query:
        # Small delay to let async DB writes complete
        print("\nWaiting 3s for async signal persistence...")
        await asyncio.sleep(3.0)

        date_strs = sorted({
            d for sym_data in all_snapshots.values() for d in sym_data.keys()
        })
        query_signals(database_url, date_strs)

    print(f"\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
