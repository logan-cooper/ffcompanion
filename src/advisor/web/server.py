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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from advisor.agent import run_turn
from advisor.agent.backend import BackendError
from advisor.config import get_settings
from advisor.context import (
    current_nfl_season,
    list_leagues,
    load_context,
    sleeper_identity,
)
from advisor.db import query
from advisor.warehouse import conversations

STATIC = Path(__file__).resolve().parent / "static"

# Seasons offered when looking a username up. `current + 1` is deliberate: a
# dynasty league rolls over on Sleeper months before its season starts, so from
# February to September the league you actually play in is next year's.
SEASONS_AHEAD = 1
SEASONS_BACK = 2


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Make sure the tables exist before the first request.

    The page is a setup surface now: clone this, `make serve`, and link a league
    from the browser. On a fresh clone nothing has created the schema yet, so
    without this the first thing a newcomer sees is a page whose every request
    500s on a missing table — while the fix is a make target they have not been
    told about.

    Idempotent DDL. If the file is locked by another process this raises at
    startup, which is the right place to find out: with the database unavailable
    no endpoint works, and DuckDB's own message names the process holding it.
    """
    from advisor.warehouse.schema import create_schema

    create_schema()
    yield


app = FastAPI(
    title="ffcompanion", docs_url=None, redoc_url=None, lifespan=lifespan
)


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    league_id: str | None = None
    week: int | None = None


class LinkRequest(BaseModel):
    username: str
    season: int
    league_id: str


class RefreshRequest(BaseModel):
    league_id: str | None = None
    force: bool = False


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
    setup state the page can explain, not a server fault. The identity and the
    season list ride along so the setup panel can prefill itself instead of
    making someone retype a name the app already knows.
    """
    username, _ = sleeper_identity()
    season = current_nfl_season()
    return {
        "leagues": list_leagues(),
        "username": username,
        "season": season,
        "seasons": list(range(season + SEASONS_AHEAD, season - SEASONS_BACK - 1, -1)),
    }


@app.get("/sleeper/leagues")
def sleeper_leagues(username: str, season: int | None = None) -> dict:
    """Every league a Sleeper username plays in, straight from Sleeper.

    Read-only, and writes nothing — this is the "show me what is there" half of
    linking, so a typo costs one request rather than a half-ingested league.

    Format is detected from the raw league object rather than after ingest,
    which is what makes the choice meaningful: dynasty and redraft get
    materially different advice, and the person picking should see which is
    which before they commit to one.
    """
    from advisor.league_format import detect_format
    from advisor.sources import sleeper
    from advisor.sources.sleeper import SleeperError

    username = username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="a Sleeper username is required")

    season = season or current_nfl_season()

    try:
        user = sleeper.get_user(username)
        # Sleeper answers `null` rather than 404 for an unknown user, so this is
        # the only signal that a name was mistyped.
        if not user:
            raise HTTPException(
                status_code=404,
                detail=f"Sleeper has no user named {username!r}. "
                "It is the username on your profile, not your team name.",
            )
        found = sleeper.get_user_leagues(user["user_id"], season)
    except SleeperError as exc:
        # Sleeper being unreachable is not a bad username, and saying so is what
        # stops someone retyping a name that was right all along.
        raise HTTPException(
            status_code=502, detail=f"Could not reach Sleeper: {exc}"
        ) from exc

    linked = {row["league_id"] for row in query("SELECT league_id FROM leagues")}

    leagues = []
    for league in found:
        detection = detect_format(league)
        leagues.append(
            {
                "league_id": league["league_id"],
                "name": (league.get("name") or "").strip(),
                "season": season,
                "format": detection.format,
                "superflex": detection.superflex,
                "total_rosters": league.get("total_rosters"),
                "linked": league["league_id"] in linked,
            }
        )

    return {
        "username": user.get("display_name") or username,
        "user_id": user["user_id"],
        "season": season,
        "leagues": leagues,
    }


@app.post("/leagues/link")
def link_league(request: LinkRequest) -> dict:
    """Pull one league into the warehouse and make it selectable.

    Slow on a cold cache — Sleeper's player dump is ~14MB — so the page says so
    before starting rather than looking hung. Runs in FastAPI's threadpool, so a
    long link does not block the rest of the server.

    Idempotent: `link_leagues` deletes a league's rows before rewriting them, and
    `team_intent` lives in its own table, so re-linking refreshes rosters without
    losing anything the user set.
    """
    from advisor.sources.sleeper import SleeperError
    from advisor.warehouse.leagues import link_leagues

    try:
        link_leagues(request.username, request.season, league_id=request.league_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SleeperError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach Sleeper: {exc}"
        ) from exc

    # Read the league back through the same ordering the dropdown renders, so
    # the row the page adds is the row the picker would choose.
    linked = next(
        (row for row in list_leagues() if row["league_id"] == request.league_id), None
    )
    if linked is None:  # pragma: no cover - link_leagues raises before this
        raise HTTPException(status_code=500, detail="league did not persist")

    username, _ = sleeper_identity()
    return {"league": linked, "username": username}


# ------------------------------------------------------------------- panels
#
# What the browsing columns read. Each resolves a context the same way `/chat`
# does, so the roster in the sidebar is the roster the answer is about.


def _panel_context(league_id: str | None):
    """The session's own context — always yours, never the team being viewed.

    The context carries *who is asking*: `roster_id` is the user's team and
    `team_intent` is the user's stance, which is what the valuation is weighted
    by. Building it around whichever roster is being looked at would mark an
    opponent's team as yours and value their players under their intent. Which
    roster to *display* is a separate argument, exactly as it is for
    `get_my_roster`.
    """
    resolved_id, roster_id = _resolve_league(league_id)
    try:
        return load_context(resolved_id, roster_id=roster_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/league/refresh")
def refresh(request: RefreshRequest) -> dict:
    """Pull this league's rosters and free agents from Sleeper.

    Called on every page load and every league switch, which is the point:
    rosters move on trades and waiver claims, and advice about a team the user
    no longer has is worse than a slow page.

    Never fails the request. Sleeper being unreachable comes back as
    `ok: false` with a reason, because the warehouse copy is a complete working
    answer that is merely older — and this app is supposed to work offline.
    """
    from advisor.warehouse.refresh import refresh_league

    league_id, _ = _resolve_league(request.league_id)
    return refresh_league(league_id, force=request.force).as_dict()


@app.get("/panel/roster")
def panel_roster(league_id: str | None = None, roster_id: int | None = None) -> dict:
    """One team in full. `roster_id` omitted means yours."""
    from advisor.web.panels import roster_panel

    return roster_panel(_panel_context(league_id), roster_id)


@app.get("/panel/standings")
def panel_standings(league_id: str | None = None) -> dict:
    from advisor.web.panels import standings_panel

    return standings_panel(_panel_context(league_id))


@app.get("/panel/waivers")
def panel_waivers(
    league_id: str | None = None,
    position: str | None = None,
    limit: int | None = None,
) -> dict:
    from advisor.web.panels import WAIVER_LIMIT, waivers_panel

    ctx = _panel_context(league_id)
    return waivers_panel(ctx, position, limit or WAIVER_LIMIT)


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
