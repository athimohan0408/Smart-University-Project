"""Intent classification: layered, explainable, fast.

1. **Rules** catch a few unambiguous utterances: a message that is *only* a greeting, *only*
   thanks/goodbye, or an explicit request for a human. A rule match wins with probability
   ``intent.rule_confidence``.
2. **Model**: logistic regression on the question's query embedding. It uses the same
   :class:`Embedder` and prefix as retrieval, so the pipeline can embed the question once and pass
   the vector in. The model learns intent structure, not KB content: retrain only when the intents
   or the embedding model change, never when faculty/room data changes.
3. **LLM fallback** (``intent.llm_fallback_enabled``): if the model's top probability is below
   ``intent.llm_fallback_threshold``, ask the LLM for a JSON-only label. If the call fails or the
   reply isn't a valid label, the model's answer stands.

Every prediction records which layer decided (``source``) and keeps the model's own top-N
distribution, so accuracy can be reported with and without the fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import joblib
import sklearn
from pydantic import BaseModel, Field
from sklearn.linear_model import LogisticRegression

from rag_core.config import EmbeddingProfile, IntentSettings, Settings
from rag_core.contracts import Intent, Probability
from rag_core.generation.llm_client import LLMClient, LLMUnavailable, make_llm_client

log = logging.getLogger(__name__)

INTENT_DESCRIPTIONS: dict[Intent, str] = {
    Intent.FIND_PERSON: "who a faculty member is, or where their cabin/office is",
    Intent.PERSON_AVAILABILITY: "whether a faculty member is free now, when to meet them, consultation hours",
    Intent.FIND_BY_SUBJECT: "which faculty teaches or works on a subject or research topic",
    Intent.FIND_ROOM: "where a room, classroom, lab, washroom or office is, or how to get there",
    Intent.SCHEDULE_QUERY: "timetables: what a faculty member teaches when, which class is on now, class timings",
    Intent.PROGRAMME_INFO: "academic programmes (MCA, MSc AIML, BCA, BSc): what they are, subjects, semesters",
    Intent.EVENT_INFO: "events, seminars, workshops, guest lectures, fests, club meetings",
    Intent.FACILITY_INFO: "amenities: wifi, drinking water, printing, lift, canteen, charging, waiting area",
    Intent.CONTACT_INFO: "email, phone or other contact details of a faculty member or office",
    Intent.GREETING: "a greeting only (hi, hello, good morning, namaste)",
    Intent.SMALLTALK_OR_THANKS: "thanks, goodbye, or chat about the assistant itself",
    Intent.ESCALATE_TO_HUMAN: "the visitor asks to talk to a human, the receptionist or staff",
    Intent.OUT_OF_SCOPE: "anything not about this floor: weather, fees, admissions, hostel, news, homework",
}


# --------------------------------------------------------------------------- rules


def normalise(text: str) -> str:
    """Lower-case, drop apostrophes ("ma'am" -> "maam"), turn other punctuation into spaces."""
    text = re.sub(r"['’`]", "", text.casefold())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


_GREET = r"(?:hi+|hello+|helo|hey+|hai|yo|greetings|namaste|namaskar(?:a|am)?|vanakkam|good (?:morning|afternoon|evening|day))"
_POLITE = r"(?:sir|madam|mam|maam|there|robot|everyone|all|ji|friend|dear|to|you|again)"
_THANKS = r"(?:thanks?|thank (?:you|u)|thanku|thankyou|thx|tq|bye(?: bye)?|goodbye|see you)"
_THANKS_TAIL = r"(?:so much|very much|a lot|for (?:the|your) help|ok(?:ay)?|bye|again|great|nice)"
_HUMAN = r"(?:a |the |some |any )?(?:real )?(?:human(?: being)?|person|receptionist|staff(?: member)?|office staff|someone|somebody|anyone|anybody)"
_NOT_A_SPECIFIC_PERSON = r"(?! (?:who|that|which|from|for|in|teaching|about)\b)"


@dataclass(frozen=True)
class Rule:
    name: str
    intent: Intent
    pattern: re.Pattern[str]


RULES: tuple[Rule, ...] = (
    Rule("greeting_only", Intent.GREETING, re.compile(rf"^{_GREET}(?: (?:{_GREET}|{_POLITE}))*$")),
    Rule(
        "thanks_or_bye_only",
        Intent.SMALLTALK_OR_THANKS,
        re.compile(rf"^(?:(?:ok(?:ay)?|great|super|cool) )?{_THANKS}(?: (?:{_THANKS_TAIL}|{_POLITE}))*$"),
    ),
    Rule("human_only", Intent.ESCALATE_TO_HUMAN, re.compile(r"^(?:a )?(?:human|receptionist|real person)(?: please)?$")),
    Rule(
        "talk_to_human",
        Intent.ESCALATE_TO_HUMAN,
        re.compile(rf"\b(?:talk|speak|chat) (?:to|with) {_HUMAN}\b{_NOT_A_SPECIFIC_PERSON}"),
    ),
    Rule(
        "connect_to_human",
        Intent.ESCALATE_TO_HUMAN,
        re.compile(rf"\b(?:connect|transfer|put) me (?:to|with|through to) {_HUMAN}\b{_NOT_A_SPECIFIC_PERSON}"),
    ),
    Rule(
        "fetch_a_human",
        Intent.ESCALATE_TO_HUMAN,
        re.compile(rf"\b(?:call|get|send|bring) (?:me )?{_HUMAN}\b{_NOT_A_SPECIFIC_PERSON}"),
    ),
    Rule("real_human", Intent.ESCALATE_TO_HUMAN, re.compile(r"\b(?:real|actual) (?:human|person)\b")),
    Rule(
        "not_a_machine",
        Intent.ESCALATE_TO_HUMAN,
        re.compile(r"\b(?:dont|do not) want to (?:talk|speak) (?:to|with) (?:a |the |this )?(?:machine|robot|bot)\b"),
    ),
)


def match_rule(text: str) -> Rule | None:
    norm = normalise(text)
    return next((rule for rule in RULES if rule.pattern.search(norm)), None)


# --------------------------------------------------------------------------- data + artifact


class IntentExample(BaseModel):
    text: str = Field(min_length=1)
    intent: Intent


def load_examples(path: Path) -> list[IntentExample]:
    with path.open(encoding="utf-8") as fh:
        return [IntentExample.model_validate_json(line) for line in fh if line.strip()]


class IntentModelMismatch(RuntimeError):
    """The saved model doesn't match the current intents or embedding model: run `just train-intent`."""


