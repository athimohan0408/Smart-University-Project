"""Speech-to-text. Pluggable: ``stt.backend`` = faster_whisper (offline) | text (typed input).

Faster-Whisper runs offline on the kiosk CPU. Its ``initial_prompt`` is filled with faculty names
and programme codes from the KB, which makes Whisper spell "Dr Thirunavukkarasu V" or "MSAIM"
the way the KB does instead of guessing ("Tiruna Vukarasu", "M.S. aim"). rag-core's entity linker
already tolerates ASR splits and lower case, so the transcript is passed on as it is.
"""

from __future__ import annotations

import time
from typing import Protocol

import numpy as np
from pydantic import BaseModel

from voice_memory.config import STTSettings
from voice_memory.kb import KBView


class Transcript(BaseModel):
    text: str
    language: str | None = None
    confidence: float | None = None  # mean token probability where the backend gives one
    latency_ms: float


class SpeechToText(Protocol):
    def transcribe(self, audio: np.ndarray, sample_rate: int) -> Transcript:
        """``audio``: mono float32 in [-1, 1]."""
        ...


def kb_prompt(kb: KBView, limit: int) -> str:
    """A Whisper initial prompt: the spellings visitors' words should map to."""
    names = sorted({p.name for p in kb.people.values()})[:limit]
    codes = sorted({n for cid, n in kb.names.items() if cid.startswith(("programme:", "course:")) and n.isupper()})
    return "CHRIST University receptionist, Central Block eighth floor. " + ", ".join([*names, *codes[:20]]) + "."


class FasterWhisperSTT:
    def __init__(self, cfg: STTSettings, language: str, prompt: str | None = None):
        from faster_whisper import WhisperModel  # optional extra: pip install -e .[stt]

        self._model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)
        self._cfg = cfg
        self._language = language
        self._prompt = prompt

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> Transcript:
        if sample_rate != 16000:
            raise ValueError("faster-whisper expects 16 kHz audio (audio.sample_rate)")
        start = time.perf_counter()
        segments, info = self._model.transcribe(
            audio.astype(np.float32), language=self._language, beam_size=self._cfg.beam_size,
            initial_prompt=self._prompt, vad_filter=True, condition_on_previous_text=False,
        )
        segments = list(segments)
        text = " ".join(s.text.strip() for s in segments).strip()
        probs = [float(np.exp(s.avg_logprob)) for s in segments]
        return Transcript(
            text=text, language=info.language, confidence=round(sum(probs) / len(probs), 3) if probs else None,
            latency_ms=(time.perf_counter() - start) * 1000,
        )


class TextSTT:
    """No audio: for typed input and tests. ``transcribe`` is not used; the text goes straight in."""

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> Transcript:
        raise RuntimeError("stt.backend is 'text': send typed text, not audio")


def make_stt(cfg: STTSettings, language: str, kb: KBView | None = None) -> SpeechToText:
    if cfg.backend == "text":
        return TextSTT()
    prompt = kb_prompt(kb, cfg.max_prompt_names) if (kb is not None and cfg.bias_with_kb_names) else None
    return FasterWhisperSTT(cfg, language, prompt)
