"""Per-database request pacing and the per-call time budget (v1.0.5).

RatePacer grants request start times spaced at least 1/rate seconds apart.
Each acquirer reserves its start time when it calls ``acquire`` — in arrival
order, so waiters are served FIFO — and then sleeps until that time. There is
no burst allowance: after an idle period the first acquirer starts at once and
the next one waits a full interval. Nothing is held across the HTTP request;
``acquire`` returns before the caller sends it.

The reservation is a read-modify-write with no ``await`` in between, so on one
event loop it is atomic without a lock.

A tool call sets a deadline with ``budget(seconds)``; it is carried in a
ContextVar, so every task created inside the call (the per-database fan-out,
the per-citation bulk tasks) sees it. Clients call ``paced(pacer, db_name)``
before each attempt; when the next start would fall after the deadline it
raises ``RateBudgetExhausted`` instead of waiting, and the server reports that
database as skipped.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import time
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "citation_mcp_deadline", default=None
)


class RatePacer:
    """Spaces request starts at least ``1 / rate_per_second`` seconds apart."""

    def __init__(
        self,
        rate_per_second: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        rate = float(rate_per_second)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(f"rate_per_second must be a positive finite number, got {rate_per_second!r}")
        self._rate = rate
        self._interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next_start: float | None = None

    @property
    def rate(self) -> float:
        return self._rate

    async def acquire(self, deadline: float | None = None) -> bool:
        """Wait for this caller's start time; return False if it falls after ``deadline``.

        A refusal returns at once, does not sleep and does not consume a slot.
        Never raises except ``asyncio.CancelledError`` (a cancelled waiter's slot
        stays used, which only ever widens the spacing).
        """
        now = self._clock()
        start = now if self._next_start is None else max(now, self._next_start)
        if deadline is not None and start > deadline:
            return False
        self._next_start = start + self._interval
        delay = start - now
        if delay > 0:
            await self._sleep(delay)
        return True


class RateBudgetExhausted(Exception):
    """Raised by a client when its pacer cannot start a request before the call's deadline.

    Deliberately not a RuntimeError or httpx.HTTPError, so the server reports it
    as a skip rather than as a request failure.
    """

    def __init__(self, db_name: str) -> None:
        super().__init__(f"{db_name} skipped: rate budget exhausted")
        self.db_name = db_name


def current_deadline() -> float | None:
    return _deadline.get()


@contextmanager
def budget(seconds: float | None, clock: Clock = time.monotonic) -> Iterator[float | None]:
    """Set the call's deadline to ``clock() + seconds`` unless an outer call already set one.

    ``seconds=None`` leaves the deadline as it is (no budget). An enclosing
    budget wins, so a bulk call's per-citation verifications share the bulk
    call's single deadline.
    """
    outer = _deadline.get()
    if seconds is None or outer is not None:
        yield outer
        return
    token = _deadline.set(clock() + float(seconds))
    try:
        yield _deadline.get()
    finally:
        _deadline.reset(token)


async def paced(pacer: RatePacer | None, db_name: str) -> None:
    """Acquire ``pacer`` against the current deadline; raise RateBudgetExhausted if refused."""
    if pacer is None:
        return
    if not await pacer.acquire(current_deadline()):
        raise RateBudgetExhausted(db_name)