@dataclass
class IntentModel:
    """What `just train-intent` saves to ``paths.intent_model``."""

    classifier: LogisticRegression
    embedding_model: str
    query_prefix: str
    c: float
    cv_accuracy: float
    cv_by_c: dict[float, float]
    train_examples: int
    train_sha256: str
    sklearn_version: str = field(default_factory=lambda: sklearn.__version__)
    format_version: int = 1

    @property
    def labels(self) -> list[Intent]:
        return [Intent(c) for c in self.classifier.classes_]

    def check_compatible(self, profile: EmbeddingProfile) -> None:
        if (self.embedding_model, self.query_prefix) != (profile.name, profile.query_prefix):
            raise IntentModelMismatch(
                f"intent model was trained on {self.embedding_model!r} embeddings but settings use "
                f"{profile.name!r}: run `just train-intent`"
            )
        trained = set(self.classifier.classes_)
        current = {i.value for i in Intent}
        if trained != current:
            raise IntentModelMismatch(
                f"intents changed (added {sorted(current - trained)}, removed {sorted(trained - current)}): "
                "update the training data and run `just train-intent`"
            )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)

    @staticmethod
    def load(path: Path) -> IntentModel:
        if not path.exists():
            raise IntentModelMismatch(f"{path} not found: run `just train-intent`")
        model = joblib.load(path)
        if not isinstance(model, IntentModel):
            raise IntentModelMismatch(f"{path} is not an intent model: run `just train-intent`")
        return model


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- LLM fallback


def llm_system_prompt() -> str:
    labels = "\n".join(f"- {intent.value}: {desc}" for intent, desc in INTENT_DESCRIPTIONS.items())
    return (
        "You classify one visitor question asked at the reception kiosk on the 8th floor of Central "
        "Block, CHRIST University (Computer Science faculty offices, classrooms and labs).\n"
        f"Choose exactly one label:\n{labels}\n\n"
        'Reply with JSON only, no other text, in the form {"intent": "<label>"}.'
    )


_JSON_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)


def parse_llm_intent(reply: str) -> Intent | None:
    """The label from a JSON reply, or None for anything malformed or not a known label."""
    match = _JSON_OBJECT.search(reply or "")
    if not match:
        return None
    try:
        value = json.loads(match.group(0)).get("intent")
        return Intent(str(value).strip())
    except (ValueError, AttributeError):
        return None


# --------------------------------------------------------------------------- classifier


class QueryEmbedder(Protocol):
    profile: EmbeddingProfile

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


class IntentScore(BaseModel):
    intent: Intent
    probability: Probability


