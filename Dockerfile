FROM python:3.12-slim

WORKDIR /app

# git: the Semgrep discovery source clones the target repository.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY app/ ./app/
COPY alembic.ini ./
COPY alembic/ ./alembic/
RUN uv sync --locked --no-dev --no-editable

EXPOSE 8000

# Default: the API service. Other deployables override the command
# (see docker-compose.yml / k8s/): celery worker -Q ingest | -Q devin | beat.
# Migrations run as a separate one-shot `alembic upgrade head` container/job.
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
