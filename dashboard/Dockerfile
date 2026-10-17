# Propagation Dashboard — Dockerfile
# Built by Portainer's "Repository" stack deployment method directly from
# github.com/N4MI73/n4mi-shack-server, on the n4misrv environment.
# Standard library only -- no pip install needed.

FROM python:3.13-slim

WORKDIR /app

COPY dashboard_server.py N4MI_PropagationDashboard.html ./

ENV PYTHONUNBUFFERED=1

EXPOSE 8073

# Ctrl+C-style stop so the server shuts down cleanly on "docker stop"
STOPSIGNAL SIGINT

CMD ["python", "dashboard_server.py"]
