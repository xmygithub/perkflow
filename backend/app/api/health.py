from fastapi import APIRouter, Request
from sqlalchemy import select

from backend.app.core.database import SessionDep
from backend.app.models import ShopType

router = APIRouter(prefix="/health", tags=["健康检查"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, session: SessionDep) -> dict[str, str]:
    # Check connectivity AND the first business table; SELECT 1 alone misses missing schema.
    await session.execute(select(ShopType.id).limit(1))
    await request.app.state.session_store.redis.ping()
    return {"status": "ok", "database": "ok", "redis": "ok"}
