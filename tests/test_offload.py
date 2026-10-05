"""Concurrency and failure behavior for bounded blocking work."""

import asyncio
import threading

import pytest

from md_mcp.util.offload import BoundedOffloader


def test_offloader_propagates_worker_exception() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=1, capacity=1)

        def fail() -> None:
            raise ValueError("worker failed")

        with pytest.raises(ValueError, match="worker failed"):
            await offloader.run(fail)

    asyncio.run(run())


def test_cancelled_await_keeps_capacity_until_worker_finishes() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=1, capacity=1)
        started = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def first() -> str:
            started.set()
            release.wait(timeout=3)
            return "first"

        def second() -> str:
            second_started.set()
            return "second"

        first_task = asyncio.create_task(offloader.run(first))
        await asyncio.to_thread(started.wait, 1)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        second_task = asyncio.create_task(offloader.run(second))
        await asyncio.sleep(0.05)
        assert not second_started.is_set()

        release.set()
        assert await second_task == "second"
        assert second_started.is_set()

    asyncio.run(run())
