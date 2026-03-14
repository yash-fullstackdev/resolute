"""
ReportBuilder -- generates weekly discipline reports.

Aggregates data from TradeJournal, CircuitBreaker, and OverrideGuard
to produce a comprehensive WeeklyDisciplineReport.
"""

from __future__ import annotations

from datetime import date, timedelta

import structlog

from .journal import TradeJournal, WeeklyDisciplineReport
from .circuit_breaker import CircuitBreaker
from .override_guard import OverrideGuard

logger = structlog.get_logger(service="user_worker_pool", module="report_builder")


class ReportBuilder:
    """Builds comprehensive weekly discipline reports."""

    def __init__(
        self,
        journal: TradeJournal,
        circuit_breaker: CircuitBreaker,
        override_guard: OverrideGuard,
    ) -> None:
        self._journal = journal
        self._circuit_breaker = circuit_breaker
        self._override_guard = override_guard

    def build_weekly_report(
        self,
        user_id: str,
        week_start: date | None = None,
    ) -> WeeklyDisciplineReport:
        """Build a complete weekly discipline report.

        If *week_start* is not provided, uses the most recent Monday.
        """
        if week_start is None:
            today = date.today()
            # Find most recent Monday
            week_start = today - timedelta(days=today.weekday())

        # Get base report from journal
        report = self._journal.get_weekly_report(user_id, week_start)

        # Enrich with override data
        override_summary = self._override_guard.get_override_history_summary(user_id)
        report.override_count = override_summary.total_overrides
        report.net_override_impact_inr = override_summary.net_override_impact_inr

        # Enrich insight if overrides had negative impact
        if override_summary.net_override_impact_inr < -100:
            report.top_insight = (
                f"Overrides cost you {abs(override_summary.net_override_impact_inr):.0f} this week. "
                f"{report.top_insight}"
            )

        # Add rolling discipline score context
        rolling_score = self._journal.get_rolling_discipline_score(user_id)
        if rolling_score > 0 and report.total_trades > 0:
            direction = "improving" if report.avg_discipline_score > rolling_score else "declining"
            report.top_insight += (
                f" Your discipline is {direction} "
                f"(this week: {report.avg_discipline_score:.0f}, "
                f"rolling 30-trade: {rolling_score:.0f})."
            )

        logger.info(
            "weekly_report_built",
            tenant_id=user_id,
            week_start=week_start.isoformat(),
            total_trades=report.total_trades,
            avg_score=report.avg_discipline_score,
            total_pnl=report.total_pnl_inr,
        )

        return report

    async def persist_report(self, report: WeeklyDisciplineReport, db) -> None:
        """Persist weekly report to the database."""
        if db is None:
            return
        try:
            await db.execute(
                """
                INSERT INTO weekly_discipline_reports
                    (user_id, week_start, week_end, total_trades,
                     disciplined_trades, undisciplined_trades,
                     avg_discipline_score, pnl_disciplined_trades_inr,
                     pnl_undisciplined_trades_inr, total_pnl_inr,
                     signals_skipped, override_count, net_override_impact_inr,
                     circuit_breaker_triggers, top_insight)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15)
                ON CONFLICT (user_id, week_start) DO UPDATE SET
                    total_trades = EXCLUDED.total_trades,
                    avg_discipline_score = EXCLUDED.avg_discipline_score,
                    total_pnl_inr = EXCLUDED.total_pnl_inr,
                    top_insight = EXCLUDED.top_insight
                """,
                report.user_id, report.week_start, report.week_end,
                report.total_trades, report.disciplined_trades,
                report.undisciplined_trades, report.avg_discipline_score,
                report.pnl_disciplined_trades_inr, report.pnl_undisciplined_trades_inr,
                report.total_pnl_inr, report.signals_skipped, report.override_count,
                report.net_override_impact_inr, report.circuit_breaker_triggers,
                report.top_insight,
                tenant_id=report.user_id,
            )
        except Exception as exc:
            logger.error(
                "report_persist_failed",
                user_id=report.user_id,
                error=str(exc),
            )

    async def publish_score_update(
        self,
        user_id: str,
        nats,
    ) -> None:
        """Publish discipline score update to NATS."""
        rolling_score = self._journal.get_rolling_discipline_score(user_id)
        if nats:
            await nats.publish(
                f"discipline.score.update.{user_id}",
                {
                    "user_id": user_id,
                    "rolling_discipline_score": rolling_score,
                },
            )
