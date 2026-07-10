# Dependencies-only image. Bot source (main.py, config.py, cogs/) is bind-
# mounted at runtime via docker-compose, NOT baked in here — that means
# editing code on the NAS + restarting the container picks up changes
# immediately, no rebuild needed. Only rebuild this image if requirements.txt
# changes.

FROM python:3.13-slim

# tzdata is required for zoneinfo (used for the midnight-Pacific waiver
# check) — python:slim images don't include it by default, unlike full
# Debian/Ubuntu images. Without this you'd hit the same
# ZoneInfoNotFoundError you saw on Windows, just for a different reason
# (missing OS tz database instead of missing tzdata package).
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Forces stdout/stderr to flush immediately instead of block-buffering,
# which is Python's default when not attached to a real terminal (as is
# the case inside a container). Without this, print() statements can sit
# in a buffer indefinitely and never show up in `docker compose logs`,
# even though the program itself is running fine.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# No COPY of source code — see bind mount in docker-compose.yml
CMD ["python", "main.py"]