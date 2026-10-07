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
            await offloader.run(fail)

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
            await asyncio.wait_for(offloader.run(lambda: "free worker"), timeout=1.0)
            == "free worker"
        )

        waiting_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_task
        release.set()
        await first_task

    asyncio.run(run())


def test_full_validator_lock_queue_does_not_starve_independent_work() -> None:
    async def run() -> None:
        offloader = BoundedOffloader()
        lock = asyncio.Lock()
        started = threading.Event()
        release = threading.Event()

        def first() -> None:
            started.set()
            release.wait(timeout=3)

        first_task = asyncio.create_task(offloader.run_serialized(lock, first))
        await asyncio.to_thread(started.wait, 1)

        waiter_count = 7
        waiters_entered = asyncio.Event()
        entered = 0

        async def wait_for_validator_lock() -> None:
            nonlocal entered
            entered += 1
            if entered == waiter_count:
                waiters_entered.set()
            await offloader.run_serialized(lock, lambda: None)

        waiting_tasks = [
            asyncio.create_task(wait_for_validator_lock()) for _ in range(waiter_count)
        ]
        await waiters_entered.wait()
        await asyncio.sleep(0.05)

        assert (
            await asyncio.wait_for(offloader.run(lambda: "review branch"), timeout=1.0)
            == "review branch"
        )

        for task in waiting_tasks:
            task.cancel()
        for task in waiting_tasks:
            with pytest.raises(asyncio.CancelledError):
                await task

        release.set()
        await first_task

    asyncio.run(run())


def test_cancelled_capacity_waiter_releases_validator_lock() -> None:
    async def run() -> None:
        offloader = BoundedOffloader(workers=1, capacity=1)
        lock = asyncio.Lock()
        started = threading.Event()
        release = threading.Event()

        def first() -> None:
            started.set()
            release.wait(timeout=3)

        first_task = asyncio.create_task(offloader.run(first))
        await asyncio.to_thread(started.wait, 1)

        waiter_entered = asyncio.Event()

        async def wait_for_capacity() -> None:
            waiter_entered.set()
            await offloader.run_serialized(lock, lambda: None)

        waiting_task = asyncio.create_task(wait_for_capacity())
        await waiter_entered.wait()

        async def wait_until_lock_is_held() -> None:
            while not lock.locked():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_until_lock_is_held(), timeout=1.0)
        assert lock.locked()

        waiting_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting_task
        assert not lock.locked()

        second_task = asyncio.create_task(offloader.run_serialized(lock, lambda: "second"))
        release.set()
        await first_task
        assert await asyncio.wait_for(second_task, timeout=1.0) == "second"

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
