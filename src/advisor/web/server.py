"""Local web UI. FastAPI bound to 127.0.0.1, serving one page.

**Localhost is the product decision, not a limitation.** The moment this serves
other people from one machine, that machine's GPU is doing everyone's inference
and someone is paying for it — which is the exact cost model the whole project
pivoted away from. So: no public bind, no auth, no accounts, no Docker.

The HTTP layer is stateless. Every request reloads its conversation from DuckDB
rather than holding it in memory, so a refresh, a second tab, and a server
restart all behave the same.
"""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from advisor.agent import run_turn
from advisor.agent.backend import BackendError
from advisor.config import get_settings
from advisor.context import list_leagues, load_context
from advisor.db import query
from advisor.warehouse import conversations

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="ffcompanion", docs_url=None, redoc_url=None)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    league_id: str | None = None
    week: int | None = None


def _backend():
    """Built per request, so switching MODEL doesn't need a restart."""
    from advisor.agent.ollama import OllamaBackend

    backend = OllamaBackend()
    backend.health()
    return backend


def _resolve_league(league_id: str | None) -> tuple[str, int | None]:
    """Reuse the CLI's picker rather than write a second one.

    Two implementations of "which league am I answering about" would drift, and
    the roster_id it returns is session state the model must never guess at.
    """
    from advisor.cli import _pick_league

    try:
        return _pick_league(league_id)
    except LookupError as exc:
        # A named league that isn't linked is a missing resource; nothing linked
        # at all is the server not being set up yet.
        status = 404 if league_id else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    try:
        _backend()
        model_ok, model_error = True, None
    except BackendError as exc:
        model_ok, model_error = False, str(exc)

    return {
        "ok": model_ok,
        "model": settings.model,
        "thinking": settings.thinking,
        "context_tokens": settings.context_tokens,
        # Stated plainly because it is the whole point of the architecture.
        "cost": "$0.00 — inference runs on this machine",
        "error": model_error,
    }


@app.get("/data-status")
def data_status() -> dict:
    ingested = query(
        """
        SELECT season, MAX(fetched_at) AS fetched_at, SUM(row_count) AS row_count
        FROM ingest_log GROUP BY season ORDER BY season
        """
    )
    leagues = query(
        "SELECT league_id, name, season, format FROM leagues ORDER BY season DESC"
    )
    return {"seasons": ingested, "leagues": leagues}


@app.get("/leagues")
def get_leagues() -> dict:
    """Linked leagues, best default first — the same order the picker uses.

    An empty list with a 200 rather than an error: nothing linked yet is a
    setup state the page can explain, not a server fault.
    """
    return {"leagues": list_leagues()}


@app.get("/conversations")
def list_conversations(league_id: str | None = None) -> dict:
    return {"conversations": conversations.recent(league_id)}


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str) -> dict:
    if not conversations.exists(conversation_id):
        raise HTTPException(status_code=404, detail="no such conversation")
    return {"messages": conversations.history(conversation_id)}


@app.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str) -> dict:
    conversations.delete(conversation_id)
    return {"deleted": conversation_id}


@app.post("/chat")
def chat(request: ChatRequest) -> StreamingResponse:
    """One turn, streamed as server-sent events.

    Events: `tool` when one is called, `token` for each chunk of the answer,
    `done` with the summary, `error` if the backend fails. The tool events are
    not decoration — this app's premise is that numbers are traceable, so
    showing which tools ran is part of the answer.
    """
    conversation_id = request.conversation_id
    if conversation_id:
        # A thread is pinned to the league it was created in. Re-running the
        # picker here is what let a conversation silently migrate when the
        # default moved, leaving league A's history in front of league B's
        # system prompt.
        pinned = conversations.league_of(conversation_id)
        if pinned is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        if request.league_id and request.league_id != pinned["league_id"]:
            raise HTTPException(
                status_code=409,
                detail="this thread is about another league; start a new one to switch",
            )
        league_id, roster_id = pinned["league_id"], pinned["roster_id"]
    else:
        league_id, roster_id = _resolve_league(request.league_id)

    # Resolved out here, not inside the generator. A LookupError in `stream()`
    # killed the response after a 200 had already gone out, which a browser can
    # only render as an answer that stops mid-sentence.
    try:
        ctx = load_context(league_id, roster_id=roster_id, current_week=request.week)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Created only once resolution has succeeded, so a failure leaves no empty
    # thread behind.
    if not conversation_id:
        conversation_id = conversations.create(league_id, roster_id)

    def stream():
        # First, before anything that can fail. A dead Ollama then still tells
        # you which league you asked about and hands back a conversation_id to
        # retry into, instead of orphaning the turn.
        yield _sse(
            "start",
            {
                "conversation_id": conversation_id,
                "league_id": ctx.league_id,
                "league": ctx.name,
                "roster_id": ctx.roster_id,
                "week": ctx.current_week,
            },
        )

        try:
            backend = _backend()
        except BackendError as exc:
            yield _sse("error", {"message": str(exc)})
            return

        # History first, then this message — the model needs the thread, and the
        # database is where the thread lives.
        messages = conversations.for_model(conversation_id)
        messages.append({"role": "user", "content": request.message})
        conversations.append(conversation_id, "user", request.message)

        # run_turn blocks, so it runs on its own thread and pushes events to a
        # queue this generator drains. Collecting everything first and emitting
        # at the end would be a spinner wearing a stream's clothes — the user
        # would still wait the full turn before seeing anything, which is the
        # exact thing streaming is here to fix.
        events: queue.Queue = queue.Queue()
        FINISHED = object()

        def work() -> None:
            try:
                turn = run_turn(
                    backend,
                    ctx,
                    messages,
                    on_token=lambda piece: events.put(("token", {"text": piece})),
                    on_tool=lambda name: events.put(("tool", {"name": name})),
                )
                conversations.append(
                    conversation_id, "assistant", turn.text, tools_used=turn.tools_used
                )
                events.put((
                    "done",
                    {
                        "conversation_id": conversation_id,
                        "text": turn.text,
                        "tools": turn.tools_used,
                        "seconds": round(turn.elapsed_seconds, 1),
                        "tokens": turn.prompt_tokens + turn.completion_tokens,
                        "hit_cap": turn.hit_iteration_cap,
                    },
                ))
            except Exception as exc:  # noqa: BLE001 - a failed turn is an event
                events.put(("error", {"message": f"{type(exc).__name__}: {exc}"}))
            finally:
                events.put(FINISHED)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        while True:
            item = events.get()
            if item is FINISHED:
                break
            name, data = item
            yield _sse(name, data)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")
