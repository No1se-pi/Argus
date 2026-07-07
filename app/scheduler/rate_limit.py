import asyncio
from time import monotonic


class TelegramRateLimiter:
    def __init__(self, min_delay_seconds: float) -> None:
        self.min_delay_seconds = min_delay_seconds
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            elapsed = monotonic() - self._last_request_at
            wait_for = self.min_delay_seconds - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_request_at = monotonic()
