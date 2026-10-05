"""Concurrency and failure behavior for bounded blocking work."""

import asyncio
import threading

import pytest

from md_mcp.util.offload import BoundedOffloader


def test_offloader_propagates_worker_exception() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=1, capacity=1)
        lock = asyncio.Lock()

        def fail() -> None:
            raise ValueError("worker failed")

        with pytest.raises(ValueError, match="worker failed"):
            await offloader.run_serialized(lock, fail)

        assert await offloader.run_serialized(lock, lambda: "recovered") == "recovered"

    asyncio.run(run())


def test_cancelled_serialized_worker_keeps_lock_until_worker_finishes() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=2, capacity=2)
        lock = asyncio.Lock()
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

        first_task = asyncio.create_task(offloader.run_serialized(lock, first))
        await asyncio.to_thread(started.wait, 1)
        first_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_task

        second_task = asyncio.create_task(offloader.run_serialized(lock, second))
        await asyncio.sleep(0.05)
        assert not second_started.is_set()

        release.set()
        assert await second_task == "second"
        assert second_started.is_set()

    asyncio.run(run())


def test_cancelled_serial_waiter_does_not_occupy_worker() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=2, capacity=3)
        lock = asyncio.Lock()
        started = threading.Event()
        release = threading.Event()

        def first() -> None:
            started.set()
            release.wait(timeout=3)

        first_task = asyncio.create_task(offloader.run_serialized(lock, first))
        await asyncio.to_thread(started.wait, 1)

        waiter_entered = asyncio.Event()

        async def wait_for_lock() -> None:
            waiter_entered.set()
            await offloader.run_serialized(lock, lambda: None)

        waiting_task = asyncio.create_task(wait_for_lock())
        await waiter_entered.wait()
        await asyncio.sleep(0.05)

        # The second call is waiting on the async lock, not occupying the other
        # pool thread. Independent work can still use that worker.
        assert (
            await asyncio.wait_for(offloader.run(lambda: "free worker"), timeout=0.5)
            == "free worker"
        )

        waiting_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_task
        release.set()
        await first_task

    asyncio.run(run())


def test_cancelled_serial_waiter_releases_admission_slot() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=2, capacity=2)
        lock = asyncio.Lock()
        started = threading.Event()
        release = threading.Event()
        independent_started = threading.Event()

        def first() -> None:
            started.set()
            release.wait(timeout=3)

        first_task = asyncio.create_task(offloader.run_serialized(lock, first))
        await asyncio.to_thread(started.wait, 1)

        waiter_entered = asyncio.Event()

        async def wait_for_lock() -> None:
            waiter_entered.set()
            await offloader.run_serialized(lock, lambda: None)

        waiting_task = asyncio.create_task(wait_for_lock())
        await waiter_entered.wait()
        await asyncio.sleep(0.05)

        waiting_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_task

        def independent() -> None:
            independent_started.set()

        await asyncio.wait_for(offloader.run(independent), timeout=0.5)
        assert independent_started.is_set()

        release.set()
        await first_task

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
