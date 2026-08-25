# syntax=docker/dockerfile:1

# --- Base: dependencies only, so a source edit never reinstalls them ---------
# 3.12 is what `requires-python`, the ruff target and the developers' virtualenvs
# all say, so it is what the image runs: shipping a runtime the suite never
# executes is how a version-specific bug reaches production first. Moving to 3.13
# means moving all four together.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Pinned *and* hashed: two builds of the same commit install byte-identical
# dependencies, and pip refuses anything whose artefact does not match the
# recorded hash — a version number alone would still accept whatever the index
# serves under that name today. Copied on its own so this layer stays cached
# until the lockfile changes.
COPY requirements.lock ./
RUN pip install --no-deps --require-hashes -r requirements.lock

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
# This stage is last, and Docker builds the last stage when none is named — so a
# `build: .` with no `target:` gets pytest, ruff and tests/ in the image. It has
# to be last (it extends `base`), which is why every production service names
# `target: base` explicitly. Check that before adding a service.
FROM base AS dev
USER root
RUN pip install "pytest>=8.3" "ruff>=0.15" "mypy>=1.14" "types-defusedxml>=0.7"
COPY tests ./tests
RUN chown -R votations:votations /app
USER votations
