FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DENO_INSTALL=/usr/local
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates fonts-inter fonts-dejavu-core fonts-lohit-guru fonts-lohit-deva \
    && curl -fsSL https://deno.land/install.sh | sh -s -- -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Cloudflare WARP as a local proxy for YouTube (used when YT_WARP=1, see app/warp.py). If a download fails the
# build still goes on; the Health page then says the tools are missing.
RUN (curl -fsSL -o /usr/local/bin/wgcf https://github.com/ViRb3/wgcf/releases/download/v2.2.22/wgcf_2.2.22_linux_amd64 \
      && chmod +x /usr/local/bin/wgcf) || echo "wgcf download failed"; \
    (curl -fsSL https://github.com/whyvl/wireproxy/releases/latest/download/wireproxy_linux_amd64.tar.gz | tar -xz -C /usr/local/bin wireproxy \
      && chmod +x /usr/local/bin/wireproxy) || echo "wireproxy download failed"

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Bundle the LiveKit browser SDK so users don't depend on a CDN at runtime
RUN mkdir -p web/vendor && curl -fsSL https://cdn.jsdelivr.net/npm/livekit-client@2.23.0/dist/livekit-client.esm.mjs -o web/vendor/livekit-client.esm.mjs || true

# Railway sets PORT. Mount a volume at /data to keep logs, playlists and the audio cache.
ENV DATA_DIR=/data
# yt-dlp is updated on every start (YouTube changes often), but never for more than 40 s: a slow PyPI used to
# hold the start past Railway's health check. "exec": Python takes the shell's place, so it gets Railway's stop
# signal and saves the queue and the song before a redeploy (under sh it was killed without a chance to).
CMD ["sh", "-c", "timeout 40 pip install -U --quiet --disable-pip-version-check --root-user-action=ignore yt-dlp[default] || true; exec python -m app.main"]
