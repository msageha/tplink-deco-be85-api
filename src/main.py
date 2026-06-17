from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import router
from api.service import DecoService
from config import get_settings
from deco import DecoAuthError, DecoConnectionError, DecoError


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.deco = DecoService(settings)
    try:
        yield
    finally:
        await app.state.deco.close()


app = FastAPI(
    title="Deco BE85 API",
    version="0.1.0",
    description="Local control/monitoring API for the TP-Link Deco BE85 mesh router.",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(DecoAuthError)
async def _auth_handler(_: Request, exc: DecoAuthError) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": exc.message})


@app.exception_handler(DecoConnectionError)
async def _conn_handler(_: Request, exc: DecoConnectionError) -> JSONResponse:
    return JSONResponse(status_code=504, content={"detail": exc.message})


@app.exception_handler(DecoError)
async def _deco_handler(_: Request, exc: DecoError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": exc.message, "error_code": exc.error_code},
    )


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": "deco-be85-api", "docs": "/docs"}
