"""Standalone client for the TP-Link Deco local (luci) API.

This subpackage is independent of the FastAPI layer and can be used on its own.
"""

from .client import DecoClient, decode_name
from .crypto import DecoEncryption
from .exceptions import DecoAuthError, DecoConnectionError, DecoError

__all__ = [
    "DecoClient",
    "DecoEncryption",
    "DecoError",
    "DecoAuthError",
    "DecoConnectionError",
    "decode_name",
]
