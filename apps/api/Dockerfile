FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY apps/api /app/apps/api
COPY deploy/run-managed-service.sh /usr/local/bin/run-managed-service

RUN pip install --upgrade pip \
    && pip install ".[managed]" \
    && chmod +x /usr/local/bin/run-managed-service

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
