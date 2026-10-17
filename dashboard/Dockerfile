# Propagation Dashboard — Dockerfile
# Built on GitHub by .github/workflows/build-images.yml and published to
# ghcr.io; Portainer on n4misrv only pulls the finished image.
# Standard library only -- no pip install needed.

FROM python:3.13-slim

WORKDIR /app

COPY dashboard_server.py N4MI_PropagationDashboard.html ./

ENV PYTHONUNBUFFERED=1

EXPOSE 8073

# Ctrl+C-style stop so the server shuts down cleanly on "docker stop"
STOPSIGNAL SIGINT

CMD ["python", "dashboard_server.py"]
