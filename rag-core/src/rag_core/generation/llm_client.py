"""Pluggable LLM client. The provider comes from ``llm.provider``; each SDK is an optional extra.

Two calls, both with the model, token limit, timeout and retries from settings:
- ``generate(system, user, schema) -> dict``: a JSON object validated against a pydantic model.
  Each provider's native structured output is used (Anthropic ``output_config.format``, OpenAI
  ``response_format`` json_schema, Ollama ``format``). The reply is still parsed and validated
  with pydantic; if that fails, the call is retried once with the validation error appended, and
  a second failure raises :class:`LLMError`.
- ``complete(system, user) -> str``: plain text (the intent classifier's JSON-only fallback).

Determinism: ``llm.temperature`` (0) is sent to OpenAI and Ollama. Current Claude models reject
sampling parameters (a 400), so the Anthropic adapter sends ``llm.effort`` instead and relies on
the schema-constrained output. Anthropic requests opt into server-side refusal fallbacks
(``llm.anthropic_fallbacks``).

Provider SDKs are imported lazily, so only the selected one needs to be installed. Tests and eval
use :class:`FakeLLMClient`; nothing here is exercised against a live API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from rag_core.config import LLMSettings, Secrets, Settings


class LLMUnavailable(RuntimeError):
    """The configured provider can't be used (SDK not installed, or credentials missing)."""


class LLMError(RuntimeError):
    """A call failed: timeout, API error, refusal, or output that never validated."""


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> dict: ...


@dataclass
class Usage:
    """Real API usage counted from responses (for cost reports). Never contains request content."""

    calls: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def cost_usd(self, input_per_mtok: float, output_per_mtok: float) -> float:
        return self.input_tokens / 1e6 * input_per_mtok + self.output_tokens / 1e6 * output_per_mtok


