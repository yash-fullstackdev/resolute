"""
Reports router — weekly discipline + P&L reports.

GET /api/v1/reports/weekly         → Latest weekly reports
GET /api/v1/reports/weekly/{date}  → Historical weekly report by week start date
"""

import uuid
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session

logger = structlog.get_logger(service="dashboard_api", module="reports")

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])


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


@router.get("/weekly")
async def get_weekly_reports(
    request: Request,
    limit: int = Query(default=12, le=52),
    offset: int = Query(default=0, ge=0),
):
    """Get recent weekly reports for the authenticated tenant."""
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    SELECT * FROM weekly_reports
                    WHERE tenant_id = :tenant_id
                    ORDER BY week_start DESC
                    LIMIT :limit OFFSET :offset
                """),
                {"tenant_id": tenant_id, "limit": limit, "offset": offset},
            )
            rows = result.mappings().all()
        return {"success": True, "data": [dict(r) for r in rows]}
    except Exception as exc:
        logger.warning("weekly_reports_query_failed", error=str(exc))
        return {"success": True, "data": []}


@router.get("/weekly/{week_date}")
async def get_weekly_report_by_date(request: Request, week_date: date):
    """Get a specific weekly report by week start date."""
    tenant_id = request.state.tenant_id

    try:
        async with rls_session(tenant_id) as session:
            result = await session.execute(
                text("""
                    SELECT * FROM weekly_reports
                    WHERE tenant_id = :tenant_id AND week_start = :week_date
                """),
                {"tenant_id": tenant_id, "week_date": week_date},
            )
            row = result.mappings().first()

        if not row:
            return _error("NOT_FOUND", f"Weekly report for week starting {week_date} not found.", 404)
        return {"success": True, "data": dict(row)}
    except Exception as exc:
        logger.warning("weekly_report_query_failed", error=str(exc))
        return _error("NOT_FOUND", "Reports not available yet.", 404)
