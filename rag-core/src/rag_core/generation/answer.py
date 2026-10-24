"""Answer generation: canned text, the LLM, or the deterministic fact composer.

Order:
1. Canned intents (greeting, thanks, human, out of scope) get fixed text: no facts, no LLM.
2. ``generation.mode: llm`` with a working client and retrieved facts: the LLM rephrases the facts
   into an :class:`AnswerDraft` (structured output). Any failure (timeout, API error, refusal,
   invalid JSON after one retry) falls back to the composer and records ``llm_error``.
3. Otherwise the :class:`FactComposer` builds the answer from structured facts.
4. When nothing can answer, the draft is unanswerable with the safe message; with an LLM error
   the escalation reason is LLM_ERROR.

The draft is not trusted here: ``rag_core.scoring`` checks it before anyone hears it.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from rag_core.config import GenerationSettings, Settings
from rag_core.contracts import Intent, RetrievedFact
from rag_core.generation.llm_client import LLMClient, LLMError, LLMUnavailable, make_llm_client
from rag_core.generation.prompts import SYSTEM_PROMPT, AnswerDraft, build_user_message
from rag_core.generation.templates import CANNED_INTENTS, FactComposer, canned_answer
from rag_core.nlu.entities import EntityMention

log = logging.getLogger(__name__)

GenerationSource = Literal["canned", "llm", "template", "fallback", "none"]


class Generation(BaseModel):
    draft: AnswerDraft
    source: GenerationSource
    llm_error: str | None = None
    llm_ms: float | None = None


# Credential VALUES only: an "sk-..." key, a bearer token, or the value after "api_key=" / "x-api-key:".
# Names such as ANTHROPIC_API_KEY stay readable, so an operator can still see what to configure.
_KEY_VALUE = re.compile(r"sk-[A-Za-z0-9_\-]{6,}")
_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)
_ASSIGNED = re.compile(r"((?:x-)?api[_-]?key\s*[:=]\s*)\S+", re.IGNORECASE)


def redact(text: str) -> str:
    """Strip credential values from an error message before it is stored or shown."""
    text = _KEY_VALUE.sub("[redacted]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    return _ASSIGNED.sub(r"\1[redacted]", text)[:300]


class AnswerGenerator:
    def __init__(self, cfg: GenerationSettings, llm: LLMClient | None = None, llm_status: str | None = None):
        self._cfg = cfg
        self._llm = llm if cfg.mode == "llm" else None
        self._composer = FactComposer(cfg)
        self.llm_status = llm_status or ("configured" if self._llm else "disabled (generation.mode: template)")
        # Outcome of real LLM calls, for /health: configured is not the same as working.
        self.last_llm_success: datetime | None = None
        self.last_llm_error: str | None = None
        self.last_llm_error_at: datetime | None = None
        self._last_call_ok: bool | None = None  # outcome of the most recent call (timestamps can tie on Windows)

    @property
    def llm_ready(self) -> bool:
        """An LLM is configured and the most recent call (if any) succeeded."""
        return self._llm is not None and self._last_call_ok is not False

    def health(self) -> dict:
        return {
            "llm": self.llm_status,
            "llm_ready": self.llm_ready,
            "llm_last_success": self.last_llm_success.isoformat(timespec="seconds") if self.last_llm_success else None,
            "llm_last_error": self.last_llm_error,
        }

    def _llm_failed(self, message: str) -> str:
        self.last_llm_error = redact(message)
        self.last_llm_error_at = datetime.now()
        self._last_call_ok = False
        return self.last_llm_error

    @classmethod
    def from_settings(cls, settings: Settings, llm: LLMClient | None = None) -> AnswerGenerator:
        cfg = settings.generation
        status = None
        if llm is None and cfg.mode == "llm":
            try:
                llm = make_llm_client(settings)
            except LLMUnavailable as exc:
                status = f"unavailable, answering from templates: {redact(str(exc))}"
                log.warning("LLM %s", status)
        return cls(cfg, llm, status)

    @property
    def has_llm(self) -> bool:
        return self._llm is not None

    def _safe(self) -> AnswerDraft:
        return AnswerDraft(answer=self._cfg.safe_message, answerable=False, cited_fact_ids=[],
                           needs_clarification=False, clarification_options=[])

    def generate(
        self,
        question: str,
        intent: Intent,
        mentions: Sequence[EntityMention],
        facts: Sequence[RetrievedFact],
        now: datetime,
    ) -> Generation:
        if intent in CANNED_INTENTS:
            text = canned_answer(intent, question)
            draft = AnswerDraft(answer=text, answerable=True, cited_fact_ids=[], needs_clarification=False,
                                clarification_options=[])
            return Generation(draft=draft, source="canned")

        llm_error = llm_ms = None
        if self._llm is not None and facts:
            start = time.perf_counter()
            try:
                raw = self._llm.generate(SYSTEM_PROMPT, build_user_message(question, now, intent, mentions, facts), AnswerDraft)
                llm_ms = (time.perf_counter() - start) * 1000
                draft = AnswerDraft.model_validate(raw)
                draft.clarification_options = draft.clarification_options[: self._cfg.max_clarification_options]
                self.last_llm_success = datetime.now()
                self._last_call_ok = True
                return Generation(draft=draft, source="llm", llm_ms=llm_ms)
            except LLMError as exc:
                llm_ms = (time.perf_counter() - start) * 1000
                llm_error = self._llm_failed(str(exc))
                log.warning("LLM failed, falling back to templates: %s", llm_error)
            except Exception as exc:  # any other provider/SDK failure must also fall back, never crash a request
                llm_ms = (time.perf_counter() - start) * 1000
                llm_error = self._llm_failed(f"{exc.__class__.__name__}: {exc}")
                log.warning("unexpected LLM failure, falling back to templates: %s", llm_error)

        draft = self._composer.compose(question, intent, mentions, facts, now)
        if draft is not None:
            source: GenerationSource = "fallback" if llm_error else "template"
            return Generation(draft=draft, source=source, llm_error=llm_error, llm_ms=llm_ms)
        return Generation(draft=self._safe(), source="none", llm_error=llm_error, llm_ms=llm_ms)
