"""GET /api/categories."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..security import Principal, current_principal

router = APIRouter(prefix="/api", tags=["categories"])


@router.get("/categories")
async def get_categories(
    _: Principal = Depends(current_principal),
):
    rows = await db.pool().fetch(
        """
        select id, name
          from categories
         order by name, id
        """
    )

    return {
        "items": [
            {"id": row["id"], "name": row["name"]}
            for row in rows
        ]
    }
