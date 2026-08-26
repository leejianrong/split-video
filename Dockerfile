# Fetches the YAMNet model in its own stage so `kagglehub` (only needed to
# resolve Kaggle's signed, short-lived download URLs) never ends up in the
# final image — see scripts/fetch_yamnet_model.py.
FROM python:3.12-slim AS models
RUN pip install --no-cache-dir kagglehub
COPY scripts/fetch_yamnet_model.py ./
RUN python fetch_yamnet_model.py /models

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=models /models ./src/split_video/editor/models
RUN uv sync --frozen --no-dev

WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["/app/.venv/bin/split-video"]
