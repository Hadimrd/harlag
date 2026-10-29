FROM node:22-bookworm-slim AS pot-provider-build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git python3 make g++ pkg-config libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider
RUN cd /opt/bgutil-ytdlp-pot-provider/server && npm ci && npx tsc && npm prune --omit=dev

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates libstdc++6 libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    libjpeg62-turbo libgif7 librsvg2-2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=pot-provider-build /usr/local/bin/node /usr/local/bin/node
COPY --from=pot-provider-build /opt/bgutil-ytdlp-pot-provider/server /opt/bgutil-ytdlp-pot-provider/server
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=10000
CMD ["sh", "-c", "node /opt/bgutil-ytdlp-pot-provider/server/build/main.js -H 127.0.0.1 >/tmp/bgutil-pot.log 2>&1 & sleep 1; uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000}"]
