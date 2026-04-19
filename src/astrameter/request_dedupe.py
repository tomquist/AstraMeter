from __future__ import annotations

import time
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)


class RequestDeduplicator(Generic[K]):
    """Drop repeated incoming requests within a time window.

    Callers pick the key (e.g. a battery IP for the Shelly emulator, a
    consumer id for CT002). A window of 0 disables dedup and every
    request is allowed through.
    """

    def __init__(
        self,
        window_seconds: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._window = max(0.0, window_seconds)
        self._clock = clock or time.monotonic
        self._last: dict[K, float] = {}

    def should_process(self, key: K) -> bool:
        if self._window <= 0.0:
            return True
        now = self._clock()
        last = self._last.get(key)
        if last is not None and (now - last) < self._window:
            return False
        self._last[key] = now
        return True

    def purge_older_than(self, max_age_seconds: float) -> None:
        if not self._last:
            return
        cutoff = self._clock() - max_age_seconds
        self._last = {k: t for k, t in self._last.items() if t >= cutoff}