def json_schema_for(schema: type[BaseModel]) -> dict:
    """The pydantic JSON schema, closed (``additionalProperties: false``) as strict modes require."""
    out = schema.model_json_schema()

    def close(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = sorted(node.get("properties", {}))
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)

    close(out)
    return out


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json_object(text: str) -> dict:
    """The first JSON object in ``text`` (tolerates code fences and stray prose around it)."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def validate_with_retry(
    call: Callable[[str], str], user: str, schema: type[BaseModel]
) -> dict:
    """Run ``call(user)``, validate the JSON reply; on failure retry once with the error appended."""
    prompt = user
    for attempt in range(2):
        reply = call(prompt)
        try:
            return schema.model_validate(parse_json_object(reply)).model_dump(mode="json")
        except (ValueError, ValidationError) as exc:
            if attempt == 1:
                raise LLMError(f"output did not validate after one retry: {exc}") from exc
            prompt = (
                f"{user}\n\nYour previous reply was not valid. Error:\n{exc}\n"
                "Reply again with ONLY a JSON object that matches the required schema."
            )
    raise AssertionError("unreachable")


def _import(module: str, extra: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise LLMUnavailable(f"`{module}` is not installed: pip install -e .[{extra}]") from exc


class AnthropicClient:
    def __init__(self, cfg: LLMSettings, secrets: Secrets, http_client=None):
        """``http_client``: an ``anthropic.DefaultHttpxClient`` (tests pass one with a mock transport)."""
        anthropic = _import("anthropic", "anthropic")
        self._anthropic = anthropic
        self._cfg = cfg
        self.usage = Usage()
        key = secrets.anthropic_api_key.get_secret_value() if secrets.anthropic_api_key else None
        extra = {"http_client": http_client} if http_client is not None else {}
        try:
            # Without a key the SDK resolves other credentials (ANTHROPIC_AUTH_TOKEN, an `ant auth login` profile).
            self._client = anthropic.Anthropic(api_key=key, timeout=cfg.timeout_s, max_retries=cfg.max_retries, **extra)
        except anthropic.AnthropicError as exc:
            raise LLMUnavailable(f"no Anthropic credentials: set ANTHROPIC_API_KEY (see .env.example): {exc}") from exc
        # The SDK resolves credentials lazily: with none at all, the first call would fail. Say so now, so
        # /health reports it and the kiosk answers from templates without a doomed call per request.
        if self._client.api_key is None and self._client.auth_token is None and getattr(self._client, "credentials", None) is None:
            raise LLMUnavailable("no Anthropic credentials: put ANTHROPIC_API_KEY in .env (see .env.example) or run `ant auth login`")

    def _create(self, system: str, user: str, output_format: dict | None) -> str:
        output_config: dict = {"effort": self._cfg.effort}
        if output_format is not None:
            output_config["format"] = output_format
        kwargs = dict(
            model=self._cfg.model,
            max_tokens=self._cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
        try:
            if self._cfg.anthropic_fallbacks:
                response = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
                )
            else:
                response = self._client.messages.create(**kwargs)
        except self._anthropic.APIError as exc:  # timeouts, connection errors, 4xx/5xx after retries
            self.usage.failures += 1
            raise LLMError(f"Anthropic call failed: {exc.__class__.__name__}: {exc}") from exc
        self.usage.calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:  # billed tokens, including any cached prompt reads/writes
            self.usage.input_tokens += sum(int(getattr(usage, f, 0) or 0) for f in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            self.usage.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        if response.stop_reason == "refusal":
            raise LLMError("Anthropic declined the request (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError("Anthropic reply hit llm.max_tokens")
        return "".join(block.text for block in response.content if block.type == "text")

    def complete(self, system: str, user: str) -> str:
        return self._create(system, user, None)

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> dict:
        fmt = {"type": "json_schema", "schema": json_schema_for(schema)}
        return validate_with_retry(lambda prompt: self._create(system, prompt, fmt), user, schema)


class OpenAIClient:
    def __init__(self, cfg: LLMSettings, secrets: Secrets):
        if secrets.openai_api_key is None:
            raise LLMUnavailable("OPENAI_API_KEY is not set (see .env.example)")
        openai = _import("openai", "openai")
        self._openai = openai
        self._cfg = cfg
        self.usage = Usage()
        self._client = openai.OpenAI(
            api_key=secrets.openai_api_key.get_secret_value(),
            timeout=cfg.timeout_s,
            max_retries=cfg.max_retries,
        )

    def _create(self, system: str, user: str, response_format: dict | None) -> str:
        kwargs = {"response_format": response_format} if response_format else {}
        try:
            response = self._client.chat.completions.create(
                model=self._cfg.model,
                max_completion_tokens=self._cfg.max_tokens,
                temperature=self._cfg.temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                **kwargs,
            )
        except self._openai.OpenAIError as exc:
            self.usage.failures += 1
            raise LLMError(f"OpenAI call failed: {exc.__class__.__name__}: {exc}") from exc
        self.usage.calls += 1
        if response.usage is not None:
            self.usage.input_tokens += response.usage.prompt_tokens or 0
            self.usage.output_tokens += response.usage.completion_tokens or 0
        return response.choices[0].message.content or ""

    def complete(self, system: str, user: str) -> str:
        return self._create(system, user, None)

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> dict:
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "schema": json_schema_for(schema), "strict": True},
        }
        return validate_with_retry(lambda prompt: self._create(system, prompt, fmt), user, schema)


class OllamaClient:
    def __init__(self, cfg: LLMSettings, secrets: Secrets):
        ollama = _import("ollama", "ollama")
        self._ollama = ollama
        self._cfg = cfg
        self.usage = Usage()
        self._client = ollama.Client(host=secrets.ollama_host, timeout=cfg.timeout_s)

    def _create(self, system: str, user: str, fmt: dict | None) -> str:
        kwargs = {"format": fmt} if fmt else {}
        last: Exception | None = None
        for _ in range(self._cfg.max_retries + 1):  # the ollama client has no retry option
            try:
                response = self._client.chat(
                    model=self._cfg.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    options={"temperature": self._cfg.temperature, "num_predict": self._cfg.max_tokens},
                    **kwargs,
                )
                self.usage.calls += 1
                self.usage.input_tokens += int(response.get("prompt_eval_count", 0) or 0)
                self.usage.output_tokens += int(response.get("eval_count", 0) or 0)
                return response["message"]["content"]
            except Exception as exc:  # httpx timeouts, connection refused, ollama.ResponseError
                last = exc
        self.usage.failures += 1
        raise LLMError(f"Ollama call failed: {last.__class__.__name__}: {last}") from last

    def complete(self, system: str, user: str) -> str:
        return self._create(system, user, None)

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> dict:
        return validate_with_retry(lambda prompt: self._create(system, prompt, json_schema_for(schema)), user, schema)


_ADAPTERS = {"anthropic": AnthropicClient, "openai": OpenAIClient, "ollama": OllamaClient}


def make_llm_client(settings: Settings) -> LLMClient:
    """The client for ``llm.provider``. Raises LLMUnavailable if it can't be used."""
    return _ADAPTERS[settings.llm.provider](settings.llm, settings.secrets)


class FakeLLMClient:
    """Offline stand-in for tests and eval: replies come from a function of (system, user).

    ``reply`` returns the raw text the model would have produced (or raises, to simulate an outage).
    ``generate`` goes through the same parse + validate + retry-once path as the real adapters.
    """

    def __init__(self, reply: Callable[[str, str], str]):
        self._reply = reply
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._reply(system, user)

    def generate(self, system: str, user: str, schema: type[BaseModel]) -> dict:
        return validate_with_retry(lambda prompt: self.complete(system, prompt), user, schema)
