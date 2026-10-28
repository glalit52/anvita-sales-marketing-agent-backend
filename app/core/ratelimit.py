import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.errors import RateLimited


class SlidingWindowLimiter:
    """Per-process sliding-window limiter.

    Good enough for a single API replica or as a first line of defence behind the gateway; move the
    counters to Redis before running several replicas that must share a budget.
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            raise RateLimited("Too many requests, slow down")
        hits.append(now)

    def reset(self) -> None:
        self._hits.clear()


limiter = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
