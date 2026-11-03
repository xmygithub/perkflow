from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent.model import NativeToolModel
from backend.app.api import assistant, auth, health, shops, vouchers
from backend.app.core.config import Settings
from backend.app.core.database import Database
from backend.app.core.errors import register_error_handlers
from backend.app.core.order_id import RedisOrderId
from backend.app.core.order_lock import RedisOrderLock
from backend.app.core.redis_connection import create_redis_client
from backend.app.core.redis_orders import RedisOrderIngress
from backend.app.core.seckill_store import SeckillStore
from backend.app.core.session_store import SessionStore
from backend.app.services.auth import ConsoleSmsSender


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.database = Database(settings)
        app.state.session_store = SessionStore(settings)
        app.state.cache_redis = create_redis_client(settings)
        app.state.seckill_store = SeckillStore(app.state.cache_redis, settings.seckill_key_prefix)
        app.state.order_ingress = RedisOrderIngress(app.state.seckill_store)
        app.state.order_ids = RedisOrderId(app.state.cache_redis, settings.order_id_key_prefix)
        app.state.order_lock = RedisOrderLock(
            app.state.cache_redis, settings.order_lock_key_prefix, settings.order_lock_ttl
        )
        app.state.sms_sender = ConsoleSmsSender()
        app.state.agent_model = NativeToolModel(settings)
        try:
            yield
        finally:
            await app.state.cache_redis.aclose()
            await app.state.session_store.close()
            await app.state.database.close()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(shops.router)
    app.include_router(auth.router)
    app.include_router(vouchers.router)
    app.include_router(assistant.router)
    return app


app = create_app()
