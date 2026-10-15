# open-tv-controller (the orman-tv-controller command): linux/amd64 and linux/arm64 (Raspberry Pi 4/5).
# Bonjour and the TV scan need the home network itself: run it with --network host.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ORMAN_TVCTL_HOME=/data ORMAN_TVCTL_PORT=8820

WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && rm -rf /src

RUN useradd --system --uid 10001 --home-dir /data tvctl && mkdir -p /data && chown tvctl /data
USER tvctl
VOLUME /data
EXPOSE 8820
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"ORMAN_TVCTL_PORT\"]}/healthz',timeout=2)"
ENTRYPOINT ["orman-tv-controller"]
CMD ["serve"]
