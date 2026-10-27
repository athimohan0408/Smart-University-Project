"""The robot's spoken greeting: one WAV, made once with voice-memory's own voice and kept in memory.

voice-memory has no "say this" endpoint (its API answers questions), so the kiosk uses its speech code
directly when it is installed in the same environment: the same Piper voice and the same "speakable"
rules ("Dr" -> "Doctor"), so the greeting sounds like the answers. Without it (or with pyttsx3, which
hangs when called from a server thread on Windows) there is no WAV and the browser speaks instead.
"""

from __future__ import annotations

import threading


class Greeting:
    def __init__(self, text: str) -> None:
        self.text = text
        self._wav: bytes | None = None
        self._tried = False
        self._lock = threading.Lock()

    def wav(self) -> bytes | None:
        with self._lock:
            if not self._tried:
                self._tried = True
                self._wav = _synthesise(self.text)
            return self._wav

    def warm_up(self) -> None:
        """Make the WAV in the background so the first visitor doesn't wait for the voice model."""
        threading.Thread(target=self.wav, name="greeting-tts", daemon=True).start()


def _synthesise(text: str) -> bytes | None:
    try:
        from voice_memory.config import get_settings as vm_settings
        from voice_memory.speech.tts import make_tts
    except ImportError:
        return None
    try:
        cfg = vm_settings()
        if cfg.tts.backend != "piper":
            return None
        return make_tts(cfg.tts, cfg.speech).say(text).wav
    except Exception:  # noqa: BLE001 - voice model missing or broken: the browser voice takes over
        return None
