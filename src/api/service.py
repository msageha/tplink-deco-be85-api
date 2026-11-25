import asyncio
from collections.abc import Callable


class DecoService[C]:
    """
    同期のクライアント (DecoClient・TmpClient) を event loop から使うためのラッパー。

    クライアントはセッション状態を持ち並行利用できないので、呼び出しを lock で直列化し
    worker thread で実行する。lock は呼び出し元の cancel ではなく worker の完了で放す。
    """

    def __init__(self, client: C) -> None:
        self.client = client
        self._lock = asyncio.Lock()
        # lock を待っている呼び出しも数える。asyncio.Lock は解放から次の待機者が取得するまで
        # locked() が False になるため。
        self._calls = 0

    @property
    def busy(self) -> bool:
        """呼び出しを実行中か、実行を待つ呼び出しがあるか。"""
        return self._calls > 0

    async def run[**P, T](
        self, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs
    ) -> T:
        self._calls += 1
        try:
            async with self._lock:
                worker = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # 呼び出し元が cancel されても worker thread は止まらないので、終わるまで lock を
                    # 持ったまま待ち、次の呼び出しとクライアントのセッションを同時に使わせない。
                    await asyncio.wait({worker})
                    raise
        finally:
            self._calls -= 1
