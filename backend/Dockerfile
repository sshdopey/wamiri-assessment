FROM python:3.13-slim

WORKDIR /app

# Install system dependencies (libpq for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency manifest
COPY pyproject.toml uv.lock* ./

# Install dependencies
RUN uv sync --no-dev --compile-bytecode

# Copy source
COPY src/ src/
COPY configs/ configs/

# Create data directories
RUN mkdir -p data/parquet data/json data/metrics uploads

# Expose API port
EXPOSE 8000

# Default: run API server
CMD ["uv", "run", "uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
