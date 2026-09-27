import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from deco import DecoClient

P = ParamSpec("P")
T = TypeVar("T")


class DecoService:
    """同期の DecoClient を event loop から使うためのラッパー。

    DecoClient はセッション状態を持ち並行利用できないので、呼び出しを lock で直列化し
    worker thread で実行する。
    """

    def __init__(self, client: DecoClient) -> None:
        self.client = client
        self._lock = asyncio.Lock()

    async def run(self, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        async with self._lock:
            return await asyncio.to_thread(fn, *args, **kwargs)
