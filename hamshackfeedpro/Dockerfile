# HamShackFeed Pro — Dockerfile
# Built by Portainer's "Repository" stack deployment method directly from
# github.com/N4MI73/n4mi-shack-server, on the n4misrv environment.
# No local Docker build step is needed on either Windows computer.

FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py fetcher.py ./
COPY templates/ ./templates/

# Database lives in /data, which docker-compose.yml bind-mounts from
# /srv/docker/hamshackfeedpro/data on the host (picked up by the nightly backup).
ENV HSF_DB_PATH=/data/hamshackfeed.db \
    HSF_OPEN_BROWSER=0 \
    PYTHONUNBUFFERED=1

EXPOSE 8074

# Ctrl+C-style stop so the server shuts down cleanly on "docker stop"
STOPSIGNAL SIGINT

CMD ["python", "server.py"]
