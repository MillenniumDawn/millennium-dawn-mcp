"""Bounded thread offload for blocking MCP handlers."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from functools import partial
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class BoundedOffloader:
    """Run blocking callables in a bounded thread pool.

    The semaphore covers both running and queued work. Its slot is released by
    the concurrent future's completion callback, rather than by the awaiting
    task, because cancelling an asyncio task does not stop an already-running
    thread.
    """

    def __init__(self, *, workers: int = 4, capacity: int = 8) -> None:
        if workers < 1 or capacity < workers:
            raise ValueError("capacity must be at least one and no smaller than workers")
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="md-mcp")
        self._capacity = asyncio.Semaphore(capacity)

    async def run(self, function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Await a blocking function without occupying the event-loop thread."""
        await self._capacity.acquire()
        loop = asyncio.get_running_loop()
        try:
            future: Future[T] = self._executor.submit(partial(function, *args, **kwargs))
        except BaseException:
            self._capacity.release()
            raise

        def release_slot(_: Future[T]) -> None:
            # The server loop may close after a cancelled call leaves this worker running.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(self._capacity.release)

        future.add_done_callback(release_slot)
        return await asyncio.wrap_future(future, loop=loop)
