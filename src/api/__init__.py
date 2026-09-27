"""FastAPI 層: routes / models / service。"""

from .routes import router
from .service import DecoService

__all__ = ["DecoService", "router"]
