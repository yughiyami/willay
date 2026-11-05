"""OGG/Opus voice note -> 16 kHz mono 16-bit WAV (the only format Voice Transcribe accepts)."""

import io
import shutil
import subprocess
import wave

import pytest

from willay.adapters.audio_convert import AudioConversionError, FfmpegAudioConverter, find_ffmpeg

FFMPEG = find_ffmpeg()
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg available")


def tone_wav(seconds=0.6, rate=48000) -> bytes:
    import math
    import struct

    frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * n / rate))) for n in range(int(seconds * rate)))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(frames)
    return buffer.getvalue()


def encode_opus(wav: bytes) -> bytes | None:
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "wav", "-i", "pipe:0", "-c:a", "libopus", "-f", "ogg", "pipe:1"],
        input=wav, capture_output=True, timeout=30,
    )
    return result.stdout if result.returncode == 0 and result.stdout else None


@needs_ffmpeg
def test_an_ogg_opus_voice_note_becomes_a_16k_mono_pcm_wav():
    ogg = encode_opus(tone_wav())
    if ogg is None:
        pytest.skip("this ffmpeg build cannot encode Opus (decoding is what production needs)")
    wav = FfmpegAudioConverter(FFMPEG).to_wav(ogg, "audio/ogg; codecs=opus")
    with wave.open(io.BytesIO(wav)) as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, 16000)
        assert reader.getnframes() > 16000 * 0.3


@needs_ffmpeg
def test_garbage_input_raises_a_conversion_error():
    with pytest.raises(AudioConversionError):
        FfmpegAudioConverter(FFMPEG).to_wav(b"not audio at all", "audio/ogg")


def test_a_missing_binary_is_a_conversion_error():
    with pytest.raises(AudioConversionError):
        FfmpegAudioConverter("definitely-not-an-ffmpeg-binary").to_wav(b"x", "audio/ogg")


def test_find_ffmpeg_returns_an_existing_path_or_none():
    found = find_ffmpeg()
    assert found is None or shutil.which(found) or __import__("os").path.exists(found)
