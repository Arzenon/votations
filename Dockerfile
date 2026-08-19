# syntax=docker/dockerfile:1

# --- Base: dependencies only, so a source edit never reinstalls them ---------
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Pinned, hash-free lockfile: two builds of the same commit install the same
# code. Copied on its own so this layer is cached until the lockfile changes.
COPY requirements.lock ./
RUN pip install --no-deps -r requirements.lock

# The application itself, in the layer that actually changes.
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY docker ./docker
RUN pip install --no-deps -e . && chmod +x docker/*.sh

# Nothing here needs to write to the image, and nothing needs root.
RUN useradd --system --create-home --uid 10001 votations \
    && chown -R votations:votations /app
USER votations

EXPOSE 8000

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Dev: adds the test tooling and the suite, for `docker compose` locally ---
FROM base AS dev
USER root
RUN pip install "pytest>=8.3" "ruff>=0.15"
COPY tests ./tests
RUN chown -R votations:votations /app
USER votations
