import asyncio

import pytest

from lunar_agent.turn_registry import ExplorerTurnRegistry, TurnCancelledError


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
