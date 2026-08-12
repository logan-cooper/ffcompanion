"""Ollama backend — the only module in the app that speaks HTTP to a model.

Runs against a local server, so there is no API key, no per-token cost, and no
rate limit. The tradeoff is that the runtime is a separate process the user has
to start, which is why `health()` exists and why its messages name the exact
command to run.

Tool schemas are translated here. `tools/registry.py` stays in its own format;
turning that into whatever the runtime wants is a backend concern, and keeping
it here means swapping runtimes never touches the tool layer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from advisor.agent.backend import BackendError, Reply, ToolCall
from advisor.config import get_settings

log = logging.getLogger(__name__)

# Local inference on a 7-8B model is slower than a hosted API, and the first
# call after a cold start also pays model-load time. Generous on purpose.
REQUEST_TIMEOUT = 300
HEALTH_TIMEOUT = 5

# Keep the model resident between turns; without this a REPL pays several
# seconds of reload on every message.
KEEP_ALIVE = "10m"

# Low but non-zero. Tool selection wants determinism; a hard 0 makes some
# models loop on the same wrong call instead of trying another.
TEMPERATURE = 0.1

# Temperature 0.1 is not deterministic, and a 12-case suite is small enough that
# sampling noise flips individual cases in both directions between runs. That is
# survivable when evals catch regressions; it is disqualifying when evals PICK
# THE MODEL, because a one-run-each comparison would partly measure luck.
# Pinning the seed makes a rerun reproducible, so a case that changes between
# two models reflects the models. It samples one draw rather than an average —
# the honest fix for that is more cases, not a floating seed.
EVAL_SEED = 7

# Ollama defaults to a 4096-token window, and it does NOT error when a request
# exceeds it — the runtime silently drops tokens and answers from what's left.
# That is a quiet correctness bug here, not a performance one: the system prompt
# holds the rule "never state a statistic that did not come from a tool result",
# so a turn that overflows can lose the very instruction that keeps numbers
# honest. Six tool schemas plus the prompt already cost ~2k tokens before the
# user has asked anything, and one roster result can cost as much again.
#
# 8192 is the default because it fits the 16GB floor this app targets (~1.2GB of
# KV cache for an 8B model, on top of ~5.5GB of weights). Raise it with
# CONTEXT_TOKENS if you have the headroom.
MIN_CONTEXT_TOKENS = 8192


def to_ollama_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate the registry's schemas into the runtime's function format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as a dict or as a JSON string, depending on model.

    A model that emits a string here is not malformed — it is common enough that
    treating it as an error would reject usable calls.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _to_reply(body: dict[str, Any], message: dict[str, Any]) -> Reply:
    """Build a Reply from a finished response, streamed or not.

    Shared so the two paths cannot drift: a tool call parsed one way in
    streaming and another way outside it would be a genuinely nasty bug.
    """
    calls = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        calls.append(
            ToolCall(
                # Ollama does not always assign an id; the loop needs one to
                # pair results with calls, so synthesise a stable fallback.
                id=str(call.get("id") or f"call_{index}"),
                name=name,
                arguments=_coerce_arguments(function.get("arguments")),
            )
        )

    return Reply(
        text=(message.get("content") or "").strip(),
        tool_calls=tuple(calls),
        prompt_tokens=int(body.get("prompt_eval_count") or 0),
        completion_tokens=int(body.get("eval_count") or 0),
        detail={
            "done_reason": body.get("done_reason"),
            # Reasoning models (qwen3 among them) return their scratchpad in a
            # separate field. Useful under --verbose, but it is NOT the answer —
            # never render it to the user as one.
            "thinking": (message.get("thinking") or "").strip(),
            "load_ms": int((body.get("load_duration") or 0) / 1_000_000),
            "total_ms": int((body.get("total_duration") or 0) / 1_000_000),
        },
    )


class OllamaBackend:
    """Chat completions against a local Ollama server."""

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
        *,
        seed: int | None = None,
        think: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.model = model or settings.model
        self.host = (host or settings.ollama_host).rstrip("/")
        self.context_tokens = settings.context_tokens
        # Reasoning models emit a thinking block before answering. It is where
        # qwen3:8b spends its time — 342s on one start/sit question — so it is
        # switchable and the eval suite decides whether it earns that.
        # Harmless on models that do not reason; verified against llama3.1:8b.
        self.think = settings.thinking if think is None else think
        self.name = f"ollama:{self.model}" + ("" if self.think else " (no-think)")
        # Chat leaves this None: a user who rephrases a question and gets the
        # identical answer back is being failed by a frozen seed.
        self.seed = seed

    # ------------------------------------------------------------------ health

    def health(self) -> None:
        """Verify the server is up and the model is pulled.

        These two failures account for essentially every first run, so each gets
        the command that fixes it rather than a traceback.
        """
        try:
            response = requests.get(f"{self.host}/api/version", timeout=HEALTH_TIMEOUT)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BackendError(
                f"Cannot reach Ollama at {self.host}.\n"
                "  Start it with:  ollama serve\n"
                "  Install it with: brew install ollama"
            ) from exc

        if self.model not in self.installed_models():
            raise BackendError(
                f"Model {self.model!r} is not pulled.\n"
                f"  Pull it with:  ollama pull {self.model}\n"
                "  See what you have:  ollama list"
            )

    def installed_models(self) -> set[str]:
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=HEALTH_TIMEOUT)
            response.raise_for_status()
            models = response.json().get("models") or []
        except (requests.RequestException, ValueError):
            return set()

        names: set[str] = set()
        for entry in models:
            name = entry.get("name") or entry.get("model") or ""
            if name:
                names.add(name)
                # `ollama list` shows "qwen3:8b"; accept a bare "qwen3" too.
                names.add(name.split(":")[0])
        return names

    # -------------------------------------------------------------------- chat

    def _payload(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": stream,
            "think": self.think,
            "keep_alive": KEEP_ALIVE,
            "options": {
                "temperature": TEMPERATURE,
                "num_ctx": max(self.context_tokens, MIN_CONTEXT_TOKENS),
                **({} if self.seed is None else {"seed": self.seed}),
            },
        }
        if tools:
            payload["tools"] = to_ollama_tools(tools)
        return payload

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Reply:
        payload = self._payload(system, messages, tools, stream=False)

        try:
            response = requests.post(
                f"{self.host}/api/chat", json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise BackendError(f"Ollama request failed: {exc}") from exc

        if response.status_code >= 400:
            raise BackendError(
                f"Ollama returned {response.status_code}: {response.text[:400]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise BackendError("Ollama returned a non-JSON response") from exc

        return _to_reply(body, body.get("message") or {})

    def chat_stream(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_token,
    ) -> Reply:
        """Same call, with content forwarded chunk by chunk as it arrives.

        Streaming matters more locally than it would against a hosted API: a
        turn takes tens of seconds, and a spinner that long reads as broken
        where the same wait with text appearing does not.

        Every iteration streams, including ones that turn out to be tool calls.
        A model's preamble ("let me look that up") is worth showing, and the
        alternative — waiting to find out whether tool_calls appear before
        forwarding anything — gives back the latency streaming was for.
        """
        payload = self._payload(system, messages, tools, stream=True)

        try:
            response = requests.post(
                f"{self.host}/api/chat",
                json=payload,
                timeout=REQUEST_TIMEOUT,
                stream=True,
            )
        except requests.RequestException as exc:
            raise BackendError(f"Ollama request failed: {exc}") from exc

        if response.status_code >= 400:
            raise BackendError(
                f"Ollama returned {response.status_code}: {response.text[:400]}"
            )

        # Ollama streams newline-delimited JSON, one object per chunk, with the
        # final object carrying the token counts and timings.
        content: list[str] = []
        thinking: list[str] = []
        tool_calls: list[dict] = []
        final: dict[str, Any] = {}

        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue  # a partial line is not worth failing a whole turn over

            message = chunk.get("message") or {}
            if piece := message.get("content"):
                content.append(piece)
                on_token(piece)
            if piece := message.get("thinking"):
                thinking.append(piece)
            if calls := message.get("tool_calls"):
                tool_calls.extend(calls)
            if chunk.get("done"):
                final = chunk

        assembled = {
            "content": "".join(content),
            "thinking": "".join(thinking),
            "tool_calls": tool_calls,
        }
        return _to_reply(final, assembled)
