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
ContextVar together with the clock it was measured on, so every task created
inside the call (the per-database fan-out, the per-citation bulk tasks) sees it.
Clients guard every request and every wait:

- ``paced(pacer, db)`` before each attempt: waits for the pacer (if any) and
  raises ``RateBudgetExhausted`` when the request could not start by the deadline;
  with no pacer it still refuses to start a request after the deadline.
- ``check_wait(delay, db)`` before each backoff, Retry-After or spacing wait:
  raises ``RateBudgetExhausted`` when the wait would end after the deadline.

The server reports that database as skipped. So no request of any kind starts
after the deadline; what can follow it is only a request already in flight.
"""

from __future__ import annotations

import asyncio
import contextvars
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterator

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class _Budget:
    deadline: float
    clock: Clock


_budget: contextvars.ContextVar[_Budget | None] = contextvars.ContextVar(
    "citation_mcp_budget", default=None
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
    b = _budget.get()
    return None if b is None else b.deadline


@contextmanager
def budget(seconds: float | None, clock: Clock = time.monotonic) -> Iterator[float | None]:
    """Set the call's deadline to ``clock() + seconds`` unless an outer call already set one.

    ``seconds=None`` leaves the deadline as it is (no budget). An enclosing
    budget wins, so a bulk call's per-citation verifications share the bulk
    call's single deadline.
    """
    outer = _budget.get()
    if seconds is None or outer is not None:
        yield None if outer is None else outer.deadline
        return
    token = _budget.set(_Budget(deadline=clock() + float(seconds), clock=clock))
    try:
        yield _budget.get().deadline
    finally:
        _budget.reset(token)


def check_start(db_name: str) -> None:
    """Raise RateBudgetExhausted if the deadline has already passed (no request may start)."""
    b = _budget.get()
    if b is not None and b.clock() > b.deadline:
        raise RateBudgetExhausted(db_name)


def wait_fits(delay: float) -> bool:
    """True when a wait of ``delay`` seconds would end by the deadline (or there is none)."""
    b = _budget.get()
    return b is None or b.clock() + max(0.0, delay) <= b.deadline


def check_wait(delay: float, db_name: str) -> None:
    """Raise RateBudgetExhausted if a wait of ``delay`` seconds would end after the deadline.

    Called before a backoff, Retry-After or spacing wait, so that wait is never
    started; the request that would have followed it could not start in time.
    """
    if not wait_fits(delay):
        raise RateBudgetExhausted(db_name)


async def paced(pacer: RatePacer | None, db_name: str) -> None:
    """Guard one request attempt against the call's deadline.

    With a pacer: wait for its slot, or raise RateBudgetExhausted if the slot
    falls after the deadline. Without one: raise if the deadline has passed.
    """
    if pacer is None:
        check_start(db_name)
        return
    if not await pacer.acquire(current_deadline()):
        raise RateBudgetExhausted(db_name)
