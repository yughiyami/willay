"""Voice note (OGG/Opus from WhatsApp) -> 16 kHz mono 16-bit PCM WAV, the only format Voice Transcribe accepts.

Decoder order: PyAV (`av`, optional, bundles FFmpeg) when importable; otherwise an ffmpeg executable, which is the
one that ships with `imageio-ffmpeg` (already a project dependency) or `ffmpeg` on PATH. With neither,
`build_audio_converter()` returns None and the bot asks the user to type instead.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

TARGET_RATE = 16_000
TIMEOUT_S = 30.0
MAX_WAV_BYTES = 5_000_000  # same cap as POST /agent/voice (about 2.5 minutes)


class AudioConversionError(RuntimeError):
    """The voice note could not be decoded or converted."""


def av_available() -> bool:
    try:
        import av  # noqa: F401
    except ImportError:
        return False
    return True


def find_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:  # the bundled binary may be absent on some platforms
        pass
    return shutil.which("ffmpeg")


class FfmpegAudioConverter:
    def __init__(self, executable: str) -> None:
        self._exe = executable

    def to_wav(self, audio: bytes, mime: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="willay-audio-") as tmp:
            source, target = Path(tmp, "in.bin"), Path(tmp, "out.wav")  # a file, not a pipe: ffmpeg then writes a seekable WAV header
            source.write_bytes(audio)
            command = [self._exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source), "-vn", "-ac", "1", "-ar", str(TARGET_RATE), "-c:a", "pcm_s16le", str(target)]
            try:
                done = subprocess.run(command, capture_output=True, timeout=TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as exc:
                raise AudioConversionError(f"ffmpeg could not run: {type(exc).__name__}") from exc
            if done.returncode != 0 or not target.exists() or target.stat().st_size == 0:
                raise AudioConversionError("ffmpeg could not decode the audio")
            wav = target.read_bytes()
        if len(wav) > MAX_WAV_BYTES:
            raise AudioConversionError("the audio is too long")
        return wav


class PyAvAudioConverter:
    def to_wav(self, audio: bytes, mime: str) -> bytes:
        import av  # local import: optional dependency

        pcm = bytearray()
        try:
            with av.open(io.BytesIO(audio)) as container:
                resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
                for frame in container.decode(audio=0):
                    for out in resampler.resample(frame):
                        pcm += bytes(out.planes[0])[: out.samples * 2]
                for out in resampler.resample(None):
                    pcm += bytes(out.planes[0])[: out.samples * 2]
        except Exception as exc:  # PyAV raises many error types for bad input
            raise AudioConversionError(f"PyAV could not decode the audio: {type(exc).__name__}") from exc
        if not pcm:
            raise AudioConversionError("the audio has no samples")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(TARGET_RATE)
            out.writeframes(bytes(pcm))
        wav = buffer.getvalue()
        if len(wav) > MAX_WAV_BYTES:
            raise AudioConversionError("the audio is too long")
        return wav


def build_audio_converter() -> FfmpegAudioConverter | PyAvAudioConverter | None:
    if av_available():
        return PyAvAudioConverter()
    exe = find_ffmpeg()
    return FfmpegAudioConverter(exe) if exe else None
