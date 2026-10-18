FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/backend \
    PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng libglib2.0-0 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements-lock-py313.txt /app/backend/requirements-lock-py313.txt
RUN python -m pip install --no-cache-dir -r /app/backend/requirements-lock-py313.txt
COPY backend /app/backend
COPY data/demo_docs /app/data/demo_docs
RUN useradd --system --uid 10001 --create-home ledgerlens \
    && mkdir -p /app/storage /app/quarantine \
    && chown -R ledgerlens:ledgerlens /app/backend /app/data /app/storage /app/quarantine

WORKDIR /app/backend
USER 10001:10001
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
