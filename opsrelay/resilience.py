"""Retries, timeouts and circuit breakers for calls to specialist agents.

    call -> timeout -> retry (exponential backoff) -> ... -> circuit breaker -> dead letter

Each specialist has a circuit breaker. After `failure_threshold` consecutive failures it opens
and calls fail fast for `recovery_seconds`; then one trial call is let through (half-open), and
a success closes it again.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .config import get_settings


class CircuitOpen(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int
    recovery_seconds: float
    failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at >= self.recovery_seconds:
            return "half-open"
        return "open"

    def before_call(self) -> None:
        with self._lock:
            if self._state() == "open":
                wait = self.recovery_seconds - (time.monotonic() - (self.opened_at or 0))
                raise CircuitOpen(f"circuit open after {self.failures} consecutive failures; retry in {wait:.0f}s")

    def record_success(self) -> None:
        with self._lock:
            self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self._state() == "half-open" or self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def breaker(name: str) -> CircuitBreaker:
    with _breakers_lock:
        if name not in _breakers:
            settings = get_settings()
            _breakers[name] = CircuitBreaker(settings.breaker_failure_threshold, settings.breaker_recovery_seconds)
        return _breakers[name]


def reset_breakers() -> None:
    with _breakers_lock:
        _breakers.clear()


@dataclass
class Attempt:
    number: int
    error: str


async def call_with_retry(
    name: str,
    call: Callable[[], Awaitable[str]],
    *,
    done: Callable[[], bool] = lambda: False,
    on_retry: Callable[[Attempt], None] = lambda _a: None,
) -> str:
    """Run `call` with a timeout per attempt, retrying failures with exponential backoff.

    `done` is checked before each retry: if the previous attempt already achieved its goal (it
    failed only on the way back), the result stands and nothing is repeated. Raises the last error
    when every attempt fails.
    """
    settings = get_settings()
    circuit = breaker(name)
    last: Exception | None = None
    for number in range(1, max(1, settings.agent_max_attempts) + 1):
        if number > 1:
            if done():
                return "(the previous attempt completed before its reply was lost)"
            await asyncio.sleep(settings.retry_backoff_seconds * 2 ** (number - 2))
        try:
            circuit.before_call()
        except CircuitOpen as e:
            last = e
            break
        try:
            reply = await asyncio.wait_for(call(), timeout=settings.agent_timeout_seconds)
        except Exception as e:  # noqa: BLE001 - every failure mode is retried the same way
            circuit.record_failure()
            last = (
                TimeoutError(f"no reply within {settings.agent_timeout_seconds:g}s")
                if isinstance(e, TimeoutError)
                else e
            )
            on_retry(Attempt(number, f"{type(last).__name__}: {last}"))
            continue
        circuit.record_success()
        return reply
    assert last is not None
    raise last
