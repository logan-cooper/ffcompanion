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


class OllamaBackend:
    """Chat completions against a local Ollama server."""

    def __init__(self, model: str | None = None, host: str | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.model
        self.host = (host or settings.ollama_host).rstrip("/")
        self.name = f"ollama:{self.model}"

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

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Reply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": TEMPERATURE},
        }
        if tools:
            payload["tools"] = to_ollama_tools(tools)

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

        message = body.get("message") or {}
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
                # Reasoning models (qwen3 among them) return their scratchpad
                # in a separate field. Useful under --verbose, but it is NOT the
                # answer — never render it to the user as one.
                "thinking": (message.get("thinking") or "").strip(),
                "load_ms": int((body.get("load_duration") or 0) / 1_000_000),
                "total_ms": int((body.get("total_duration") or 0) / 1_000_000),
            },
        )
