# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.14

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
# package = false の virtual project なので依存だけが /app/.venv に入る。ソースは runner 側で COPY する。
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

FROM python:${PYTHON_VERSION}-slim-bookworm AS runner

LABEL org.opencontainers.image.source="https://github.com/msageha/tplink-deco-be85-api" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

# hadolint DL3066: 数値 UID にすると実行環境側が非 root を検証できる (useradd の --uid と同じ値)
USER 10001
EXPOSE 8000

# PASSWORD と DECO_HOST (必須) はイメージに含めず、実行時に環境変数で渡す。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
