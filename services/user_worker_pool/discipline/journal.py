"""
TradeJournal -- automatically writes structured journal entries for every
closed position and computes per-trade discipline scores.

Discipline score (0-100) per trade:
  +25  Stop-loss was respected (not moved)
  +25  Time-stop was respected (not extended)
  +25  Trade was part of the locked plan (not added ad-hoc)
  +25  No override requests made during the trade's lifetime

  Deductions:
    -10  Override requested (even if not confirmed)
    -20  Override confirmed (stop moved, time extended)
    -15  Trade exited manually before stop or target

Aggregate discipline score = rolling 30-trade weighted average.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import structlog

from ..strategies.base import Position

logger = structlog.get_logger(service="user_worker_pool", module="journal")


@dataclass
class JournalEntry:
    """A single trade journal entry."""
    id: str
    user_id: str
    position_id: str
    trade_date: date
    strategy: str
    underlying: str
    entry_time: datetime
    exit_time: datetime
    exit_reason: str           # "STOP_HIT" | "TARGET_HIT" | "TIME_STOP" | "MANUAL_EXIT"
    entry_cost_inr: float
    exit_value_inr: float
    realised_pnl_inr: float
    pnl_pct: float
    was_in_plan: bool
    stop_loss_respected: bool
    time_stop_respected: bool
    override_count: int
    discipline_score: float    # 0-100
    pre_market_thesis: str
    post_trade_notes: str = ""


@dataclass
class WeeklyDisciplineReport:
    """Weekly summary of trading discipline."""
    user_id: str
    week_start: date
    week_end: date
    total_trades: int
    disciplined_trades: int      # score >= 75
    undisciplined_trades: int    # score < 50
    avg_discipline_score: float
    pnl_disciplined_trades_inr: float
    pnl_undisciplined_trades_inr: float
    total_pnl_inr: float
    signals_skipped: int
    override_count: int
    net_override_impact_inr: float
    circuit_breaker_triggers: int
    top_insight: str


class TradeJournal:
    """Writes journal entries and computes discipline scores."""

    def __init__(self, db=None, nats=None) -> None:
        self._db = db
        self._nats = nats
        self._entries: dict[str, list[JournalEntry]] = {}  # user_id -> entries

    def write_entry(
        self,
        position: Position,
        locked_plan,       # LockedPlan | None
        override_requests: list,  # list of OverrideRequest
    ) -> JournalEntry:
        """Write a journal entry for a closed position.

        Computes discipline_score based on rule adherence:
          +25 stop_loss respected
          +25 time_stop respected
          +25 trade in plan
          +25 no overrides
          -10 per override requested (even if not confirmed)
          -20 per override confirmed
          -15 if manual exit
        """
        now = datetime.now(timezone.utc)

        # -- Determine discipline components
        # 1. Was trade in plan?
        was_in_plan = False
        pre_market_thesis = ""
        if locked_plan is not None:
            was_in_plan = (
                position.strategy_name in locked_plan.enabled_strategies
                and position.underlying in locked_plan.active_underlyings
            )
            pre_market_thesis = locked_plan.notes or ""

        # 2. Stop loss respected?
        stop_loss_respected = not position.stop_loss_moved

        # 3. Time stop respected?
        time_stop_respected = not position.time_stop_extended

        # 4. Override count
        override_count = len(override_requests) if override_requests else 0
        confirmed_overrides = sum(
            1 for r in (override_requests or [])
            if r.status == "CONFIRMED"
        )

        # -- Compute discipline score
        score = 0.0

        # +25 for each rule respected
        if stop_loss_respected:
            score += 25.0
        if time_stop_respected:
            score += 25.0
        if was_in_plan:
            score += 25.0
        if override_count == 0:
            score += 25.0

        # Deductions
        # -10 per override requested (even if not confirmed)
        score -= 10.0 * override_count

        # -20 per override confirmed (additional to the -10 above)
        score -= 20.0 * confirmed_overrides

        # -15 if manual exit
        exit_reason = position.exit_reason or "MANUAL_EXIT"
        if exit_reason == "MANUAL_EXIT":
            score -= 15.0

        # Clamp to 0-100
        score = max(0.0, min(100.0, score))

        # -- Compute P&L
        realised_pnl = position.exit_value_inr - position.entry_cost_inr
        pnl_pct = (
            (realised_pnl / position.entry_cost_inr * 100)
            if position.entry_cost_inr > 0
            else 0.0
        )

        entry = JournalEntry(
            id=str(uuid.uuid4()),
            user_id=position.tenant_id,
            position_id=position.position_id,
            trade_date=position.entry_time.date() if position.entry_time else date.today(),
            strategy=position.strategy_name,
            underlying=position.underlying,
            entry_time=position.entry_time,
            exit_time=position.exit_time or now,
            exit_reason=exit_reason,
            entry_cost_inr=position.entry_cost_inr,
            exit_value_inr=position.exit_value_inr,
            realised_pnl_inr=realised_pnl,
            pnl_pct=round(pnl_pct, 2),
            was_in_plan=was_in_plan,
            stop_loss_respected=stop_loss_respected,
            time_stop_respected=time_stop_respected,
            override_count=override_count,
            discipline_score=round(score, 1),
            pre_market_thesis=pre_market_thesis,
        )

        # Store in memory
        if position.tenant_id not in self._entries:
            self._entries[position.tenant_id] = []
        self._entries[position.tenant_id].append(entry)

        logger.info(
            "journal_entry_written",
            tenant_id=position.tenant_id,
            position_id=position.position_id,
            strategy=position.strategy_name,
            discipline_score=entry.discipline_score,
            pnl_inr=round(realised_pnl, 2),
            exit_reason=exit_reason,
            was_in_plan=was_in_plan,
            stop_loss_respected=stop_loss_respected,
            time_stop_respected=time_stop_respected,
            override_count=override_count,
        )

        return entry

    async def persist_entry(self, entry: JournalEntry) -> None:
        """Persist journal entry to the database."""
        if self._db is None:
            return
        try:
            await self._db.execute(
                """
                INSERT INTO trade_journal
                    (id, user_id, position_id, trade_date, strategy, underlying,
                     entry_time, exit_time, exit_reason, entry_cost_inr,
                     exit_value_inr, realised_pnl_inr, pnl_pct, was_in_plan,
                     stop_loss_respected, time_stop_respected, override_count,
                     discipline_score, pre_market_thesis, post_trade_notes)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
                """,
                entry.id, entry.user_id, entry.position_id, entry.trade_date,
                entry.strategy, entry.underlying, entry.entry_time, entry.exit_time,
                entry.exit_reason, entry.entry_cost_inr, entry.exit_value_inr,
                entry.realised_pnl_inr, entry.pnl_pct, entry.was_in_plan,
                entry.stop_loss_respected, entry.time_stop_respected,
                entry.override_count, entry.discipline_score,
                entry.pre_market_thesis, entry.post_trade_notes,
                tenant_id=entry.user_id,
            )
        except Exception as exc:
            logger.error(
                "journal_persist_failed",
                entry_id=entry.id,
                error=str(exc),
            )

    async def publish_entry(self, entry: JournalEntry) -> None:
        """Publish journal entry event to NATS."""
        if self._nats is None:
            return
        await self._nats.publish(
            f"discipline.journal.entry.{entry.user_id}.{entry.position_id}",
            {
                "entry_id": entry.id,
                "user_id": entry.user_id,
                "position_id": entry.position_id,
                "strategy": entry.strategy,
                "discipline_score": entry.discipline_score,
                "pnl_inr": entry.realised_pnl_inr,
                "exit_reason": entry.exit_reason,
            },
        )

    def get_weekly_report(
        self,
        user_id: str,
        week_start: date,
    ) -> WeeklyDisciplineReport:
        """Generate weekly discipline report from journal entries."""
        week_end = week_start + timedelta(days=6)

        entries = self._entries.get(user_id, [])
        week_entries = [
            e for e in entries
            if week_start <= e.trade_date <= week_end
        ]

        total_trades = len(week_entries)
        disciplined = [e for e in week_entries if e.discipline_score >= 75]
        undisciplined = [e for e in week_entries if e.discipline_score < 50]

        avg_score = (
            sum(e.discipline_score for e in week_entries) / total_trades
            if total_trades > 0
            else 0.0
        )

        pnl_disciplined = sum(e.realised_pnl_inr for e in disciplined)
        pnl_undisciplined = sum(e.realised_pnl_inr for e in undisciplined)
        total_pnl = sum(e.realised_pnl_inr for e in week_entries)

        total_overrides = sum(e.override_count for e in week_entries)

        # Generate insight
        if total_trades == 0:
            top_insight = "No trades this week."
        elif pnl_disciplined > 0 and pnl_undisciplined < 0:
            diff = pnl_disciplined - pnl_undisciplined
            top_insight = (
                f"Your disciplined trades earned {pnl_disciplined:.0f} more "
                f"than undisciplined ones (net difference: {diff:.0f})."
            )
        elif avg_score >= 75:
            top_insight = (
                f"Excellent discipline this week! Average score: {avg_score:.0f}. "
                f"Keep following your plan."
            )
        elif avg_score < 50:
            top_insight = (
                f"Discipline needs improvement. Average score: {avg_score:.0f}. "
                f"Focus on sticking to your plan and respecting stops."
            )
        else:
            top_insight = (
                f"Mixed week. Average discipline score: {avg_score:.0f}. "
                f"Total P&L: {total_pnl:.0f}."
            )

        return WeeklyDisciplineReport(
            user_id=user_id,
            week_start=week_start,
            week_end=week_end,
            total_trades=total_trades,
            disciplined_trades=len(disciplined),
            undisciplined_trades=len(undisciplined),
            avg_discipline_score=round(avg_score, 1),
            pnl_disciplined_trades_inr=pnl_disciplined,
            pnl_undisciplined_trades_inr=pnl_undisciplined,
            total_pnl_inr=total_pnl,
            signals_skipped=0,  # tracked externally
            override_count=total_overrides,
            net_override_impact_inr=0.0,  # requires override outcome data
            circuit_breaker_triggers=0,    # tracked externally
            top_insight=top_insight,
        )

    def get_rolling_discipline_score(self, user_id: str, last_n: int = 30) -> float:
        """Compute rolling N-trade weighted average discipline score."""
        entries = self._entries.get(user_id, [])
        if not entries:
            return 0.0

        recent = sorted(entries, key=lambda e: e.exit_time, reverse=True)[:last_n]
        if not recent:
            return 0.0

        # Weighted: more recent trades have higher weight
        total_weight = 0.0
        weighted_score = 0.0
        for i, entry in enumerate(recent):
            weight = last_n - i  # most recent gets highest weight
            weighted_score += entry.discipline_score * weight
            total_weight += weight

        return round(weighted_score / total_weight, 1) if total_weight > 0 else 0.0
