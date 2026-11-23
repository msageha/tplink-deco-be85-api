class DecoError(Exception):
    """ルーターとの通信・応答の失敗。error_code はルーターが返した値 (無ければ None)。"""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class DecoAuthError(DecoError):
    """ログイン失敗。"""


class DecoConnectionError(DecoError):
    """ルーターへ到達できない (タイムアウト・接続拒否など)。"""


class DecoRequestTooLargeError(DecoError):
    """要求がルーターのプロトコルで送れる大きさを超える。"""


class DecoHostKeyError(DecoError):
    """接続先の SSH の host key が、設定で信頼した鍵と一致しない。"""
