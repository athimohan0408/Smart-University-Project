"""Speakable text, audio helpers, and the receptionist hand-off."""

import io
import json
import wave

import httpx
import numpy as np
import pytest

from tests.conftest import result
from voice_memory.handoff import Handoff
from voice_memory.speech.audio import FRAME_S, detect_utterance, read_wav, resample
from voice_memory.speech.speakable import speakable
from voice_memory.speech.stt import kb_prompt


@pytest.mark.parametrize("screen,spoken", [
    ("Dr Nisha Varghese's office is room 874, Central Block.", "Doctor Nisha Varghese's office is room 8 7 4, Central Block."),
    ("Their listed email is nisha.varghese@christuniversity.in.", "Their listed email is nisha dot varghese at christuniversity dot in."),
    ("consultation hours are 1:45 pm to 3:45 pm", "consultation hours are 1:45 P M to 3:45 P M"),
    ("room 607A in Block II", "room 6 0 7 A in Block I I"),
    ("2 MSAIM ML; 5 MCA A AMI", "2 M S A I M M L, 5 M C A A A M I"),
    ("AI lab is OK", "AI lab is OK"),
])
def test_speakable(screen, spoken):
    assert speakable(screen) == spoken


def test_speakable_options_off():
    assert speakable("room 874 MSAIM", rooms_as_digits=False, spell_codes=False) == "room 874 MSAIM"


def test_whisper_prompt_lists_kb_names(kb):
    prompt = kb_prompt(kb, limit=3)
    assert prompt.count("Dr ") == 3 and prompt.startswith("CHRIST University")


def _wav(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((samples * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def test_wav_roundtrip_and_resample():
    tone = (0.5 * np.sin(np.linspace(0, 2 * np.pi * 440, 48000))).astype(np.float32)
    audio, rate = read_wav(_wav(tone, 48000))
    assert rate == 48000 and np.allclose(audio, tone, atol=1e-3)
    assert len(resample(audio, 48000, 16000)) == 16000


def test_detect_utterance(settings):
    n = int(settings.audio.sample_rate * FRAME_S)
    quiet, loud = np.zeros(n, np.float32), np.full(n, 0.1, np.float32)
    assert detect_utterance([quiet] * 5, settings.audio) is None
    assert detect_utterance([quiet, quiet, loud, loud, quiet], settings.audio) == (2, 3)


# --------------------------------------------------------------------------- hand-off


def test_escalation_offers_the_receptionist(settings):
    h = Handoff(settings.handoff)
    res = result("I'm sorry, I can't verify a phone number for Dr Nisha Varghese.", escalate=True, reason="not_in_kb")
    offer = h.offer("s1", "phone of Dr Nisha Varghese", res)
    assert offer.speech.endswith(settings.handoff.offer_prompt) and offer.awaiting_reply and offer.ticket is None
    assert offer.options == ["Talk to the receptionist", "Ask another question"]
    assert not settings.handoff.tickets.exists()  # nobody is alerted until the visitor says yes


def test_asking_for_a_person_alerts_straight_away(settings):
    h = Handoff(settings.handoff)
    offer = h.offer("s1", "I want to talk to someone", result("Of course. Please speak to the receptionist at the desk.",
                                                              intent="escalate_to_human", escalate=True, reason="user_requested"))
    assert offer.ticket is not None and not offer.awaiting_reply
    line = json.loads(settings.handoff.tickets.read_text().splitlines()[0])
    assert line["reason"] == "user_requested" and line["session_id"] == "s1"
    assert set(line) == {"ticket_id", "timestamp", "session_id", "question", "reason", "answer_given"}


@pytest.mark.parametrize("reply,expected", [
    ("yes please", True), ("yeah", True), ("call the receptionist", True), ("ok", True),
    ("no thanks", False), ("it's okay", False), ("never mind", False),
    ("where is room 874", None),
])
def test_reply_to_the_offer(settings, reply, expected):
    assert Handoff(settings.handoff).reply(reply) is expected


def test_webhook_receives_the_ticket_and_failures_dont_break_it(settings):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    h = Handoff(settings.handoff, "https://desk.example/alert", httpx.Client(transport=httpx.MockTransport(handler)))
    ticket = h.alert("s1", "where is the canteen", "not_in_kb")
    assert seen and seen[0]["ticket_id"] == ticket.ticket_id

    def down(request):
        raise httpx.ConnectError("desk offline")

    h = Handoff(settings.handoff, "https://desk.example/alert", httpx.Client(transport=httpx.MockTransport(down)))
    assert h.alert("s1", "q", "not_in_kb").ticket_id  # still written to the ticket file
    assert len(settings.handoff.tickets.read_text().splitlines()) == 2
