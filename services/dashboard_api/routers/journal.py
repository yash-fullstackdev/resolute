"""
Journal router — trade journal management.

GET   /api/v1/journal                  → Paginated trade journal
GET   /api/v1/journal/{position_id}    → Single journal entry
PATCH /api/v1/journal/{position_id}    → Add post-trade notes
"""

import json
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..db import rls_session
from ..models.schemas import JournalPatchInput

logger = structlog.get_logger(service="dashboard_api", module="journal")

router = APIRouter(prefix="/api/v1/journal", tags=["journal"])


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
async def list_journal_entries(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, le=100),
    strategy_name: str | None = None,
    underlying: str | None = None,
):
    """Get paginated trade journal entries for the authenticated tenant."""
    tenant_id = request.state.tenant_id
    offset = (page - 1) * page_size

    async with rls_session(tenant_id) as session:
        query = "SELECT * FROM trade_journal WHERE tenant_id = :tenant_id"
        count_query = "SELECT COUNT(*) FROM trade_journal WHERE tenant_id = :tenant_id"
        params: dict = {"tenant_id": tenant_id}

        if strategy_name:
            query += " AND strategy_name = :strategy_name"
            count_query += " AND strategy_name = :strategy_name"
            params["strategy_name"] = strategy_name

        if underlying:
            query += " AND underlying = :underlying"
            count_query += " AND underlying = :underlying"
            params["underlying"] = underlying

        query += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        params["limit"] = page_size
        params["offset"] = offset

        result = await session.execute(text(query), params)
        rows = result.mappings().all()

        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        count_result = await session.execute(text(count_query), count_params)
        total = count_result.scalar() or 0

    logger.info("journal_listed", tenant_id=tenant_id, count=len(rows), page=page)

    return {
        "entries": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/{position_id}")
async def get_journal_entry(request: Request, position_id: str):
    """Get a single journal entry by position ID (must belong to tenant)."""
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        result = await session.execute(
            text("""
                SELECT * FROM trade_journal
                WHERE position_id = :position_id AND tenant_id = :tenant_id
            """),
            {"position_id": position_id, "tenant_id": tenant_id},
        )
        row = result.mappings().first()

    if not row:
        return _error("NOT_FOUND", f"Journal entry for position {position_id} not found.", 404)

    logger.info("journal_entry_retrieved", tenant_id=tenant_id, position_id=position_id)
    return dict(row)


@router.patch("/{position_id}")
async def update_journal_entry(
    request: Request,
    position_id: str,
    body: JournalPatchInput,
):
    """
    Add post-trade notes to a journal entry.
    Requires SEMI_AUTO tier for write (enforced by subscription middleware for PATCH).
    """
    tenant_id = request.state.tenant_id

    async with rls_session(tenant_id) as session:
        # Verify entry exists
        existing = await session.execute(
            text("""
                SELECT id FROM trade_journal
                WHERE position_id = :position_id AND tenant_id = :tenant_id
            """),
            {"position_id": position_id, "tenant_id": tenant_id},
        )
        if not existing.first():
            return _error("NOT_FOUND", f"Journal entry for position {position_id} not found.", 404)

        # Update with notes and tags
        await session.execute(
            text("""
                UPDATE trade_journal
                SET post_trade_notes = :notes,
                    tags = :tags::jsonb,
                    updated_at = NOW()
                WHERE position_id = :position_id AND tenant_id = :tenant_id
            """),
            {
                "position_id": position_id,
                "tenant_id": tenant_id,
                "notes": body.post_trade_notes,
                "tags": json.dumps(body.tags),
            },
        )

    logger.info(
        "journal_entry_updated",
        tenant_id=tenant_id,
        position_id=position_id,
    )

    return {"message": "Journal entry updated.", "position_id": position_id}
