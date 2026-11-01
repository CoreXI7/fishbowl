# Fishbowl's dashboard as a ready-made image (ghcr.io/corexi7/fishbowl). The optional collector isn't in it: it runs
# as root on the host (sudo bash collector/install.sh). Run it with the hardening in docker-compose.yml.
# Same base as before, pinned by digest so a rebuild never changes it silently (Dependabot proposes updates).
FROM python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8
ENV PYTHONDONTWRITEBYTECODE=1
# Owned by root, so the app (uid 1000) can't change its own code
COPY app/ /app/
COPY config.example.toml /config.example.toml
USER 1000:1000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)"]
CMD ["python", "-u", "/app/app.py"]
