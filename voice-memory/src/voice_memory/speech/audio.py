"""Kiosk microphone and speaker (optional extra: pip install -e .[audio]).

``record_utterance`` waits for the visitor to start speaking, then stops after ``end_silence_s``
of silence, using RMS energy per 30 ms frame. Simple, but enough for a push-to-talk or always-on
kiosk; Whisper's own VAD trims what's left.
"""

from __future__ import annotations

import io
import wave

import numpy as np

from voice_memory.config import AudioSettings

FRAME_S = 0.03


def detect_utterance(frames: list[np.ndarray], cfg: AudioSettings) -> tuple[int, int] | None:
    """(first, last) speech frame indexes, or None if nobody spoke. Pure, so it is unit-tested."""
    loud = [float(np.sqrt(np.mean(f.astype(np.float64) ** 2))) >= cfg.silence_threshold for f in frames]
    if True not in loud:
        return None
    first = loud.index(True)
    last = len(loud) - 1 - loud[::-1].index(True)
    return first, last


def record_utterance(cfg: AudioSettings) -> np.ndarray | None:
    import sounddevice as sd

    frame = int(cfg.sample_rate * FRAME_S)
    frames: list[np.ndarray] = []
    started, quiet = False, 0
    with sd.InputStream(samplerate=cfg.sample_rate, channels=1, dtype="float32", blocksize=frame) as stream:
        while True:
            block, _ = stream.read(frame)
            block = block[:, 0].copy()
            frames.append(block)
            loud = float(np.sqrt(np.mean(block**2))) >= cfg.silence_threshold
            elapsed = len(frames) * FRAME_S
            if not started:
                if loud:
                    started, frames = True, frames[-10:]  # keep ~300 ms before the first loud frame
                elif elapsed > cfg.start_timeout_s:
                    return None
                continue
            quiet = 0 if loud else quiet + 1
            if quiet * FRAME_S >= cfg.end_silence_s or elapsed >= cfg.max_utterance_s:
                break
    return np.concatenate(frames)


def play_wav(data: bytes) -> None:
    import sounddevice as sd

    audio, rate = read_wav(data)
    sd.play(audio, rate)
    sd.wait()


def read_wav(data: bytes) -> tuple[np.ndarray, int]:
    """16-bit PCM WAV bytes -> (mono float32, sample rate)."""
    with wave.open(io.BytesIO(data)) as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM WAV")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def resample(audio: np.ndarray, rate: int, target: int) -> np.ndarray:
    """Linear resampling (browser uploads are often 44.1/48 kHz; Whisper wants 16 kHz)."""
    if rate == target:
        return audio
    n = int(round(len(audio) * target / rate))
    return np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
