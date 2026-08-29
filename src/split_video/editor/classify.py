"""YAMNet-based coarse audio classification for the timeline's overlay.

Classifies each frame of a recording's audio into one of six coarse buckets
(music / singing / speech / applause-crowd / laughter / silence-other) using
Google's YAMNet, run via a locally bundled quantized TFLite model (see
scripts/fetch_yamnet_model.py for how `models/` gets populated). Consecutive
frames sharing a bucket are merged into contiguous regions before being
handed to the API layer.

The model and label loading (`get_model_and_labels`) is process-global and
lazy: it's expensive enough (a few dozen ms) to not want on every request,
but the whole feature is opt-in (an explicit "Analyze audio" action), so
nothing pays for it until that's actually clicked.
"""

from __future__ import annotations

import csv
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).parent / "models"
MODEL_PATH = MODELS_DIR / "yamnet.tflite"
CLASS_MAP_PATH = MODELS_DIR / "yamnet_class_map.csv"

SAMPLE_RATE = 16000
FRAME_SAMPLES = 15600  # YAMNet's native input width (~0.975s at 16kHz)
HOP_SAMPLES = 7680  # ~0.48s hop, half the frame

SILENCE_BUCKET = "silence_other"

# Maps our 6 coarse overlay buckets to exact AudioSet display names (see
# yamnet_class_map.csv, fetched from the same tensorflow/models source as
# the model). A few labels from the original design doc don't actually
# exist in AudioSet's 521-class ontology (likely misremembered) and were
# dropped/swapped for the closest real class: "Male/Female speech, ...
# speaking" (plain "Speech" already covers general speech), "Booing" (no
# close equivalent; Crowd/Cheering/Whoop already cover crowd reaction), and
# "Background noise" -> "Environmental noise" (the real class name).
BUCKET_LABELS: dict[str, list[str]] = {
    "music": [
        "Music",
        "Musical instrument",
        "Jazz",
        "Piano",
        "Electric piano",
        "Bass guitar",
        "Double bass",
        "Drum kit",
        "Drum",
        "Cymbal",
        "Brass instrument",
        "Wind instrument, woodwind instrument",
    ],
    "singing": ["Singing", "Choir", "A capella", "Vocal music"],
    "speech": ["Speech", "Narration, monologue", "Conversation"],
    "applause_crowd": ["Applause", "Clapping", "Cheering", "Crowd", "Whoop"],
    "laughter": ["Laughter", "Chuckle, chortle", "Giggle"],
    SILENCE_BUCKET: ["Silence", "Noise", "Environmental noise"],
}

DEFAULT_THRESHOLDS: dict[str, float] = {bucket: 0.3 for bucket in BUCKET_LABELS}


class ModelNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClassLabels:
    """The 521 AudioSet class display names, and which overlay bucket each belongs to."""

    display_names: list[str]
    bucket_indices: dict[str, list[int]]


def load_labels(path: Path = CLASS_MAP_PATH) -> ClassLabels:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    display_names = [""] * len(rows)
    for row in rows:
        display_names[int(row["index"])] = row["display_name"]

    name_to_index = {name: i for i, name in enumerate(display_names)}
    bucket_indices = {bucket: [name_to_index[name] for name in names] for bucket, names in BUCKET_LABELS.items()}
    return ClassLabels(display_names=display_names, bucket_indices=bucket_indices)


@dataclass(frozen=True)
class ClassificationRegion:
    start: float
    end: float
    bucket: str
    score: float


def bucket_scores_for_frame(scores: np.ndarray, labels: ClassLabels) -> dict[str, float]:
    """The max class score within each bucket, for one frame's raw 521-class scores."""
    return {bucket: float(scores[indices].max()) for bucket, indices in labels.bucket_indices.items()}


def winning_bucket(frame_bucket_scores: dict[str, float], thresholds: dict[str, float]) -> tuple[str, float]:
    """Pick the highest-scoring bucket that clears its threshold; fall back to Silence/Other."""
    best_bucket = None
    best_score = -1.0
    for bucket, score in frame_bucket_scores.items():
        if score >= thresholds.get(bucket, 0.0) and score > best_score:
            best_bucket, best_score = bucket, score
    if best_bucket is None:
        return SILENCE_BUCKET, frame_bucket_scores.get(SILENCE_BUCKET, 0.0)
    return best_bucket, best_score


