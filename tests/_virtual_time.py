"""Virtual time for pacer tests (v1.0.5, CM-108C).

``VirtualTime.clock`` and ``VirtualTime.sleep`` are injected into a RatePacer.
``sleep`` parks the caller on a future keyed by its wake time; ``run`` drives
the coroutines and, whenever every runnable task is parked, advances the clock
to the earliest wake time. No real time passes.
"""

from __future__ import annotations

import asyncio
import heapq
from typing import Any, Awaitable


class VirtualTime:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self._heap: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if delay <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._heap, (self.now + delay, self._seq, fut))
        self._seq += 1
        await fut

    async def run(self, *coros: Awaitable[Any]) -> list[Any]:
        tasks = [asyncio.ensure_future(c) for c in coros]
        idle_spins = 0
        while not all(t.done() for t in tasks):
            for _ in range(50):
                await asyncio.sleep(0)
            if all(t.done() for t in tasks):
                break
            if self._heap:
                wake, _, fut = heapq.heappop(self._heap)
                self.now = max(self.now, wake)
                if not fut.done():
                    fut.set_result(None)
                idle_spins = 0
            else:
                idle_spins += 1
                if idle_spins > 100:
                    raise RuntimeError("VirtualTime: tasks blocked with no pending sleep")
        return [t.result() for t in tasks]
