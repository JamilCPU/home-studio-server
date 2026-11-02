FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY homestudio_mcp ./homestudio_mcp
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home homestudio
USER 10001

EXPOSE 8000
CMD ["homestudio-mcp"]
