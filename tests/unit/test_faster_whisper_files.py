"""faster-whisper must be able to open an audio FILE (SPL-108). faster-whisper 1.2.1 calls `av.open(...,
metadata_errors=...)`, which av 19 does not accept, so `WhisperModel.transcribe("file.ogg")` raised a TypeError. This
app passes numpy arrays and was never affected, but the `transcribe` extra now pins `av<19` so that the file path
works for anyone using the model directly. Skipped without the extra (the default CI install)."""

from pathlib import Path

import pytest

pytest.importorskip("faster_whisper", reason="needs the transcribe extra (uv sync --extra transcribe)")

CLIP = Path(__file__).resolve().parents[1] / "fixtures" / "speech_clip.ogg"


def test_faster_whisper_decodes_an_audio_file_path():
    from faster_whisper.audio import decode_audio

    audio = decode_audio(str(CLIP), sampling_rate=16000)
    assert audio.dtype.name == "float32" and 11.0 < len(audio) / 16000 < 11.6


def test_av_is_below_19_with_the_extra():
    import av

    assert int(av.__version__.split(".")[0]) < 19
