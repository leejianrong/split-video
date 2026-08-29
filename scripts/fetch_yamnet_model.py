"""One-off setup script: download the YAMNet TFLite model and its class map.

Not a runtime dependency of the app — these are large-ish binary assets that
live outside git (see .gitignore) and get fetched once, either for local dev
via `make fetch-model` or as an isolated Docker build stage, so the published
image ships them without needing `kagglehub` itself at runtime.

Source: google/yamnet on Kaggle Models (the successor to the now-retired
TF Hub), converted from the Apache-2.0-licensed model at
https://github.com/tensorflow/models/tree/master/research/audioset/yamnet.
The class map is fetched straight from that same repo, since it's a stable
public URL and (unlike the TFLite model) doesn't need Kaggle's signed,
short-lived download links.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "src" / "split_video" / "editor" / "models"
CLASS_MAP_URL = (
    "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"
)
KAGGLE_MODEL_HANDLE = "google/yamnet/tfLite/classification-tflite"


def main() -> None:
    import kagglehub

    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded = Path(kagglehub.model_download(KAGGLE_MODEL_HANDLE))
    tflite_files = list(downloaded.glob("*.tflite"))
    if len(tflite_files) != 1:
        raise RuntimeError(f"expected exactly one .tflite file in {downloaded}, found {tflite_files}")
    (output_dir / "yamnet.tflite").write_bytes(tflite_files[0].read_bytes())

    urllib.request.urlretrieve(CLASS_MAP_URL, output_dir / "yamnet_class_map.csv")

    print(f"Wrote yamnet.tflite + yamnet_class_map.csv to {output_dir}")


if __name__ == "__main__":
    main()
