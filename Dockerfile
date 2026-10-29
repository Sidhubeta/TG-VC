FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DENO_INSTALL=/usr/local
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates fonts-inter fonts-dejavu-core \
    && curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Bundle the LiveKit browser SDK so users don't depend on a CDN at runtime
RUN mkdir -p web/vendor && curl -fsSL https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.esm.mjs -o web/vendor/livekit-client.esm.mjs || true

# Railway sets PORT. Mount a volume at /data to keep logs, playlists and the audio cache.
ENV DATA_DIR=/data
CMD ["sh", "-c", "pip install -U --quiet yt-dlp[default] || true; python -m app.main"]
