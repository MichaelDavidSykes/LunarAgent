import asyncio

import pytest

from lunar_agent.turn_registry import (
    ExplorerTurnRegistry,
    TurnCancelledError,
    TurnIdentityConflictError,
)


def test_cancel_stops_only_the_exact_active_turn():
    async def exercise():
        registry = ExplorerTurnRegistry()
        started = asyncio.Event()

        async def worker():
            await registry.register("session-a", "request-a", asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await registry.unregister("session-a", "request-a", asyncio.current_task())

        task = asyncio.create_task(worker())
        await started.wait()

        unrelated = await registry.cancel("session-a", "request-b")
        assert unrelated.active is False
        assert task.done() is False

        matching = await registry.cancel("session-a", "request-a")
        assert matching.active is True
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_cancel_before_registration_fences_late_response():
    async def exercise():
        registry = ExplorerTurnRegistry()
        result = await registry.cancel("session-a", "request-a")
        assert result.active is False

        with pytest.raises(TurnCancelledError):
            await registry.register(
                "session-a",
                "request-a",
                asyncio.current_task(),
            )

    asyncio.run(exercise())


def test_exact_retries_join_one_execution_and_reuse_its_result():
    async def exercise():
        registry = ExplorerTurnRegistry()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return {"reply": "one durable result"}

        first = asyncio.create_task(registry.run_or_join(
            "session-a",
            "request-a",
            "fingerprint-a",
            operation,
        ))
        await started.wait()
        second = asyncio.create_task(registry.run_or_join(
            "session-a",
            "request-a",
            "fingerprint-a",
            operation,
        ))
        release.set()

        assert await first == {"reply": "one durable result"}
        assert await second == {"reply": "one durable result"}
        assert await registry.run_or_join(
            "session-a",
            "request-a",
            "fingerprint-a",
            operation,
        ) == {"reply": "one durable result"}
        assert calls == 1

    asyncio.run(exercise())


def test_interrupted_waiter_does_not_cancel_the_shared_execution():
    async def exercise():
        registry = ExplorerTurnRegistry()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return "recovered"

        interrupted = asyncio.create_task(registry.run_or_join(
            "session-a",
            "request-a",
            "fingerprint-a",
            operation,
        ))
        await started.wait()
        interrupted.cancel()
        with pytest.raises(asyncio.CancelledError):
            await interrupted

        recovered = asyncio.create_task(registry.run_or_join(
            "session-a",
            "request-a",
            "fingerprint-a",
            operation,
        ))
        release.set()

        assert await recovered == "recovered"
        assert calls == 1

    asyncio.run(exercise())


def test_request_identity_conflict_never_joins_or_reruns():
    async def exercise():
        registry = ExplorerTurnRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            started.set()
            await release.wait()
            return "done"

        first = asyncio.create_task(registry.run_or_join(
            "session-a",
            "request-a",
            "fingerprint-a",
            operation,
        ))
        await started.wait()
        with pytest.raises(TurnIdentityConflictError):
            await registry.run_or_join(
                "session-a",
                "request-a",
                "fingerprint-b",
                operation,
            )
        release.set()
        assert await first == "done"

    asyncio.run(exercise())
