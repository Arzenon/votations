# syntax=docker/dockerfile:1

# --- Base: dependencies first, so a code change doesn't reinstall them -------
# Same Python version as requires-python, ruff and mypy. `scripts/relock.sh`
# reads the version from the line below.
FROM python:3.14-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Pinned and hashed, for reproducible builds. Copied alone so the layer stays
# cached until the lockfile changes.
COPY requirements.lock ./
RUN pip install --no-deps --require-hashes -r requirements.lock

# The application. Copied rather than `pip install -e .`, which would fetch an
# unpinned setuptools. Everything runs from /app, which is on sys.path
# (uvicorn, alembic.ini, python -m), so `import app` needs to run from /app.
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY docker ./docker
RUN chmod +x docker/*.sh

# Doesn't need root.
RUN useradd --system --create-home --uid 10001 votations \
    && chown -R votations:votations /app
USER votations

EXPOSE 8000

ENTRYPOINT ["./docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# --- Dev: adds the test tools and the tests ---------------------------------
# Docker builds the last stage by default, so production services must set
# `target: base`.
FROM base AS dev
USER root
# Dev dependencies read from pyproject.toml's `dev` extra. Not
# `pip install -e ".[dev]"`, for the reason above.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev']))" > /tmp/dev.txt \
    && pip install -r /tmp/dev.txt \
    && rm /tmp/dev.txt
COPY tests ./tests
# Read by tests/test_config.py.
COPY .env.example docker-compose.prod.yml ./
RUN chown -R votations:votations /app
USER votations
