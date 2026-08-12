"""Local tool-use agent loop. Phase 5.

Inference runs on the user's own machine through Ollama — no API key, no
per-token cost, no rate limit. `backend.py` is the seam that keeps the loop and
the tool layer independent of which runtime is generating tokens.
"""

from advisor.agent.backend import Backend, BackendError, Reply, ToolCall
from advisor.agent.loop import MAX_ITERATIONS, Turn, new_backend, run_turn
from advisor.agent.ollama import OllamaBackend
from advisor.agent.prompt import build_system_prompt

__all__ = [
    "MAX_ITERATIONS",
    "Backend",
    "BackendError",
    "OllamaBackend",
    "Reply",
    "ToolCall",
    "Turn",
    "build_system_prompt",
    "new_backend",
    "run_turn",
]
