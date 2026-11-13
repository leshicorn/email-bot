# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# tzdata keeps ZoneInfo("Europe/Moscow") working in a slim image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py config.py ./
COPY bot ./bot
COPY fetcher ./fetcher
COPY scheduler ./scheduler
COPY storage ./storage
COPY summarizer ./summarizer

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

VOLUME ["/app/data"]

# The heartbeat file is rewritten after every digest run.
# The heartbeat is written at startup and after every digest run, so a stale
# file means the scheduler stopped working even if the process is still up.
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m --retries=3 \
    CMD python -c "import os,sys,time; p='/app/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 172800 else 1)"

CMD ["python", "main.py"]
