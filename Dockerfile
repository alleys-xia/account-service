# Build only from the parent workspace containing the sibling source repositories.
FROM python:3.11-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY account-service/packages/oms-protocol/pyproject.toml /app/account-service/packages/oms-protocol/pyproject.toml
COPY account-service/packages/oms-protocol/src /app/account-service/packages/oms-protocol/src
COPY account-service/packages/oms-client/pyproject.toml /app/account-service/packages/oms-client/pyproject.toml
COPY account-service/packages/oms-client/src /app/account-service/packages/oms-client/src
COPY account-service/packages/portfolio-client/pyproject.toml /app/account-service/packages/portfolio-client/pyproject.toml
COPY account-service/packages/portfolio-client/src /app/account-service/packages/portfolio-client/src
COPY account-service/pyproject.toml /app/account-service/pyproject.toml
COPY account-service/src /app/account-service/src
COPY account-service/uv.lock /app/account-service/uv.lock
WORKDIR /app/account-service
RUN UV_CACHE_DIR=/tmp/uv-cache uv sync --locked --no-dev --no-editable && rm -rf /tmp/uv-cache

FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 trading \
    && useradd --uid 10001 --gid 10001 --home-dir /home/trading --no-create-home trading \
    && mkdir -p /home/trading && chown 10001:10001 /home/trading
COPY --from=build /app /app
ENV PATH="/app/account-service/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Shanghai
WORKDIR /app/account-service
USER 10001:10001
ENTRYPOINT ["python", "-m", "account_runtime.app"]
CMD ["--oms", "/config/oms.toml", "--risk", "/config/risk.toml", "--portfolio", "/config/portfolio.toml"]
