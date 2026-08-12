"""The tool-use loop.

Backend-agnostic: it asks for a reply, runs whatever tools come back, feeds the
results in, and repeats. It never learns which runtime produced the tokens.

Two properties matter more than anything else here:

* **Tool failures come back as tool results, not exceptions.** A model that
  passes a bad player_id should see the error and re-resolve the name, which is
  exactly what the Phase 4 tools were built to support by returning
  `{"error", "detail"}` instead of raising. Crashing the turn would throw away a
  recoverable mistake — and small models make more of them.
* **The iteration cap is hard.** Local inference has no per-token cost, so a
  runaway loop costs time rather than money, but an agent stuck calling the same
  tool forever is still broken and should surface as such.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from advisor.agent.backend import Backend, BackendError, Reply, ToolCall
from advisor.agent.prompt import build_system_prompt
from advisor.context import LeagueContext
from advisor.tools import REGISTRY, TOOLS
from advisor.tools.registry import coerce_arguments

log = logging.getLogger(__name__)

MAX_ITERATIONS = 8

# Tool results are already capped near 1500 tokens by the tool layer. This is a
# backstop against a future tool that forgets to truncate.
MAX_RESULT_CHARS = 8000


@dataclass
class Turn:
    """What one user message produced, for display and for evals."""

    text: str
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    iterations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0
    hit_iteration_cap: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def tools_used(self) -> list[str]:
        return [name for name, _ in self.tool_calls]

    def summary(self) -> str:
        return (
            f"[{self.iterations} iter | {len(self.tool_calls)} tools | "
            f"{self.prompt_tokens}+{self.completion_tokens} tok | "
            f"{self.elapsed_seconds:.1f}s]"
        )


def _serialise(result: Any) -> str:
    text = json.dumps(result, default=str)
    if len(text) > MAX_RESULT_CHARS:
        return text[:MAX_RESULT_CHARS] + '... [truncated]"}'
    return text


def _run_tool(call: ToolCall, ctx: LeagueContext) -> tuple[str, bool]:
    """Execute one tool call. Returns (payload, is_error).

    Every failure path returns a *result* the model can read and act on. The
    model chose the tool and the arguments, so it is the only thing that can
    correct them.
    """
    handler: Callable[..., dict] | None = REGISTRY.get(call.name)
    if handler is None:
        return (
            json.dumps(
                {
                    "error": f"no tool named {call.name!r}",
                    "detail": f"available tools: {sorted(REGISTRY)}",
                }
            ),
            True,
        )

    arguments = dict(call.arguments)
    # Bind the session. The league, the week, and the user's roster and intent
    # are all session state — passing the live context (rather than letting each
    # tool rebuild one from an id) is what keeps a session set to week 14 and
    # intent=contend from being answered as week 18 and intent=balanced.
    # Anything the model invented for these is discarded on purpose.
    for session_owned in ("league_id", "ctx"):
        arguments.pop(session_owned, None)
    # Fit "8" to 8 and a bare string to a list before dispatch. A small model
    # getting the type wrapper wrong is not the same as getting the call wrong.
    arguments = coerce_arguments(call.name, arguments)
    arguments["ctx"] = ctx

    try:
        return _serialise(handler(**arguments)), False
    except TypeError as exc:
        # Wrong or missing arguments — the most common small-model failure.
        return (
            json.dumps(
                {
                    "error": f"bad arguments for {call.name}",
                    "detail": f"{exc}. Check the tool's required parameters.",
                }
            ),
            True,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
        log.warning("tool %s raised: %s", call.name, exc)
        return (
            json.dumps({"error": f"{call.name} failed", "detail": str(exc)}),
            True,
        )


def run_turn(
    backend: Backend,
    ctx: LeagueContext,
    messages: list[dict[str, Any]],
    *,
    verbose: bool = False,
    on_event: Callable[[str], None] = lambda _: None,
    on_token: Callable[[str], None] | None = None,
    on_tool: Callable[[str], None] | None = None,
) -> Turn:
    """Run one user message to completion, mutating `messages` with the history.

    `messages` is the running conversation and is updated in place so the caller
    keeps full history across turns.

    Pass `on_token` to stream the answer as it is generated, and `on_tool` to be
    told which tool is running. Both exist for the web UI: a local turn takes
    tens of seconds, and silence for that long reads as a hang.
    """
    system = build_system_prompt(ctx)
    turn = Turn(text="")
    started = time.monotonic()

    for iteration in range(1, MAX_ITERATIONS + 1):
        turn.iterations = iteration

        try:
            # Stream only when someone is listening, and only when the backend
            # can — the Backend protocol guarantees chat(), not chat_stream().
            streamer = getattr(backend, "chat_stream", None)
            if on_token is not None and streamer is not None:
                reply: Reply = streamer(system, messages, TOOLS, on_token)
            else:
                reply = backend.chat(system, messages, TOOLS)
        except BackendError as exc:
            turn.errors.append(str(exc))
            turn.text = f"The model backend failed:\n{exc}"
            break

        turn.prompt_tokens += reply.prompt_tokens
        turn.completion_tokens += reply.completion_tokens

        if verbose:
            thinking = reply.detail.get("thinking")
            if thinking:
                on_event(f"  thinking: {thinking[:300]}")
            if reply.text:
                on_event(f"  says: {reply.text[:300]}")

        assistant: dict[str, Any] = {"role": "assistant", "content": reply.text}
        if reply.wants_tools:
            assistant["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ]
        messages.append(assistant)

        if not reply.wants_tools:
            turn.text = reply.text
            break

        for call in reply.tool_calls:
            turn.tool_calls.append((call.name, call.arguments))
            if on_tool is not None:
                on_tool(call.name)
            if verbose:
                on_event(f"  -> {call.name}({json.dumps(call.arguments, default=str)})")

            payload, is_error = _run_tool(call, ctx)
            if is_error:
                turn.errors.append(f"{call.name}: {payload[:200]}")
            if verbose:
                marker = "!!" if is_error else "<-"
                on_event(f"  {marker} {payload[:400]}")

            messages.append(
                {"role": "tool", "tool_name": call.name, "content": payload}
            )
    else:
        turn.hit_iteration_cap = True
        turn.text = (
            "I hit the tool-call limit without reaching an answer. "
            "Try asking a narrower question."
        )

    turn.elapsed_seconds = time.monotonic() - started
    return turn


def new_backend() -> Backend:
    """The configured backend, health-checked before first use."""
    from advisor.agent.ollama import OllamaBackend

    backend = OllamaBackend()
    backend.health()
    return backend
