FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git python3 python3-pip python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# bgutil PO-token provider (HTTP server on 127.0.0.1:4416)
RUN git clone --single-branch --branch 2.0.0 --depth 1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /pot \
    && cd /pot/server && npm ci && npx tsc

# python app deps
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
       flask gunicorn "edge-tts==7.2.8" "yt-dlp>=2025.9.0" \
       imageio-ffmpeg requests bgutil-ytdlp-pot-provider

WORKDIR /srv
COPY app.py start.sh ./
RUN chmod +x start.sh
CMD ["./start.sh"]
