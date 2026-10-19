# No system GDAL needed: the rasterio wheel bundles its own
FROM python:3.12-slim AS base

# The rasterio wheel bundles GDAL but links to the system libexpat, which slim omits
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/
# Use the base image's Python 3.12, matching .python-version
# Compile .pyc at build time so Lambda cold starts skip it
ENV UV_PROJECT_ENVIRONMENT=/code/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1
WORKDIR /code

# Dependencies first so code changes don't rebuild this layer.
COPY pyproject.toml uv.lock .python-version README.md ./
# Cache mount keeps uv's download cache out of the image while speeding up rebuilds
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project --no-dev

COPY ghrsst ghrsst
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
ENV PATH="/code/.venv/bin:$PATH"

# Test stage: adds dev deps on top of the production layer
FROM base AS test
COPY tests tests
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --group dev
CMD ["pytest", "tests/", "-v"]

# Final stage = base, i.e. the Lambda image with no dev deps
FROM base AS final
RUN python -c "import ghrsst.cogger, ghrsst.dategen, ghrsst.create_parquet"
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["ghrsst.cogger.lambda_handler"]
