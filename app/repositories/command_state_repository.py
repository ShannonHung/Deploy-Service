import json
import asyncio
from datetime import timedelta
from typing import Callable, Coroutine
from redis.asyncio import Redis
from app.domain.command import CommandState
from app.core.exceptions import CommandExecutionException

class CommandStateRepository:
    PREFIX = "command"

    def __init__(self, redis: Redis):
        self.redis = redis

    def _key(self, command_id: str) -> str:
        return f"{self.PREFIX}:{command_id}"

    async def save(self, state: CommandState, ttl_seconds: int):
        await self.redis.setex(
            self._key(state.command_id),
            timedelta(seconds=ttl_seconds),
            state.model_dump_json()
        )

    async def get(self, command_id: str) -> CommandState:
        data = await self.redis.get(self._key(command_id))
        if not data:
            raise CommandExecutionException(f"Execution record {command_id} not found in Redis.")
        try:
            return CommandState.model_validate_json(data)
        except Exception:
            raise CommandExecutionException(f"Invalid record format for {command_id}.")

    async def update(self, command_id: str, updater: Callable[[CommandState], Coroutine[None, None, None] | None], ttl_seconds: int):
        """Fetch, apply synchronous or asynchronous updater function, and save state."""
        state = await self.get(command_id)
        result = updater(state)
        if asyncio.iscoroutine(result):
            await result
        await self.save(state, ttl_seconds)
