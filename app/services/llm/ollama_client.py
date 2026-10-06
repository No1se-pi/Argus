import asyncio
from collections import deque
from time import monotonic

import aiohttp


class OllamaUnavailable(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, url: str, model: str, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.last_latency_ms: int | None = None
        self.last_error: str | None = None
        self.failure_count = 0
        self._latencies: deque[int] = deque(maxlen=100)

    @property
    def average_latency_ms(self) -> int | None:
        if not self._latencies:
            return None
        return round(sum(self._latencies) / len(self._latencies))

    async def generate(self, *, system: str, prompt: str, json_mode: bool = True) -> str:
        if not self.model:
            raise OllamaUnavailable("OLLAMA_MODEL is not configured")
        started = monotonic()
        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.url}/api/generate", json=payload) as response:
                    if response.status != 200:
                        raise OllamaUnavailable(f"Ollama HTTP {response.status}")
                    data = await response.json()
            result = data.get("response")
            if not isinstance(result, str):
                raise OllamaUnavailable("Ollama response has no text")
            self.last_error = None
            self.failure_count = 0
            return result
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.last_error = str(exc)
            self.failure_count += 1
            raise OllamaUnavailable(str(exc)) from exc
        finally:
            self.last_latency_ms = round((monotonic() - started) * 1000)
            self._latencies.append(self.last_latency_ms)

    async def health(self) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=min(self.timeout, 5.0))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.url}/api/tags") as response:
                    return response.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
