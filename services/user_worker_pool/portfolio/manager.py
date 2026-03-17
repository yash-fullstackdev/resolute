"""
PortfolioManager -- tracks open positions, P&L, and portfolio value per user.

Maintains in-memory state loaded from DB on worker startup.
All state changes are persisted to DB asynchronously.
"""

from __future__ import annotations

from datetime import datetime, date, timezone
from typing import Optional

import structlog

from ..strategies.base import Position, FillConfirmation, Leg

logger = structlog.get_logger(service="user_worker_pool", module="portfolio")


class PortfolioManager:
    """Per-user portfolio state manager."""

    def __init__(self, tenant_id: str, db=None) -> None:
        self._tenant_id = tenant_id
        self._db = db
        self._positions: dict[str, Position] = {}  # position_id -> Position
        self._portfolio_value_inr: float = 0.0
        self._realised_pnl_today: float = 0.0
        self._unrealised_pnl_today: float = 0.0

    @property
    def open_positions(self) -> list[Position]:
        """Return all open positions."""
        return [p for p in self._positions.values() if p.status == "OPEN"]

    @property
    def all_positions(self) -> list[Position]:
        """Return all positions (open and closed)."""
        return list(self._positions.values())

    @property
    def portfolio_value_inr(self) -> float:
        return self._portfolio_value_inr

    @portfolio_value_inr.setter
    def portfolio_value_inr(self, value: float) -> None:
        self._portfolio_value_inr = value

    @property
    def realised_pnl_today(self) -> float:
        return self._realised_pnl_today

    @property
    def unrealised_pnl_today(self) -> float:
        return self._unrealised_pnl_today

    @property
    def total_open_premium(self) -> float:
        """Total premium deployed in open positions."""
        return sum(p.entry_cost_inr for p in self.open_positions)

    async def load_from_db(self) -> None:
        """Load open positions and portfolio value from DB."""
        if self._db is None:
            return

        try:
            # Load portfolio value from user_strategy_configs (any row for this tenant)
            row = await self._db.fetchrow(
                """
                SELECT portfolio_value_inr
                FROM user_strategy_configs
                WHERE tenant_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                self._tenant_id,
                tenant_id=self._tenant_id,
            )
            if row:
                self._portfolio_value_inr = float(row["portfolio_value_inr"])

            # Load open positions (map schema columns to dataclass fields via aliases)
            position_rows = await self._db.fetch(
                """
                SELECT id AS position_id,
                       strategy AS strategy_name,
                       underlying, segment,
                       entry_time,
                       COALESCE(entry_cost_inr, 0) AS entry_cost_inr,
                       COALESCE(entry_cost_inr, 0) AS current_value_inr,
                       stop_loss_price, target_price, time_stop,
                       1 AS lots, status
                FROM positions
                WHERE tenant_id = $1 AND status = 'OPEN'
                """,
                self._tenant_id,
                tenant_id=self._tenant_id,
            )

            for row in position_rows:
                position = Position(
                    position_id=str(row["position_id"]),
                    tenant_id=self._tenant_id,
                    strategy_name=row["strategy_name"],
                    underlying=row["underlying"],
                    segment=row["segment"],
                    legs=[],  # legs loaded separately if needed
                    entry_time=row["entry_time"],
                    entry_cost_inr=float(row["entry_cost_inr"]),
                    current_value_inr=float(row["current_value_inr"]),
                    stop_loss_price=float(row["stop_loss_price"]),
                    target_price=float(row["target_price"]),
                    time_stop=row["time_stop"],
                    lots=row["lots"],
                    status=row["status"],
                )
                self._positions[position.position_id] = position

            # Load today's realised P&L
            pnl_row = await self._db.fetchrow(
                """
                SELECT COALESCE(SUM(realised_pnl_inr), 0) AS total_pnl
                FROM trade_journal
                WHERE tenant_id = $1 AND trade_date = CURRENT_DATE
                """,
                self._tenant_id,
                tenant_id=self._tenant_id,
            )
            if pnl_row:
                self._realised_pnl_today = float(pnl_row["total_pnl"])

            logger.info(
                "portfolio_loaded",
                tenant_id=self._tenant_id,
                portfolio_value=self._portfolio_value_inr,
                open_positions=len(self.open_positions),
                realised_pnl_today=self._realised_pnl_today,
            )

        except Exception as exc:
            logger.error(
                "portfolio_load_failed",
                tenant_id=self._tenant_id,
                error=str(exc),
            )

    def add_position(self, position: Position) -> None:
        """Add a new open position."""
        self._positions[position.position_id] = position
        logger.info(
            "position_added",
            tenant_id=self._tenant_id,
            position_id=position.position_id,
            strategy=position.strategy_name,
            underlying=position.underlying,
            entry_cost=position.entry_cost_inr,
        )

    def close_position(
        self,
        position_id: str,
        exit_value_inr: float,
        exit_reason: str,
        exit_time: datetime | None = None,
    ) -> Position | None:
        """Close a position and update P&L."""
        position = self._positions.get(position_id)
        if position is None:
            logger.warning("position_not_found", position_id=position_id)
            return None

        position.status = "CLOSED"
        position.exit_value_inr = exit_value_inr
        position.exit_reason = exit_reason
        position.exit_time = exit_time or datetime.now(timezone.utc)
        position.pnl_inr = exit_value_inr - position.entry_cost_inr

        self._realised_pnl_today += position.pnl_inr

        logger.info(
            "position_closed",
            tenant_id=self._tenant_id,
            position_id=position_id,
            strategy=position.strategy_name,
            pnl=round(position.pnl_inr, 2),
            exit_reason=exit_reason,
        )

        return position

    def on_fill(self, fill: FillConfirmation) -> Position | None:
        """Process a fill confirmation from order_router."""
        position = self._positions.get(fill.position_id)

        if fill.fill_type == "OPEN":
            # Position already added via add_position, just update
            if position:
                position.current_value_inr = fill.fill_price
            return position

        if position is None:
            logger.warning(
                "fill_for_unknown_position",
                tenant_id=fill.tenant_id,
                position_id=fill.position_id,
            )
            return None

        # Close/stop/target fills
        if fill.fill_type in {"CLOSE", "STOP_HIT", "TIME_STOP", "TARGET_HIT"}:
            return self.close_position(
                position_id=fill.position_id,
                exit_value_inr=fill.fill_price,
                exit_reason=fill.fill_type,
                exit_time=fill.filled_at,
            )

        return position

    def update_unrealised_pnl(self, current_chain) -> None:
        """Update unrealised P&L for all open positions from current chain data."""
        total_unrealised = 0.0

        for position in self.open_positions:
            current_value = 0.0
            for leg in position.legs:
                for s in current_chain.strikes:
                    if abs(s.strike - leg.strike) < 0.01:
                        premium = (
                            s.call_ltp if leg.option_type == "CE" else s.put_ltp
                        )
                        if leg.action == "BUY":
                            current_value += premium * leg.lots
                        else:
                            current_value -= premium * leg.lots
                        break

            position.current_value_inr = current_value
            total_unrealised += current_value - position.entry_cost_inr

        self._unrealised_pnl_today = total_unrealised

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L at start of new trading day."""
        self._realised_pnl_today = 0.0
        self._unrealised_pnl_today = 0.0

    async def persist_position(self, position: Position) -> None:
        """Persist position state to DB."""
        if self._db is None:
            return
        try:
            import json as _json
            legs_json = _json.dumps([
                {"option_type": l.option_type, "strike": l.strike,
                 "expiry": l.expiry.isoformat(), "action": l.action,
                 "lots": l.lots, "premium": l.premium}
                for l in position.legs
            ])
            await self._db.execute(
                """
                INSERT INTO positions
                    (id, tenant_id, strategy, underlying, segment,
                     entry_time, entry_cost_inr,
                     stop_loss_price, target_price, time_stop,
                     legs, status,
                     exit_time, exit_value_inr, exit_reason, realised_pnl_inr)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::jsonb, $12, $13, $14, $15, $16)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    exit_time = EXCLUDED.exit_time,
                    exit_value_inr = EXCLUDED.exit_value_inr,
                    exit_reason = EXCLUDED.exit_reason,
                    realised_pnl_inr = EXCLUDED.realised_pnl_inr
                """,
                position.position_id, position.tenant_id,
                position.strategy_name, position.underlying, position.segment,
                position.entry_time, position.entry_cost_inr,
                position.stop_loss_price, position.target_price, position.time_stop,
                legs_json, position.status,
                position.exit_time, position.exit_value_inr,
                position.exit_reason, position.pnl_inr,
                tenant_id=position.tenant_id,
            )
        except Exception as exc:
            logger.error(
                "position_persist_failed",
                position_id=position.position_id,
                error=str(exc),
            )
