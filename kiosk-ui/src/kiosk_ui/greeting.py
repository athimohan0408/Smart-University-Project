"""What the kiosk itself says out loud: the robot's greeting, and its own answers from the floor map.

Each WAV is made once with voice-memory's own voice and kept in memory.

voice-memory has no "say this" endpoint (its API answers questions), so the kiosk uses its speech code
directly when it is installed in the same environment: the same Piper voice and the same "speakable"
rules ("Dr" -> "Doctor"), so the greeting sounds like the answers. Without it (or with pyttsx3, which
hangs when called from a server thread on Windows) there is no WAV and the browser speaks instead.
"""

from __future__ import annotations

import threading
from collections import OrderedDict


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


_speaker = None  # voice-memory's Speaker, loaded once (loading the Piper voice takes a second or two)
_speaker_lock = threading.Lock()


def _synthesise(text: str) -> bytes | None:
    global _speaker
    try:
        from voice_memory.config import get_settings as vm_settings
        from voice_memory.speech.tts import make_tts
    except ImportError:
        return None
    try:
        with _speaker_lock:
            if _speaker is None:
                cfg = vm_settings()
                if cfg.tts.backend != "piper":
                    return None
                _speaker = make_tts(cfg.tts, cfg.speech)
            return _speaker.say(text).wav
    except Exception:  # noqa: BLE001 - voice model missing or broken: the browser voice takes over
        return None


class Voice:
    """Speak short kiosk sentences ("Room 815 is ... on the east side"), keeping the last few WAVs."""

    def __init__(self, enabled: bool, keep: int = 64) -> None:
        self.enabled = enabled
        self._cache: OrderedDict[str, bytes | None] = OrderedDict()
        self._keep = keep
        self._lock = threading.Lock()

    def wav(self, text: str) -> bytes | None:
        if not self.enabled:
            return None
        with self._lock:
            if text in self._cache:
                self._cache.move_to_end(text)
                return self._cache[text]
            data = self._cache[text] = _synthesise(text)
            while len(self._cache) > self._keep:
                self._cache.popitem(last=False)
            return data
