"""TP-Link Deco ローカル API クライアント。FastAPI 層には依存しない。"""

from deco.client import DecoClient
from deco.exceptions import DecoAuthError, DecoConnectionError, DecoError

__all__ = ["DecoAuthError", "DecoClient", "DecoConnectionError", "DecoError"]
