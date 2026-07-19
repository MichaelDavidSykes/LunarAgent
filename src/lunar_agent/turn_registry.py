from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar


TurnResult = TypeVar("TurnResult")


class TurnAlreadyActiveError(RuntimeError):
    """Raised when the same turn is already owned by another request task."""


class TurnCancelledError(RuntimeError):
    """Raised when cancellation reached the service before the response request."""


class TurnIdentityConflictError(RuntimeError):
    """Raised when one request identity is reused with different turn input."""


@dataclass(frozen=True)
class TurnCancellationResult:
    active: bool


@dataclass
class _TurnExecution:
    fingerprint: str
    task: asyncio.Task
    completed_at: float | None = None


class ExplorerTurnRegistry:
    """Share one exact Codex execution across interrupted HTTP deliveries."""

    def __init__(
        self,
        *,
        tombstone_ttl_seconds: int = 1200,
        result_ttl_seconds: int = 1200,
        max_tombstones: int = 2048,
        max_results: int = 2048,
    ):
        self._tombstone_ttl_seconds = max(60, min(int(tombstone_ttl_seconds), 3600))
        self._result_ttl_seconds = max(60, min(int(result_ttl_seconds), 3600))
        self._max_tombstones = max(64, min(int(max_tombstones), 8192))
        self._max_results = max(64, min(int(max_results), 8192))
        self._active: dict[tuple[str, str], asyncio.Task] = {}
        self._executions: OrderedDict[tuple[str, str], _TurnExecution] = OrderedDict()
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
        completed_expired = [
            key
            for key, execution in self._executions.items()
            if (
                execution.completed_at is not None
                and execution.completed_at + self._result_ttl_seconds <= now
            )
        ]
        for key in completed_expired:
            self._executions.pop(key, None)
        while len(self._executions) > self._max_results:
            removable = next(
                (
                    key
                    for key, execution in self._executions.items()
                    if execution.completed_at is not None
                ),
                None,
            )
            if removable is None:
                break
            self._executions.pop(removable, None)

    async def run_or_join(
        self,
        session_id: str,
        request_id: str,
        fingerprint: str,
        operation: Callable[[], Awaitable[TurnResult]],
    ) -> TurnResult:
        """Run once, let exact retries join, and retain the bounded result."""
        key = self._key(session_id, request_id)
        normalized_fingerprint = str(fingerprint or "").strip()
        if not normalized_fingerprint:
            raise ValueError("fingerprint is required")
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            if key in self._cancelled:
                raise TurnCancelledError("turn was cancelled")
            execution = self._executions.get(key)
            if execution is not None:
                if execution.fingerprint != normalized_fingerprint:
                    raise TurnIdentityConflictError("turn identity conflict")
                self._executions.move_to_end(key)
                task = execution.task
            else:
                task = asyncio.create_task(operation())
                execution = _TurnExecution(
                    fingerprint=normalized_fingerprint,
                    task=task,
                )
                self._executions[key] = execution
                self._active[key] = task

                def completed(completed_task: asyncio.Task) -> None:
                    execution.completed_at = time.monotonic()
                    if self._active.get(key) is completed_task:
                        self._active.pop(key, None)
                    if not completed_task.cancelled():
                        # A disconnected HTTP waiter may leave only the
                        # retained canonical task. Retrieve its exception to
                        # avoid an unhandled-task warning; later exact joins
                        # still receive the same stored task outcome.
                        completed_task.exception()

                task.add_done_callback(completed)
        return await asyncio.shield(task)

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
            self._executions.pop(key, None)
        active = task is not None and not task.done()
        if active and task is not asyncio.current_task():
            task.cancel()
        return TurnCancellationResult(active=active)
