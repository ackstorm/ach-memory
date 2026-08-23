# Digest-pinned, not just tag-pinned: this image is PUBLISHED PUBLICLY, so a
# tag that moves under us makes a released version unreproducible for everyone
# who pulled it. Same reasoning as hindsight-api==0.9.1 in Dockerfile.hindsight.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
# Install from the lock file, never from a hand-copied dependency list:
# a second list in the Dockerfile drifts from pyproject.toml silently.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --target=/app/deps -r requirements.txt

FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a
# Declared in this stage (not the builder) because it's only consumed by the
# LABEL below -- an ARG must be re-declared in every stage that reads it.
ARG GIT_SHA
WORKDIR /app
ENV PYTHONPATH=/app/deps:/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY --from=builder /app/deps /app/deps
COPY src/ /app/src/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini
# Non-root at rest: the service only ever reads /app (PYTHONDONTWRITEBYTECODE=1
# leaves no .pyc behind), so an unprivileged uid needs no ownership changes.
# Numeric on purpose -- a named USER makes the kubelet reject the pod under
# `runAsNonRoot: true`, which it cannot resolve to a uid without running it.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app \
    --shell /usr/sbin/nologin app
USER 10001
LABEL org.opencontainers.image.title="ach-memory" \
      org.opencontainers.image.description="Multi-tenant memory service for coding agents, over Hindsight" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/ackstorm/ach-memory" \
      org.opencontainers.image.revision="${GIT_SHA:-unknown}"
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "memory.api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