def merge_regions(frame_buckets: list[tuple[float, float, str, float]]) -> list[ClassificationRegion]:
    """Merge consecutive, contiguous, same-bucket frames into regions.

    `frame_buckets` is `(frame_start, frame_end, bucket, score)` tuples, in
    time order. A merged region's score is the max of its frames' scores.
    Frames are only merged when contiguous (allowing float slop) — a gap
    between frames starts a new region even if the bucket matches, since a
    gap means something (silence, an unclassified span) happened in between.
    """
    regions: list[ClassificationRegion] = []
    for start, end, bucket, score in frame_buckets:
        if regions and regions[-1].bucket == bucket and abs(start - regions[-1].end) < 1e-6:
            prev = regions[-1]
            regions[-1] = ClassificationRegion(prev.start, end, bucket, max(prev.score, score))
        else:
            regions.append(ClassificationRegion(start, end, bucket, score))
    return regions


class YamnetModel:
    """Thin wrapper around the bundled TFLite interpreter.

    Not unit-tested directly — it needs the real (large-ish, gitignored)
    model file — see tests/integration/test_classify.py, which skips if the
    model hasn't been fetched.
    """

    def __init__(self, model_path: Path = MODEL_PATH) -> None:
        if not model_path.exists():
            raise ModelNotFoundError(
                f"YAMNet model not found at {model_path} — run `make fetch-model` "
                "(or rebuild the Docker image, which fetches it automatically)."
            )
        from ai_edge_litert.interpreter import Interpreter

        self._interpreter = Interpreter(model_path=str(model_path))
        self._interpreter.allocate_tensors()
        self._input_index = self._interpreter.get_input_details()[0]["index"]
        self._output_index = self._interpreter.get_output_details()[0]["index"]

    def classify_frame(self, frame: np.ndarray) -> np.ndarray:
        self._interpreter.set_tensor(self._input_index, frame)
        self._interpreter.invoke()
        return self._interpreter.get_tensor(self._output_index)[0]


_model: YamnetModel | None = None
_labels: ClassLabels | None = None
_model_lock = threading.Lock()


def get_model_and_labels() -> tuple[YamnetModel, ClassLabels]:
    global _model, _labels
    with _model_lock:
        if _model is None:
            _model = YamnetModel()
            _labels = load_labels()
    return _model, _labels


def _frame_starts(sample_count: int) -> list[int]:
    if sample_count == 0:
        return []
    return list(range(0, sample_count, HOP_SAMPLES))


def classify_audio(
    pcm_s16le: bytes,
    model: YamnetModel,
    labels: ClassLabels,
    thresholds: dict[str, float],
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[list[ClassificationRegion], list[dict[str, float]], float]:
    """Classify raw mono 16kHz PCM audio.

    Returns the merged regions, the per-frame bucket-max scores (cached by
    the caller so thresholds can be retuned later via `rethreshold` without
    rerunning inference), and the total duration in seconds.

    Region boundaries tile each frame's hop-sized slot rather than its full
    (overlapping) analysis window, so adjacent regions never overlap.
    """
    samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32) / 32768.0
    total_duration = samples.size / SAMPLE_RATE
    starts = _frame_starts(samples.size)

    frame_bucket_scores: list[dict[str, float]] = []
    frame_buckets: list[tuple[float, float, str, float]] = []
    for i, start in enumerate(starts):
        frame = samples[start : start + FRAME_SAMPLES]
        if frame.size < FRAME_SAMPLES:
            frame = np.pad(frame, (0, FRAME_SAMPLES - frame.size))
        scores = model.classify_frame(frame)
        scores_by_bucket = bucket_scores_for_frame(scores, labels)
        frame_bucket_scores.append(scores_by_bucket)

        bucket, score = winning_bucket(scores_by_bucket, thresholds)
        slot_start = start / SAMPLE_RATE
        slot_end = min((start + HOP_SAMPLES) / SAMPLE_RATE, total_duration)
        frame_buckets.append((slot_start, slot_end, bucket, score))

        if on_progress:
            on_progress(i + 1, len(starts))

    return merge_regions(frame_buckets), frame_bucket_scores, total_duration


def rethreshold(
    frame_bucket_scores: list[dict[str, float]],
    thresholds: dict[str, float],
    total_duration: float,
) -> list[ClassificationRegion]:
    """Recompute regions from already-cached per-frame bucket scores with
    new thresholds, without rerunning inference."""
    frame_buckets = []
    for i, scores_by_bucket in enumerate(frame_bucket_scores):
        bucket, score = winning_bucket(scores_by_bucket, thresholds)
        slot_start = i * HOP_SAMPLES / SAMPLE_RATE
        slot_end = min((i * HOP_SAMPLES + HOP_SAMPLES) / SAMPLE_RATE, total_duration)
        frame_buckets.append((slot_start, slot_end, bucket, score))
    return merge_regions(frame_buckets)
