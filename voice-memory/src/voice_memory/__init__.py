"""Portion 2 of the AI receptionist: voice in/out, conversation memory and the receptionist hand-off.

Entry points:
- :class:`voice_memory.memory.ConversationMemory` ``.rewrite(session_id, text)``: follow-up -> standalone question
- :class:`voice_memory.receptionist.Receptionist` ``.handle_text`` / ``.handle_audio``: one full turn
- ``python -m voice_memory.api``: HTTP API for the kiosk UI
- ``python -m voice_memory.metrics``: response latency and query completion rate
"""
