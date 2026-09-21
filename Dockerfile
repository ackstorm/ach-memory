FROM python:3.12-alpine AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --target=/app/deps -r requirements.txt

FROM python:3.12-alpine
ARG GIT_SHA
WORKDIR /app
ENV PYTHONPATH=/app/deps:/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=builder /app/deps /app/deps
COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini
# The npm bundle served at /plugin, packed by `make plugin-tarball` before the build.
# Packing outside the image is what keeps it honest: `npm pack` sees the real working
# tree, so the bundle can never drift from a hand-written list of COPY paths.
COPY dist/ach-memory.tgz /app/plugin.tgz
# Non-root, numeric uid/gid so `runAsNonRoot: true` can verify it without running the image.
RUN addgroup -g 10001 -S app \
    && adduser -S -D -u 10001 -G app -h /home/app -s /sbin/nologin app
USER 10001
LABEL org.opencontainers.image.title="ach-memory" \
      org.opencontainers.image.description="Governed, engine-neutral memory harness for coding agents" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ackstorm/ach-memory" \
      org.opencontainers.image.revision="${GIT_SHA:-unknown}"
EXPOSE 8000
CMD ["python", "-m", "memory.mcp.server"]
