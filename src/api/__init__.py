"""FastAPI 層: routes / models / service。"""

from api.routes import router
from api.service import DecoService

__all__ = ["DecoService", "router"]
