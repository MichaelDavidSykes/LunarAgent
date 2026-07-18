from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass


class TurnAlreadyActiveError(RuntimeError):
    """Raised when the same turn is already owned by another request task."""


class TurnCancelledError(RuntimeError):
    """Raised when cancellation reached the service before the response request."""


@dataclass(frozen=True)
class TurnCancellationResult:
    active: bool


class ExplorerTurnRegistry:
    """Process-local ownership and cancellation tombstones for Codex turns."""

    def __init__(self, *, tombstone_ttl_seconds: int = 1200, max_tombstones: int = 2048):
        self._tombstone_ttl_seconds = max(60, min(int(tombstone_ttl_seconds), 3600))
        self._max_tombstones = max(64, min(int(max_tombstones), 8192))
        self._active: dict[tuple[str, str], asyncio.Task] = {}
        self._cancelled: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(session_id: str, request_id: str) -> tuple[str, str]:
        session = str(session_id or "").strip()
        request = str(request_id or "").strip()
        if not session or not request:
            raise ValueError("session_id and request_id are required")
        return session, request

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, expires_at in self._cancelled.items()
            if expires_at <= now
        ]
        for key in expired:
            self._cancelled.pop(key, None)
        while len(self._cancelled) > self._max_tombstones:
            self._cancelled.popitem(last=False)

    async def register(
        self,
        session_id: str,
        request_id: str,
        task: asyncio.Task,
    ) -> None:
        key = self._key(session_id, request_id)
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            if key in self._cancelled:
                raise TurnCancelledError("turn was cancelled")
            existing = self._active.get(key)
            if existing is not None and existing is not task and not existing.done():
                raise TurnAlreadyActiveError("turn is already active")
            self._active[key] = task

    async def unregister(
        self,
        session_id: str,
        request_id: str,
        task: asyncio.Task,
    ) -> None:
        key = self._key(session_id, request_id)
        async with self._lock:
            if self._active.get(key) is task:
                self._active.pop(key, None)

    async def cancel(
        self,
        session_id: str,
        request_id: str,
    ) -> TurnCancellationResult:
        key = self._key(session_id, request_id)
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            self._cancelled[key] = now + self._tombstone_ttl_seconds
            self._cancelled.move_to_end(key)
            self._prune_locked(now)
            task = self._active.pop(key, None)
        active = task is not None and not task.done()
        if active and task is not asyncio.current_task():
            task.cancel()
        return TurnCancellationResult(active=active)
