FROM ghcr.io/astral-sh/uv:0.12.10 AS uv
FROM python:3.13-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv export --frozen --all-extras --no-dev --no-emit-project -o requirements.txt \
    && uv build --wheel

FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/requirements.txt /tmp/requirements.txt
COPY --from=builder /build/dist/*.whl /tmp/wheels/
RUN python -m pip install --require-hashes -r /tmp/requirements.txt \
    && python -m pip install --no-deps /tmp/wheels/*.whl \
    && python -c "from kilntainers.fly_runtime import install_flyctl; install_flyctl()" \
    && ln -s /root/.fly/bin/flyctl /usr/local/bin/fly \
    && ln -s /root/.fly/bin/flyctl /usr/local/bin/flyctl
WORKDIR /app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8080/healthz',headers={'Authorization':'Bearer '+os.environ.get('KILNTAINERS_AUTH_TOKEN','')}); urllib.request.urlopen(r,timeout=3)"
ENTRYPOINT ["kilntainers"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8080", "--backend", "docker"]
