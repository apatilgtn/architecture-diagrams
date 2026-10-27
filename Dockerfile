# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels ".[cloud]"

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends graphviz fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --home-dir /app app \
 && mkdir -p /data/output /data/input && chown -R app /data
COPY --from=build /wheels /wheels
# Install only from the wheels built above, so the image contains exactly what was resolved at build time.
RUN pip install --no-cache-dir --no-index --find-links /wheels "arch-diagram-mcp[cloud]" && rm -rf /wheels

ENV ARCH_DIAGRAM_OUTPUT_DIR=/data/output \
    ARCH_DIAGRAM_INPUT_ROOTS=/data/input \
    ARCH_DIAGRAM_HTTP_HOST=0.0.0.0 \
    ARCH_DIAGRAM_HTTP_PORT=8000 \
    ARCH_DIAGRAM_RETENTION_DAYS=7 \
    PYTHONUNBUFFERED=1
USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
# ARCH_DIAGRAM_AUTH_TOKEN must be supplied at runtime; the server refuses 0.0.0.0 without it.
ENTRYPOINT ["arch-diagram-mcp", "--transport", "http"]
