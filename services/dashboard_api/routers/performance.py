"""
Performance router — P&L summaries and daily time series.

GET /api/v1/performance        → P&L summary for the authenticated tenant
GET /api/v1/performance/daily  → Daily P&L time series
"""

import uuid
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="performance")

router = APIRouter(prefix="/api/v1/performance", tags=["performance"])


def _error(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            },
            "request_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )


@router.get("")
async def get_performance(
    request: Request,
    period_start: date | None = Query(None, description="Start date for period filter"),
    period_end: date | None = Query(None, description="End date for period filter"),
):
    """
    Get P&L summary for the authenticated tenant.
    Includes total P&L, win rate, average win/loss, max drawdown.
    """
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            query = """
                SELECT
                    COALESCE(SUM(realised_pnl_inr), 0) AS realised_pnl_inr,
                    COALESCE(SUM(CASE WHEN status = 'OPEN' THEN unrealised_pnl_inr ELSE 0 END), 0) AS unrealised_pnl_inr,
                    COUNT(*) FILTER (WHERE status != 'OPEN') AS total_trades,
                    COUNT(*) FILTER (WHERE realised_pnl_inr > 0 AND status != 'OPEN') AS winning_trades,
                    COUNT(*) FILTER (WHERE realised_pnl_inr <= 0 AND status != 'OPEN') AS losing_trades,
                    COALESCE(AVG(realised_pnl_inr) FILTER (WHERE realised_pnl_inr > 0 AND status != 'OPEN'), 0) AS avg_win_inr,
                    COALESCE(AVG(realised_pnl_inr) FILTER (WHERE realised_pnl_inr <= 0 AND status != 'OPEN'), 0) AS avg_loss_inr,
                    COALESCE(MIN(realised_pnl_inr) FILTER (WHERE status != 'OPEN'), 0) AS max_drawdown_inr
                FROM positions
                WHERE tenant_id = :tenant_id
            """
            params: dict = {"tenant_id": tenant_id}

            if period_start:
                query += " AND entry_time >= :period_start"
                params["period_start"] = period_start.isoformat()

            if period_end:
                query += " AND entry_time <= :period_end"
                params["period_end"] = period_end.isoformat()

            result = await session.execute(text(query), params)
            row = result.mappings().first()
    except Exception as exc:
        logger.warning("performance_query_failed", tenant_id=tenant_id, error=str(exc))
        row = None

    if not row:
        row = {
            "realised_pnl_inr": 0, "unrealised_pnl_inr": 0,
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "avg_win_inr": 0, "avg_loss_inr": 0, "max_drawdown_inr": 0,
        }

    total_trades = row["total_trades"] or 0
    winning_trades = row["winning_trades"] or 0
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0.0

    logger.info("performance_retrieved", tenant_id=tenant_id)

    return {
        "success": True,
        "data": {
            "total_pnl": float(row["realised_pnl_inr"]) + float(row["unrealised_pnl_inr"]),
            "realised_pnl_inr": float(row["realised_pnl_inr"]),
            "unrealised_pnl_inr": float(row["unrealised_pnl_inr"]),
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": row["losing_trades"] or 0,
            "win_rate": round(win_rate, 2),
            "avg_win_inr": round(float(row["avg_win_inr"]), 2),
            "avg_loss_inr": round(float(row["avg_loss_inr"]), 2),
            "max_drawdown_pct": 0,
            "avg_return_pct": 0,
            "best_day": {"date": "N/A", "pnl": 0},
            "worst_day": {"date": "N/A", "pnl": 0},
            "sharpe_ratio": None,
            "profit_factor": None,
            "period_start": period_start.isoformat() if period_start else None,
            "period_end": period_end.isoformat() if period_end else None,
        },
    }


@router.get("/daily")
async def get_daily_performance(
    request: Request,
    days: int = Query(default=30, le=365, ge=1),
):
    """
    Get daily P&L time series for the authenticated tenant.
    Returns one row per day with P&L and trade count.
    """
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    SELECT
                        DATE(entry_time) AS date,
                        COALESCE(SUM(realised_pnl_inr), 0) AS pnl_inr,
                        COUNT(*) AS trades,
                        SUM(COALESCE(SUM(realised_pnl_inr), 0)) OVER (ORDER BY DATE(entry_time)) AS cumulative_pnl_inr
                    FROM positions
                    WHERE tenant_id = :tenant_id
                      AND status != 'OPEN'
                      AND entry_time >= NOW() - INTERVAL :days_interval
                    GROUP BY DATE(entry_time)
                    ORDER BY DATE(entry_time)
                """),
                {"tenant_id": tenant_id, "days_interval": f"{days} days"},
            )
            rows = result.mappings().all()
    except Exception as exc:
        logger.warning("daily_performance_query_failed", tenant_id=tenant_id, error=str(exc))
        rows = []

    logger.info("daily_performance_retrieved", tenant_id=tenant_id, days=days, count=len(rows))

    return {
        "success": True,
        "data": [
            {
                "date": str(r["date"]),
                "pnl": float(r["pnl_inr"]),
            }
            for r in rows
        ],
    }
