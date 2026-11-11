# Runs Clipz on a server so it can be used from a phone. Needs ffmpeg 6.1+ (Debian trixie has 7).
FROM python:3.13-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY pyproject.toml ./
COPY clipz ./clipz
RUN pip install --no-cache-dir .
# Everything Clipz remembers (settings, sign-ins, videos) lives in /srv/data - attach a volume there.
VOLUME /srv/data
ENV PORT=8777
EXPOSE 8777
# CLIPZ_PASSWORD must be set; the app refuses to start without it.
CMD ["sh", "-c", "clipz app --host 0.0.0.0 --port $PORT --no-browser"]
