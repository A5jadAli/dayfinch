FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACKER_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY api ./api
COPY ui ./ui
RUN pip install --no-cache-dir ".[s3]"

RUN useradd --create-home --uid 10001 tracker && \
    mkdir -p /data && chown -R tracker:tracker /data
USER tracker

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
