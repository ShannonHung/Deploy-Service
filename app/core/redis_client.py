import logging
from typing import Optional
import redis.asyncio as redis
from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

class RedisClient:
    _instance: Optional[redis.Redis] = None

    @classmethod
    async def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            logger.info(f"Connecting to Redis at {settings.REDIS_URL}")
            cls._instance = redis.from_url(
                settings.REDIS_URL, 
                encoding="utf-8", 
                decode_responses=True
            )
        return cls._instance

    @classmethod
    async def close(cls):
        if cls._instance is not None:
            logger.info("Closing Redis connection.")
            await cls._instance.aclose()
            cls._instance = None
