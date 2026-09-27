"""TP-Link Deco ローカル API クライアント。FastAPI 層には依存しない。"""

from .client import DecoClient
from .exceptions import DecoAuthError, DecoConnectionError, DecoError

__all__ = ["DecoAuthError", "DecoClient", "DecoConnectionError", "DecoError"]
