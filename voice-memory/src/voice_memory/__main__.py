"""Talk to the receptionist from a terminal.

    python -m voice_memory                 # type questions (follow-ups are resolved by memory)
    python -m voice_memory --voice         # kiosk mode: microphone in, speaker out (needs .[stt,tts,audio])
    python -m voice_memory --wav q.wav     # transcribe one WAV file and answer it

rag-core must be running (``just serve`` in rag-core) unless rag.mode is ``inprocess``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from voice_memory.config import get_settings


def _show(turn) -> None:
    if turn.question != turn.transcript:
        print(f"  (understood as: {turn.question})")
    print(f"receptionist> {turn.answer if turn.outcome != 'escalated' else turn.speech}")
    if turn.options:
        print(f"  [options: {' | '.join(turn.options)}]")
    if turn.ticket:
        print(f"  [receptionist alerted, ticket {turn.ticket.ticket_id}]")
    print(f"  [{turn.outcome}, {turn.latency_ms.get('total', 0):.0f} ms]")


def main(argv: list[str] | None = None) -> int:
    from voice_memory.receptionist import build_receptionist

    parser = argparse.ArgumentParser(description="CHRIST University 8th floor receptionist (voice + memory).")
    parser.add_argument("--voice", action="store_true", help="microphone in, speaker out")
    parser.add_argument("--wav", type=Path, help="answer the question in this WAV file")
    parser.add_argument("--session", default="terminal")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not (args.voice or args.wav):
        settings = settings.model_copy(update={
            "stt": settings.stt.model_copy(update={"backend": "text"}),
            "tts": settings.tts.model_copy(update={"backend": "none"}),
        })
    r = build_receptionist(settings)

    if args.wav:
        from voice_memory.speech.audio import read_wav, resample

        audio, rate = read_wav(args.wav.read_bytes())
        turn = r.handle_audio(args.session, resample(audio, rate, settings.audio.sample_rate), settings.audio.sample_rate)
        print(f"visitor> {turn.transcript}")
        _show(turn)
        return 0

    if args.voice:
        from voice_memory.speech.audio import play_wav, record_utterance

        print("Listening. Press Ctrl+C to stop.")
        try:
            while True:
                audio = record_utterance(settings.audio)
                if audio is None:
                    r.new_visitor(args.session)  # nobody spoke for a while: next visitor
                    continue
                turn = r.handle_audio(args.session, audio, settings.audio.sample_rate)
                print(f"visitor> {turn.transcript}")
                _show(turn)
                if turn.audio_wav_base64:
                    import base64

                    play_wav(base64.b64decode(turn.audio_wav_base64))
        except KeyboardInterrupt:
            return 0

    print("Ask about faculty, rooms, timetables, places or events. 'new' = new visitor, Ctrl+D to quit.")
    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        if text.casefold() in ("new", "reset"):
            r.new_visitor(args.session)
            print("  [new visitor]")
            continue
        _show(r.handle_text(args.session, text, speak=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
