FROM python:3.11-slim

# -----------------------------
# System deps (required for some libs)
# -----------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

# -----------------------------
# Security: non-root user
# -----------------------------
RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app

# -----------------------------
# Copy & validate requirements
# -----------------------------
COPY requirements.txt .

# 🔴 Fail fast if missing
RUN test -f requirements.txt || (echo "❌ requirements.txt missing" && exit 1)

# Install deps
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# -----------------------------
# Copy app
# -----------------------------
COPY . .

# -----------------------------
# Permissions
# -----------------------------
RUN mkdir -p uploads \
    && chown -R appuser:appuser /app

USER appuser

# -----------------------------
# Runtime
# -----------------------------
EXPOSE 5000

CMD ["gunicorn", \
     "--workers", "2", \
     "--bind", "0.0.0.0:5000", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "app:app"]

# -----------------------------
# Healthcheck
# -----------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1