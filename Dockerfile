# Slim single-stage image. No browser automation in the default build, so it
# stays under 200MB and boots in well under a second -- which matters on free
# tiers that cold-start containers.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY fixtures/ ./fixtures/
COPY scripts/ ./scripts/

# The cache lives here. Mount a volume at /app/data to make it survive
# redeploys; without one the service simply refills it, more slowly.
RUN mkdir -p /app/data

# Run unprivileged.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Most PaaS hosts inject $PORT. Default to 8000 for plain `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
