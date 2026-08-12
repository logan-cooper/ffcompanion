"""The inference backend interface.

One seam, so the agent loop never learns what is generating the tokens. Today
that is a local Ollama server; if this ever moves to a hosted runtime, only
`ollama.py` gets a sibling — `loop.py`, `prompt.py`, and the whole tool layer
stay untouched.

Deliberately not modelled on any vendor's SDK: `ToolCall` and `Reply` carry only
what the loop actually needs, which keeps a backend swap from leaking provider
shapes into the rest of the app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """The backend could not produce a reply.

    Raised with an actionable message — a local runtime that isn't started, or a
    model that hasn't been pulled, is the single most likely first-run failure
    and deserves better than a connection traceback.
    """


@dataclass(frozen=True)
class ToolCall:
    """A request from the model to run one tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Reply:
    """One turn from the model.

    `text` and `tool_calls` are not exclusive — some models narrate before
    calling a tool, and that narration is worth showing in verbose mode.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@runtime_checkable
class Backend(Protocol):
    """Anything that can turn a conversation plus tool schemas into a reply."""

    name: str

    def health(self) -> None:
        """Raise `BackendError` with a fixable message if unusable."""
        ...

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> Reply:
        ...
