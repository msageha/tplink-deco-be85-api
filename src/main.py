from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from api import DecoService, router
from deco import DecoAuthError, DecoClient, DecoConnectionError, DecoError
from settings import settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    service = DecoService(
        DecoClient(
            settings.deco_host,
            password=settings.password,
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


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    # 既定の応答は各エラーに入力値を付け、Wi-Fi のパスワードなどを平文で返してしまうので除く。
    errors = [
        {key: value for key, value in error.items() if key != "input"}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.exception_handler(ValidationError)
async def router_data_error_handler(_: Request, __: ValidationError) -> JSONResponse:
    # endpoint の中で検証するモデルはルーターの応答のモデルなので、失敗はルーターの不正な応答。
    # 例外の文字列には応答の値が入るので、detail には載せない。
    return JSONResponse(
        status_code=502,
        content={"detail": "Deco returned an unexpected response", "error_code": None},
    )


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
