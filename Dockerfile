FROM ghcr.io/osgeo/gdal:ubuntu-small-3.12.3 AS base

RUN apt-get update && apt-get install -y \
    python3-dev git build-essential pkg-config \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /usr/local/bin/
# Use the base image's Python 3.12 (Ubuntu 24.04), matching .python-version
ENV UV_PROJECT_ENVIRONMENT=/code/.venv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /code

# Dependencies first so code changes don't rebuild this layer.
# rasterio builds from source against the image's GDAL (see no-binary-package).
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY ghrsst ghrsst
RUN uv sync --frozen --no-dev
ENV PATH="/code/.venv/bin:$PATH"

# Test stage: adds dev deps on top of the production layer
FROM base AS test
COPY tests tests
RUN uv sync --frozen --group dev
CMD ["pytest", "tests/", "-v"]

# Final stage = base, i.e. the Lambda image with no dev deps
FROM base AS final
RUN python -c "import ghrsst.cogger, ghrsst.dategen, ghrsst.create_parquet"
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["ghrsst.cogger.lambda_handler"]