class IntentPrediction(BaseModel):
    intent: Intent
    probability: Probability
    top: list[IntentScore] = Field(description="Top-N distribution, best first (rule-adjusted if a rule fired).")
    source: Literal["rule", "model", "llm"]
    rule: str | None = None
    model_intent: Intent = Field(description="The classifier's own top intent, whatever decided in the end.")
    model_probability: Probability
    llm_attempted: bool = False


def _rule_distribution(
    ranked: list[tuple[Intent, float]], intent: Intent, confidence: float
) -> list[tuple[Intent, float]]:
    """Give the rule's intent ``confidence`` and share the rest in the model's proportions."""
    rest = [(i, p) for i, p in ranked if i != intent]
    mass = sum(p for _, p in rest)
    scale = (1.0 - confidence) / mass if mass > 0 else 0.0
    return [(intent, confidence)] + [(i, p * scale) for i, p in rest]


class IntentClassifier:
    def __init__(
        self,
        model: IntentModel,
        embedder: QueryEmbedder,
        cfg: IntentSettings,
        llm: LLMClient | None = None,
    ):
        model.check_compatible(embedder.profile)
        self._model = model
        self._labels = model.labels
        self._embedder = embedder
        self._cfg = cfg
        self._llm = llm
        self._system_prompt = llm_system_prompt()

    @classmethod
    def from_settings(
        cls, settings: Settings, embedder: QueryEmbedder | None = None, llm: LLMClient | None = None
    ) -> IntentClassifier:
        model = IntentModel.load(settings.paths.intent_model)
        if embedder is None:
            from rag_core.retrieval.embeddings import Embedder

            embedder = Embedder.from_settings(settings.embeddings)
        if llm is None and settings.intent.llm_fallback_enabled:
            try:
                llm = make_llm_client(settings)
            except LLMUnavailable as exc:
                log.warning("intent LLM fallback disabled: %s", exc)
        return cls(model, embedder, settings.intent, llm)

    @property
    def model(self) -> IntentModel:
        return self._model

    @property
    def has_llm(self) -> bool:
        return self._llm is not None

    def predict(
        self, text: str, *, query_vector: Sequence[float] | None = None, use_llm: bool | None = None
    ) -> IntentPrediction:
        vector = list(query_vector) if query_vector is not None else self._embedder.embed_queries([text])[0]
        return self._predict(text, vector, use_llm)

    def predict_many(self, texts: Sequence[str], *, use_llm: bool | None = None) -> list[IntentPrediction]:
        vectors = self._embedder.embed_queries(list(texts))
        return [self._predict(t, v, use_llm) for t, v in zip(texts, vectors)]

    def _predict(self, text: str, vector: Sequence[float], use_llm: bool | None) -> IntentPrediction:
        cfg = self._cfg
        probs = self._model.classifier.predict_proba([vector])[0]
        ranked = sorted(zip(self._labels, (float(p) for p in probs)), key=lambda pair: -pair[1])
        model_intent, model_p = ranked[0]
        base = {"model_intent": model_intent, "model_probability": model_p}

        if rule := match_rule(text):
            dist = _rule_distribution(ranked, rule.intent, cfg.rule_confidence)
            return IntentPrediction(
                intent=rule.intent, probability=cfg.rule_confidence, top=self._top(dist),
                source="rule", rule=rule.name, **base,
            )

        prediction = IntentPrediction(
            intent=model_intent, probability=model_p, top=self._top(ranked), source="model", **base
        )
        wants_llm = cfg.llm_fallback_enabled if use_llm is None else use_llm
        if wants_llm and self._llm is not None and model_p < cfg.llm_fallback_threshold:
            chosen = self._ask_llm(text)
            update = {"llm_attempted": True}
            if chosen is not None:
                update |= {"intent": chosen, "probability": cfg.llm_fallback_confidence, "source": "llm"}
            prediction = prediction.model_copy(update=update)
        return prediction

    def _ask_llm(self, text: str) -> Intent | None:
        try:
            reply = self._llm.complete(self._system_prompt, text)
        except Exception as exc:  # any provider failure: keep the model's answer
            log.warning("intent LLM fallback failed: %s", exc)
            return None
        chosen = parse_llm_intent(reply)
        if chosen is None:
            log.warning("intent LLM fallback returned no valid label: %r", reply[:200])
        return chosen

    def _top(self, ranked: list[tuple[Intent, float]]) -> list[IntentScore]:
        return [IntentScore(intent=i, probability=p) for i, p in ranked[: self._cfg.top_n]]
