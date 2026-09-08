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
#
# Copied, not installed. `pip install -e .` used to run here, and it was the one
# step in this build that reached the network unpinned: pip's default build
# isolation fetches a `setuptools` from PyPI to run the backend declared in
# pyproject.toml, with no version and no hash — in a build whose whole claim,
# stated above, is that the same commit produces the same image.
#
# Nothing needed the install. Every process in this image starts from WORKDIR
# /app and puts it on `sys.path` itself: uvicorn inserts its `--app-dir`
# (defaulting to the working directory), `alembic.ini` sets `prepend_sys_path =
# .`, and `python -m scripts.…` — which is how all three job containers run —
# prepends the working directory the way `-m` always does.
#
# The one thing that goes away with it: `import app` no longer works from an
# arbitrary directory inside the container. `docker compose exec` starts in
# WORKDIR, so it does not come up in practice; `cd /app` first if it ever does.
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts
COPY docker ./docker
RUN chmod +x docker/*.sh

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
# Read out of pyproject.toml's `dev` extra rather than written again here. The
# two lists were identical and had no way to stay so: the one that drifts is this
# one, and what it drifts into is an image whose mypy silently stops checking a
# module nobody notices. `pip install -e ".[dev]"` would be the obvious way to
# say it and is the one thing this build must not do — see the note above about
# build isolation reaching the network unpinned.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['dev']))" > /tmp/dev.txt \
    && pip install -r /tmp/dev.txt \
    && rm /tmp/dev.txt
COPY tests ./tests
RUN chown -R votations:votations /app
USER votations
