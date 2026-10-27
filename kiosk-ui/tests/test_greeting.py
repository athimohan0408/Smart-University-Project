from __future__ import annotations

from fastapi.testclient import TestClient

from kiosk_ui import greeting as greeting_module
from kiosk_ui.greeting import Greeting
from kiosk_ui.server import create_app


def test_greeting_is_made_once_and_kept(monkeypatch):
    calls = []
    monkeypatch.setattr(greeting_module, "_synthesise", lambda text: calls.append(text) or b"RIFF-wav")
    g = Greeting("Hello!")
    assert g.wav() == b"RIFF-wav" and g.wav() == b"RIFF-wav"
    assert calls == ["Hello!"]


def test_no_voice_means_the_browser_speaks(monkeypatch):
    monkeypatch.setattr(greeting_module, "_synthesise", lambda text: None)
    assert Greeting("Hello!").wav() is None


def test_greeting_endpoints(settings, voice_memory, monkeypatch):
    with TestClient(create_app(settings, transport=voice_memory)) as client:  # speak is off in test settings
        info = client.get("/api/greeting").json()
        assert info == {"text": settings.kiosk.greeting, "audio_url": None}
        assert client.get("/api/greeting.wav").status_code == 404

    monkeypatch.setattr(greeting_module, "_synthesise", lambda text: b"RIFF-wav")
    on = settings.model_copy(update={"kiosk": settings.kiosk.model_copy(update={"speak": True})})
    with TestClient(create_app(on, transport=voice_memory)) as client:
        assert client.get("/api/greeting").json()["audio_url"] == "/api/greeting.wav"
        r = client.get("/api/greeting.wav")
        assert r.status_code == 200 and r.content == b"RIFF-wav" and r.headers["content-type"] == "audio/wav"


def test_kiosk_page_opens_on_the_robot_welcome():
    from kiosk_ui.server import STATIC

    html = (STATIC / "kiosk.html").read_text(encoding="utf-8")
    for needed in ('id="intro"', 'id="intro-slot"', 'id="intro-line"', 'id="intro-ask"', 'id="intro-mic"',
                   'id="chat" class="layout" hidden', 'id="stage-slot"', 'id="robot"', 'id="tap-start"',
                   'class="r-mouth"', 'class="r-arm r-arm-r"'):
        assert needed in html
