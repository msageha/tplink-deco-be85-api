import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from config import Settings
from deco import DecoClient

T = TypeVar("T")


class DecoService:
    """Async-friendly wrapper around the synchronous DecoClient.

    The client mutates session state (stok / sysauth / AES key) and is not
    safe for concurrent use, so calls are serialized behind a lock and run in
    a worker thread to keep the event loop responsive.
    """

    def __init__(self, settings: Settings) -> None:
        self.client = DecoClient(
            settings.deco_host,
            settings.password,
            account=settings.account,
            verify_ssl=settings.verify_ssl,
            timeout=settings.timeout,
        )
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    async def close(self) -> None:
        if self.client.logged_in:
            await self.run(self.client.logout)
