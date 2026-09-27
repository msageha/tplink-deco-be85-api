from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api import DecoService, router
from deco import DecoAuthError, DecoClient, DecoConnectionError, DecoError
from settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    service = DecoService(
        DecoClient(
            settings.deco_host,
            settings.password,
            account=settings.account,
            verify_ssl=settings.verify_ssl,
            timeout=settings.timeout,
        )
    )
    app.state.deco = service
    try:
        yield
    finally:
        await service.run(service.client.logout)


app = FastAPI(
    title="Deco BE85 API",
    version="0.1.0",
    description="TP-Link Deco BE85 のローカル API をラップした監視・操作 API。",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(DecoError)
async def deco_error_handler(_: Request, exc: DecoError) -> JSONResponse:
    if isinstance(exc, DecoAuthError):
        return JSONResponse(status_code=401, content={"detail": str(exc)})
    if isinstance(exc, DecoConnectionError):
        return JSONResponse(status_code=504, content={"detail": str(exc)})
    return JSONResponse(
        status_code=502, content={"detail": str(exc), "error_code": exc.error_code}
    )


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": "deco-be85-api", "docs": "/docs"}
