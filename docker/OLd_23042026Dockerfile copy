FROM python:3.11-slim

RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# Copy requirements from backend explicitly
COPY backend/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy full backend code
COPY backend/ .

RUN mkdir -p uploads && chown -R appuser:appuser /app

USER appuser

CMD ["gunicorn",
     "--workers", "2",
     "--bind", "0.0.0.0:5000",
     "--timeout", "120",
     "--access-logfile", "-",
     "--error-logfile", "-",
     "--log-level", "info",
     "app:app"]

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1