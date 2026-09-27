# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.13

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
# package = false の virtual project なので依存だけが /app/.venv に入る。ソースは runner 側で COPY する。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

FROM python:${PYTHON_VERSION}-slim-bookworm AS runner

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    DECO_HOST=http://172.16.1.1

USER appuser
EXPOSE 8000

# 認証情報は実行時に渡す (--env-file .env または -e PASSWORD=...)。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
