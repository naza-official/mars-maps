# Single image for the API and the processing pipeline (modular monolith).
# GDAL and PROJ come from the rasterio/pyproj manylinux wheels (GDAL 3.12.2, PROJ 9.8.1);
# no system GDAL is installed, which avoids two GDAL/PROJ copies disagreeing.
# Multi-arch base image: native on Apple Silicon (arm64) and amd64.
FROM python:3.12-slim-trixie

# libexpat1: required by the rasterio 1.5 wheel but absent from the slim image
# (ImportError: libexpat.so.1 — verified during compatibility testing).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --root-user-action=ignore uv==0.13.7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR \
    GDAL_CACHEMAX=256

WORKDIR /app
COPY requirements.lock ./
RUN uv pip install --no-cache -r requirements.lock

COPY pyproject.toml ./
COPY src ./src
COPY tests ./tests
# Editable install so bind-mounted source changes are picked up in development.
RUN uv pip install --no-cache --no-deps -e .

RUN useradd --create-home --uid 1000 marsgis
USER marsgis

EXPOSE 8000
CMD ["uvicorn", "marsgis.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
