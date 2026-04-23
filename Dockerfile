# ==============================================================================
# Multi-stage Dockerfile for CustomBot
#
# Stages:
#   1. builder  — install all deps (including dev) for optional test runs
#   2. runtime  — production image with only runtime deps
#
# Usage:
#   docker build -t custombot .
#   docker run -d \
#     -p 8080:8080 \
#     -v ./config.json:/app/config.json \
#     -v ./workspace:/app/workspace \
#     custombot
# ==============================================================================

# ---------------------------------------------------------------------------
# Stage 1: Builder — installs dependencies
# ---------------------------------------------------------------------------
FROM python:3.11.12-slim-bookworm AS builder

WORKDIR /build

# Install build dependencies for packages that need compilation
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Copy dependency manifests first for layer caching
COPY requirements.txt requirements-dev.txt* ./

# Install production dependencies into a clean prefix
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Install dev dependencies into a separate prefix (for optional test stage)
RUN if [ -f requirements-dev.txt ]; then \
      pip install --no-cache-dir --prefix=/install-dev \
        -r requirements.txt -r requirements-dev.txt; \
    fi

# ---------------------------------------------------------------------------
# Stage 2: Runtime — minimal production image
# ---------------------------------------------------------------------------
FROM python:3.11.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="CustomBot"
LABEL org.opencontainers.image.description="A lightweight WhatsApp AI assistant"
LABEL org.opencontainers.image.version="1.0.0"

# Create non-root user for security
RUN groupadd --gid 1000 custombot && \
    useradd --uid 1000 --gid custombot --create-home custombot

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ src/
COPY main.py .

# Create workspace directories with correct ownership
RUN mkdir -p workspace/logs/llm workspace/skills workspace/whatsapp_data && \
    chown -R custombot:custombot /app

# Switch to non-root user
USER custombot

# Default environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check — requires --health-port to be passed at runtime
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Default port for the health endpoint
EXPOSE 8080

# Persistent data volumes
VOLUME ["/app/workspace", "/app/config.json"]

ENTRYPOINT ["python", "main.py"]
CMD ["start", "--health-port", "8080"]
