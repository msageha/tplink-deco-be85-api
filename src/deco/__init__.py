"""TP-Link Deco ローカル API クライアント。FastAPI 層には依存しない。"""

from deco.catalog import LUCI_OPERATIONS, TMP_OPERATIONS, LuciOperation, TmpOperation
from deco.client import DecoClient
from deco.exceptions import (
    DecoAuthError,
    DecoConnectionError,
    DecoError,
    DecoHostKeyError,
    DecoRequestTooLargeError,
)
from deco.tmp import TmpClient, TmpCredentials

__all__ = [
    "LUCI_OPERATIONS",
    "TMP_OPERATIONS",
    "DecoAuthError",
    "DecoClient",
    "DecoConnectionError",
    "DecoError",
    "DecoHostKeyError",
    "DecoRequestTooLargeError",
    "LuciOperation",
    "TmpClient",
    "TmpCredentials",
    "TmpOperation",
]
