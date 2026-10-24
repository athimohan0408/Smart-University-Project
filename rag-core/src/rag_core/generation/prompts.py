"""The grounded receptionist prompt: system prompt, user message and the JSON answer schema.

The exact text is also written to ``docs/prompts.md`` for the report:
``python -m rag_core.generation.prompts`` regenerates it, and a test fails when the two drift.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rag_core.config import PROJECT_ROOT
from rag_core.contracts import Intent, RetrievedFact
from rag_core.nlu.entities import EntityMention

PROMPTS_DOC = PROJECT_ROOT / "docs" / "prompts.md"


class AnswerDraft(BaseModel):
    """What the LLM (or a template) returns. Checked by the grounding stage before anyone hears it."""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(description="1-3 short spoken sentences, no markdown.")
    answerable: bool = Field(description="False when the facts don't contain the answer.")
    cited_fact_ids: list[str] = Field(description="fact ids supporting every claim in the answer.")
    needs_clarification: bool = Field(description="True when several people/things match and the visitor must pick.")
    clarification_options: list[str] = Field(description="The candidates to offer (at most 3), else empty.")


SYSTEM_PROMPT = """\
You are the receptionist for the Computer Science department on the 8th floor of Central Block, \
CHRIST University. You help visitors find faculty members, rooms, timetables and consultation hours.

Your answers are SPOKEN ALOUD by a text-to-speech voice on a kiosk:
- Reply in 1 to 3 short, polite sentences. No markdown, no bullet points, no lists, no emojis.
- Say room numbers as digits exactly as written in the facts, for example "room 874".
- Say times in 12-hour form with am or pm, for example "1:45 pm" or "9 am".

Grounding rules (these override everything else):
1. Use ONLY the numbered FACTS in the user message. Do not use general knowledge about the university, \
its people, its buildings or any subject.
2. Every factual claim must be supported by a fact, and you must list the ids of those facts in \
"cited_fact_ids". Cite only ids that appear in FACTS.
3. If the FACTS do not contain the answer, set "answerable" to false, say politely that you can't verify \
that, and offer the human receptionist at the desk. Never guess, and never fill in a value from a pattern.
4. Never say an email address, phone number, room number, name or time that does not appear verbatim in \
a fact. There are no phone numbers in the facts.
5. If a fact says a value is "not listed", say that it isn't listed and offer the receptionist; set \
"answerable" to false.
6. If a faculty member is NOT on this floor (Central Block, Eighth Floor), say so and give their actual \
block, floor and room from the facts.
7. If a subject or programme name is marked "not verified", say the code as written (for example "ML") \
and do not expand it into a name.
8. If a fact carries a "Caution" about conflicting records, mention both values and say the records \
disagree. Do not pick one.
9. If several different people match the question equally, do not choose. Set "needs_clarification" to \
true, put at most 3 of their names in "clarification_options", and ask which one the visitor means.
10. If the question asks about something the FACTS only partly cover, answer the supported part and say \
plainly what isn't available.
11. If DETECTED ENTITIES says a person was not found, never answer with facts about anyone else, even \
someone with a similar name. Set "answerable" to false, say you can't find that person, and you may ask \
whether they meant the similar name listed.

Reply with ONLY a JSON object with exactly these keys:
{"answer": string, "answerable": boolean, "cited_fact_ids": [string], "needs_clarification": boolean, \
"clarification_options": [string]}"""


def format_now(now: datetime) -> str:
    hour = now.hour % 12 or 12
    minutes = f":{now.minute:02d}" if now.minute else ""
    return f"{now:%A} {now.day} {now:%B %Y}, {hour}{minutes} {'am' if now.hour < 12 else 'pm'}"


def _entity_line(m: EntityMention) -> str:
    if m.ambiguous:
        names = " | ".join(c.name for c in m.candidates)
        return f'- {m.type.value} "{m.text}": AMBIGUOUS, could be {names}'
    if m.canonical_id:
        return f'- {m.type.value} "{m.text}" -> {m.candidates[0].name} ({m.canonical_id})'
    if m.near_misses:
        names = " | ".join(c.name for c in m.near_misses)
        return f'- {m.type.value} "{m.text}": not found in the records (similar name, a DIFFERENT person: {names})'
    return f'- {m.type.value} "{m.text}": not found in the records'


def build_user_message(
    question: str,
    now: datetime,
    intent: Intent,
    mentions: Sequence[EntityMention],
    facts: Sequence[RetrievedFact],
) -> str:
    entities = "\n".join(_entity_line(m) for m in mentions) or "- none"
    numbered = "\n".join(f"{n}. [{f.fact_id}] {f.text}" for n, f in enumerate(facts, start=1)) or "(no facts found)"
    return (
        f"QUESTION: {question}\n"
        f"CURRENT DAY AND TIME: {format_now(now)}\n"
        f"DETECTED INTENT: {intent.value}\n"
        f"DETECTED ENTITIES:\n{entities}\n\n"
        f"FACTS:\n{numbered}"
    )


def render_prompts_doc() -> str:
    from rag_core.generation.llm_client import json_schema_for
    import json

    example_facts = [
        RetrievedFact(
            fact_id="faculty:nisha-varghese:location",
            text="Dr Nisha Varghese's office is room 874, Central Block, Eighth Floor (on this floor).",
            score=1.0, rank=1, via="structured",
        ),
        RetrievedFact(
            fact_id="faculty:nisha-varghese:profile",
            text="Dr Nisha Varghese (short name NV) is a faculty member in the Computer Science department. ...",
            score=1.0, rank=2, via="structured",
        ),
    ]
    example = build_user_message(
        "where is Dr Nisha Varghese's cabin?",
        datetime(2026, 10, 20, 14, 30),
        Intent.FIND_PERSON,
        [],
        example_facts,
    ).replace("DETECTED ENTITIES:\n- none", 'DETECTED ENTITIES:\n- faculty "Nisha Varghese" -> Dr Nisha Varghese (faculty:nisha-varghese)')
    return "\n".join([
        "# Generation prompts",
        "",
        "Generated from `src/rag_core/generation/prompts.py` by `python -m rag_core.generation.prompts`. "
        "Do not edit by hand; a test fails when this file and the code drift.",
        "",
        "The LLM only rephrases retrieved facts. Its reply is validated against the schema below, "
        "then checked by the grounding stage (`rag_core.scoring.grounding_check`) before anyone hears it. "
        "Greetings, thanks, requests for a human and out-of-scope questions never reach the LLM "
        "(`rag_core.generation.templates`).",
        "",
        "## System prompt",
        "",
        "```text",
        SYSTEM_PROMPT,
        "```",
        "",
        "## User message template",
        "",
        "```text",
        "QUESTION: {resolved question}",
        "CURRENT DAY AND TIME: {weekday} {day} {month} {year}, {h:mm am/pm}",
        "DETECTED INTENT: {intent}",
        "DETECTED ENTITIES:",
        '- {type} "{surface text}" -> {name} ({canonical_id})   | AMBIGUOUS, could be A | B   | not found in the records '
        "[(similar name, a DIFFERENT person: X)]",
        "",
        "FACTS:",
        "{n}. [{fact_id}] {fact text}",
        "```",
        "",
        "Example:",
        "",
        "```text",
        example,
        "```",
        "",
        "## Output schema (structured output / JSON mode)",
        "",
        "```json",
        json.dumps(json_schema_for(AnswerDraft), indent=2),
        "```",
        "",
    ])


def write_prompts_doc(path: Path = PROMPTS_DOC) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_prompts_doc(), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(f"wrote {write_prompts_doc()}")
