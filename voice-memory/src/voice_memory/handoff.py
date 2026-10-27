"""The escalation message and the "contact the receptionist" option.

When rag-core escalates (``escalate: true``), its ``answer`` already says politely what it couldn't
verify. This module adds what the visitor can do next:

- the on-screen options (``Talk to the receptionist`` / ``Ask another question``);
- the spoken offer ("Would you like me to call the receptionist for you?"), except when the visitor
  already asked for a person (``user_requested``): then the receptionist is alerted straight away;
- the hand-off itself: a ticket appended to ``handoff.tickets`` (JSON lines) and, when
  ``RECEPTIONIST_WEBHOOK_URL`` is set in .env, POSTed to the desk.

A ticket holds the question and why it was escalated, never anything else about the visitor.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field

from voice_memory.config import HandoffSettings

log = logging.getLogger(__name__)

# The visitor's reply to "Would you like me to call the receptionist?"
_ACCEPT = re.compile(
    r"^\s*(yes|yeah|yep|sure|please|ok(?:ay)?|call|haan|ha|go ahead|do it)\b|\b(call|get|need|want) (?:the |a )?(receptionist|someone|person|human)\b",
    re.I,
)
_DECLINE = re.compile(r"^\s*(no|nope|not now|no thanks|it'?s ok(?:ay)?|never ?mind|that'?s fine)\b", re.I)


class Ticket(BaseModel):
    ticket_id: str
    timestamp: str
    session_id: str | None
    question: str
    reason: str = Field(description="rag-core escalate_reason, or 'visitor_button' when the button was pressed.")
    answer_given: str


class HandoffOffer(BaseModel):
    """What the UI shows and the voice says after an escalation."""

    speech: str = Field(description="rag-core's answer plus, unless already alerted, the spoken offer.")
    options: list[str]
    ticket: Ticket | None = Field(default=None, description="Set when the receptionist was alerted already.")
    awaiting_reply: bool = Field(description="True: the next utterance may be a yes/no to the offer.")


class Handoff:
    def __init__(self, cfg: HandoffSettings, webhook_url: str | None = None, client: httpx.Client | None = None):
        self._cfg = cfg
        self._webhook = webhook_url
        self._client = client
        self._lock = threading.Lock()

    @property
    def options(self) -> list[str]:
        return [self._cfg.option_label, self._cfg.ask_again_label]

    def offer(self, session_id: str | None, question: str, result: dict) -> HandoffOffer:
        answer = result.get("answer", "")
        reason = result.get("escalate_reason") or "unknown"
        if reason == "user_requested":
            ticket = self.alert(session_id, question, reason, answer)
            return HandoffOffer(speech=f"{answer} {self._cfg.confirmed_message}", options=[self._cfg.ask_again_label],
                                ticket=ticket, awaiting_reply=False)
        return HandoffOffer(speech=f"{answer} {self._cfg.offer_prompt}", options=self.options, awaiting_reply=True)

    def unavailable(self) -> HandoffOffer:
        """rag-core could not be reached: nothing can be verified, so offer a person."""
        return HandoffOffer(speech=f"{self._cfg.unavailable_message} {self._cfg.offer_prompt}", options=self.options,
                            awaiting_reply=True)

    def reply(self, utterance: str) -> bool | None:
        """True = the visitor accepted the offer, False = declined, None = neither (treat as a new question)."""
        if _DECLINE.search(utterance):
            return False
        if _ACCEPT.search(utterance) and len(utterance.split()) <= 8:
            return True
        return None

    def alert(self, session_id: str | None, question: str, reason: str, answer_given: str = "") -> Ticket:
        ticket = Ticket(
            ticket_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            session_id=session_id,
            question=question,
            reason=reason,
            answer_given=answer_given,
        )
        line = ticket.model_dump_json()
        with self._lock:
            self._cfg.tickets.parent.mkdir(parents=True, exist_ok=True)
            with self._cfg.tickets.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self._webhook:
            try:
                client = self._client or httpx.Client(timeout=self._cfg.webhook_timeout_s)
                client.post(self._webhook, json=json.loads(line), timeout=self._cfg.webhook_timeout_s)
            except httpx.HTTPError as exc:  # the ticket is on disk; the desk alert is best effort
                log.warning("receptionist webhook failed: %s", exc)
        return ticket
