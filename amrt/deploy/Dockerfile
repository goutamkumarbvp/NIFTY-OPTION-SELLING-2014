# AI Market Risk Terminal — one image runs the API/dashboard/loops; the same image runs the external watchdog.
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 amrt
WORKDIR /app
COPY requirements.txt requirements-brokers.txt ./
RUN pip install -r requirements.txt -r requirements-brokers.txt
COPY amrt ./amrt
COPY frontend/out ./frontend/out
ENV AMRT_RUNTIME_DIR=/data AMRT_STATIC_DIR=/app/frontend/out AMRT_BIND_HOST=0.0.0.0 AMRT_PORT=8700 AMRT_LOG_FORMAT=json
RUN mkdir -p /data && chown amrt:amrt /data
USER amrt
EXPOSE 8700
HEALTHCHECK --interval=10s --timeout=3s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/api/health/live',timeout=2).status==200 else 1)"
CMD ["python", "-m", "amrt"]
