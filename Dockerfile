FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
# Install from the lock file, never from a hand-copied dependency list:
# a second list in the Dockerfile drifts from pyproject.toml silently.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt \
    && uv pip install --target=/app/deps -r requirements.txt

FROM python:3.12-slim
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
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "memory.api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
