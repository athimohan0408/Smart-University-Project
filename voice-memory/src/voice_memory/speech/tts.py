"""Text-to-speech. Pluggable: ``tts.backend`` = piper (offline neural) | pyttsx3 (OS voice) | none.

``synthesize`` returns 16-bit mono WAV bytes, so the kiosk can play them locally and the UI can
receive them over HTTP. The text is passed through :func:`speakable` first.
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import wave
from typing import Protocol

from pydantic import BaseModel

from voice_memory.config import SpeechSettings, TTSSettings
from voice_memory.speech.speakable import speakable


class Speech(BaseModel):
    text: str  # what was actually spoken (after speakable)
    wav: bytes | None
    latency_ms: float


class TextToSpeech(Protocol):
    def wav(self, text: str) -> bytes | None: ...


class PiperTTS:
    def __init__(self, cfg: TTSSettings):
        from piper import PiperVoice  # optional extra: pip install -e .[tts]

        if not cfg.piper_voice.exists():
            raise FileNotFoundError(f"Piper voice not found: {cfg.piper_voice} (see README, 'Speech models')")
        self._voice = PiperVoice.load(str(cfg.piper_voice))

    def wav(self, text: str) -> bytes:
        buf = io.BytesIO()
        # piper-tts >= 1.3 names it synthesize_wav; older releases write the WAV from synthesize.
        write = getattr(self._voice, "synthesize_wav", None) or self._voice.synthesize
        with wave.open(buf, "wb") as out:
            write(text, out)
        return buf.getvalue()


class Pyttsx3TTS:
    def __init__(self, cfg: TTSSettings):
        import pyttsx3  # optional extra: pip install -e .[tts-basic]

        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", cfg.pyttsx3_rate)

    def wav(self, text: str) -> bytes:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            self._engine.save_to_file(text, path)
            self._engine.runAndWait()
            with open(path, "rb") as fh:
                return fh.read()
        finally:
            os.unlink(path)


class NoTTS:
    """Screen only (or tests): no audio."""

    def wav(self, text: str) -> None:
        return None


class Speaker:
    def __init__(self, engine: TextToSpeech, speech: SpeechSettings):
        self._engine = engine
        self._speech = speech

    def say(self, text: str) -> Speech:
        start = time.perf_counter()
        spoken = speakable(text, rooms_as_digits=self._speech.rooms_as_digits, spell_codes=self._speech.spell_codes)
        audio = self._engine.wav(spoken)
        return Speech(text=spoken, wav=audio, latency_ms=(time.perf_counter() - start) * 1000)


def make_tts(cfg: TTSSettings, speech: SpeechSettings) -> Speaker:
    engine: TextToSpeech = {"piper": PiperTTS, "pyttsx3": Pyttsx3TTS}.get(cfg.backend, lambda _: NoTTS())(cfg)
    return Speaker(engine, speech)
