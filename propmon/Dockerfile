# PropMon -- Dockerfile
# Built by GitHub Actions and published as ghcr.io/n4mi73/propmon
# (see .github/workflows/build-images.yml). Portainer only pulls the image.

FROM python:3.12-slim

WORKDIR /app

# Flask for the JSON endpoint, requests for the outbound HTTP calls to
# HamQSL/NOAA/Tempest/NWS, astral for real sunrise/sunset/civil-twilight
# calculation (160m/80m day-night detection -- pure Python, no system deps).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY propmon_server.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8076

# Python as PID 1 ignores SIGTERM; SIGINT gives a clean, fast stop.
STOPSIGNAL SIGINT

CMD ["python", "propmon_server.py"]
