# Multi-stage Dockerfile for the Link Intelligence Platform
# Note: User requested NO Docker for the database. This Dockerfile is for
# the APPLICATION only — SQLite is a file and lives on the host volume.
# Redis is also expected to run on the host (or as a separate process),
# NOT inside this container.

FROM python:3.11-slim AS base

# System deps (some Python packages need build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create runtime directories
RUN mkdir -p /app/data /app/logs

# Expose web port
EXPOSE 8000

# Default command: run all workers + web together
# For production, override with: docker run ... python scripts/run_web.py
CMD ["python", "scripts/run_all.py"]
