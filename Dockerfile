# Fetches the YAMNet model in its own stage so `kagglehub` (only needed to
# resolve Kaggle's signed, short-lived download URLs) never ends up in the
# final image — see scripts/fetch_yamnet_model.py.
FROM python:3.12-slim AS models
RUN pip install --no-cache-dir kagglehub
COPY scripts/fetch_yamnet_model.py ./
RUN python fetch_yamnet_model.py /models

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Optional on-screen text (see docs/decisions/vision-and-ocr.md): `--build-arg WITH_OCR=1` adds the Tesseract
# program and its English data (about 65 MB more image). No Python package and no model download: the
# `ocr` / `ocr-search` commands (and the API and MCP equivalents) just call `tesseract`.
ARG WITH_OCR=0
RUN if [ "$WITH_OCR" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
        && rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=models /models ./src/split_video/editor/models

# Optional speech-to-text (see docs/decisions/transcription.md): `--build-arg WITH_TRANSCRIPTION=1` adds the
# `transcribe` extra (faster-whisper, about 340 MB more image). `--build-arg TRANSCRIPTION_MODEL=base.en` also bakes
# that model (139 MB) into /opt/split-video-models, which is only a fallback: without it the model is downloaded on
# first use into the mounted folder (.split-video-models/), never into the image or a home directory.
ARG WITH_TRANSCRIPTION=0
ARG TRANSCRIPTION_MODEL=
RUN if [ "$WITH_TRANSCRIPTION" = "1" ]; then uv sync --frozen --no-dev --extra transcribe; \
    else uv sync --frozen --no-dev; fi
RUN if [ -n "$TRANSCRIPTION_MODEL" ]; then \
        [ "$WITH_TRANSCRIPTION" = "1" ] || { echo "TRANSCRIPTION_MODEL needs --build-arg WITH_TRANSCRIPTION=1" >&2; exit 1; }; \
        HF_HOME=/opt/split-video-models HF_HUB_DISABLE_XET=1 /app/.venv/bin/python -c \
            "from faster_whisper.utils import download_model; download_model('$TRANSCRIPTION_MODEL', cache_dir='/opt/split-video-models')" \
        && chmod -R a+rX /opt/split-video-models; \
    fi
ENV SPLIT_VIDEO_MODEL_SEED=/opt/split-video-models

WORKDIR /data
EXPOSE 8765
ENTRYPOINT ["/app/.venv/bin/split-video"]
