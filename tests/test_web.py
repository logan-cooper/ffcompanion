"""Local web UI.

These tests cover the HTTP contract and the SSE framing without calling a model,
so they run in a second and work while Ollama is not running. What they cannot
cover is a real streamed turn — that needs the model and the database, and is
verified by hand against `make serve`.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from advisor.web.server import _sse, app


@pytest.fixture
def client():
    return TestClient(app)


# ------------------------------------------------------------------ SSE framing

def test_sse_frames_are_wellformed():
    """A frame the browser cannot parse loses the whole turn, and the framing is
    hand-rolled on both ends, so it is worth pinning."""
    frame = _sse("token", {"text": "hello"})

    assert frame.startswith("event: token\n")
    assert frame.endswith("\n\n"), "frames are separated by a blank line"
    payload = frame.split("data: ", 1)[1].strip()
    assert json.loads(payload) == {"text": "hello"}


def test_sse_survives_content_that_looks_like_framing():
    """Model output containing newlines must not be read as a frame boundary —
    JSON encoding is what prevents that, so assert it rather than assume it."""
    frame = _sse("token", {"text": "line one\n\nline two"})

    assert frame.count("\n\n") == 1, "the only blank line is the terminator"
    body = json.loads(frame.split("data: ", 1)[1].strip())
    assert body["text"] == "line one\n\nline two"


# --------------------------------------------------------------------- routing

def test_the_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "<title>ffcompanion</title>" in response.text


def test_the_page_needs_no_build_step_and_no_network(client):
    """One file, no framework, no CDN. A leaguemate should be able to run this
    offline after the model is pulled."""
    page = client.get("/").text
    assert "<script" in page and "src=" not in page.split("<script")[1][:200]
    for remote in ("http://", "https://", "cdn."):
        assert remote not in page, f"page reaches out to {remote}"


def test_health_reports_model_and_zero_cost(client):
    body = client.get("/health").json()

    assert body["model"]
    assert body["context_tokens"] >= 8192
    # Stated in the payload because it is the point of the whole architecture.
    assert "$0.00" in body["cost"]


def test_health_explains_a_missing_backend_rather_than_500ing(client, monkeypatch):
    """Ollama not running is THE first-run failure. It has to arrive as an
    explanation with a fix in it, not a stack trace."""
    from advisor.agent.backend import BackendError

    def refuse():
        raise BackendError("Cannot reach Ollama at http://localhost:11434.\n  Start it with:  ollama serve")

    monkeypatch.setattr("advisor.web.server._backend", refuse)
    body = client.get("/health").json()

    assert body["ok"] is False
    assert "ollama serve" in body["error"]


def test_unknown_conversation_is_a_404_not_a_new_thread(client):
    """Silently starting a fresh thread would look like the history vanished."""
    response = client.get("/conversations/does-not-exist")
    assert response.status_code == 404


def test_chat_rejects_an_unknown_conversation(client):
    response = client.post(
        "/chat", json={"message": "hi", "conversation_id": "nope"}
    )
    assert response.status_code == 404


def test_chat_requires_a_message(client):
    assert client.post("/chat", json={}).status_code == 422


# ------------------------------------------------------- queries against the DB

def test_data_status_query_actually_runs(client):
    """Hits the real schema. Both bugs found by hand here were invented
    identifiers — a `rosters` table and a `rows` column, neither of which
    exists — and only executing the SQL catches that."""
    response = client.get("/data-status")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "seasons" in body and "leagues" in body
    for season in body["seasons"]:
        assert {"season", "fetched_at", "row_count"} <= set(season)


def test_listing_conversations_runs(client):
    response = client.get("/conversations")
    assert response.status_code == 200
    assert "conversations" in response.json()


def test_a_conversation_round_trips_through_the_database(client):
    """The HTTP layer is stateless, so this is what makes a refresh keep the
    thread."""
    from advisor.warehouse import conversations

    league = client.get("/data-status").json()["leagues"]
    if not league:
        pytest.skip("no league linked")

    cid = conversations.create(league[0]["league_id"], 1)
    conversations.append(cid, "user", "How is Puka Nacua?")
    conversations.append(cid, "assistant", "1793 yards.", tools_used=["resolve_player"])
    try:
        body = client.get(f"/conversations/{cid}").json()
        assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
        assert body["messages"][1]["tools_used"] == "resolve_player"
    finally:
        conversations.delete(cid)

    assert client.get(f"/conversations/{cid}").status_code == 404


# ------------------------------------------------------------------ no cloud

def test_nothing_in_the_app_binds_beyond_localhost():
    """Serving other people from one machine is what the pivot was away from —
    that machine's GPU would be doing everyone's inference."""
    from pathlib import Path

    makefile = Path("Makefile").read_text()
    serve_line = next(l for l in makefile.splitlines() if "uvicorn" in l)
    assert "127.0.0.1" in serve_line
    assert "0.0.0.0" not in serve_line


# ------------------------------------------------------------- league selection

@pytest.fixture
def two_leagues(tmp_path, monkeypatch):
    """A hermetic two-league database, so these run on a fresh clone."""
    from advisor import db
    from advisor.config import get_settings
    from advisor.db import query
    from advisor.warehouse.schema import create_schema

    db.close_conn()
    db.get_conn(tmp_path / "web.duckdb")
    create_schema()
    monkeypatch.setattr(get_settings(), "sleeper_username", "tester", raising=False)

    for league_id, name in (("A", "Alpha Dynasty"), ("B", "Bravo Dynasty")):
        query(
            """
            INSERT INTO leagues (league_id, season, name, status, total_rosters,
                                 sleeper_type, format, format_source, superflex,
                                 has_taxi, is_continuation, roster_positions,
                                 scoring_settings, settings, fetched_at)
            VALUES (?, 2025, ?, 'complete', 12, 2, 'dynasty', 'settings.type',
                    false, false, false, '[]', '{}', '{}', now())
            """,
            [league_id, name],
        )
        query(
            "INSERT INTO league_users (league_id, user_id, display_name, team_name) "
            "VALUES (?, ?, 'tester', NULL)",
            [league_id, f"U-{league_id}"],
        )
        query(
            "INSERT INTO league_rosters (league_id, roster_id, owner_id, players, "
            "starters, taxi, reserve, wins, losses, ties, fpts) "
            "VALUES (?, 1, ?, '[]', '[]', '[]', '[]', 0, 0, 0, 0.0)",
            [league_id, f"U-{league_id}"],
        )
    yield query
    db.close_conn()


def _frames(response):
    """Parse an SSE body into (event, data) pairs."""
    out = []
    for frame in response.text.split("\n\n"):
        lines = frame.splitlines()
        event = next((l[7:] for l in lines if l.startswith("event: ")), None)
        data = next((l[6:] for l in lines if l.startswith("data: ")), None)
        if event and data:
            out.append((event, json.loads(data)))
    return out


@pytest.fixture
def dead_model(monkeypatch):
    """No Ollama. Everything about league selection is testable without one."""
    from advisor.agent.backend import BackendError

    def refuse():
        raise BackendError("Cannot reach Ollama. Start it with: ollama serve")

    monkeypatch.setattr("advisor.web.server._backend", refuse)


def test_leagues_lists_what_the_ui_needs_to_switch(client, two_leagues):
    """Runs the real SQL against the real schema — the only thing that catches
    an invented column, which has bitten twice."""
    body = client.get("/leagues").json()

    assert len(body["leagues"]) == 2
    for row in body["leagues"]:
        assert {"league_id", "name", "season", "format", "roster_id"} <= set(row)


def test_the_default_league_is_the_first_one_offered(client, two_leagues):
    """One picker, one order. If these ever disagree, the dropdown shows one
    league while answers come from another."""
    from advisor.cli import _pick_league

    offered = client.get("/leagues").json()["leagues"]
    assert _pick_league(None)[0] == offered[0]["league_id"]


def test_an_unknown_league_is_refused_before_the_stream_opens(client, two_leagues):
    """A bogus id used to pass the picker and raise inside the generator, so the
    stream died after a 200 — which a browser renders as an answer that stops
    mid-sentence."""
    response = client.post("/chat", json={"message": "hi", "league_id": "nope"})
    assert response.status_code == 404
    assert "not linked" in response.json()["detail"]


def test_a_thread_keeps_its_league_when_the_default_moves(
    client, two_leagues, dead_model
):
    """The drift bug. conversations.league_id was written and never read back,
    so turn two re-ran the picker — and the picker's answer moves."""
    from advisor.warehouse import conversations

    conversation_id = conversations.create("B", 1)

    # Flip the default to A by giving it an intent, which outranks everything.
    two_leagues(
        "INSERT INTO team_intent (league_id, roster_id, intent, updated_at) "
        "VALUES ('A', 1, 'contend', now())"
    )
    from advisor.cli import _pick_league

    assert _pick_league(None)[0] == "A", "the default really did move"

    response = client.post(
        "/chat", json={"message": "hi", "conversation_id": conversation_id}
    )
    start = next(data for event, data in _frames(response) if event == "start")
    assert start["league_id"] == "B", "the thread must stay in the league it began in"


def test_switching_leagues_mid_thread_is_refused(client, two_leagues):
    """Answering about A under a UI showing B is worse than saying no."""
    from advisor.warehouse import conversations

    conversation_id = conversations.create("A", 1)
    response = client.post(
        "/chat",
        json={"message": "hi", "conversation_id": conversation_id, "league_id": "B"},
    )
    assert response.status_code == 409
    assert "start a new one" in response.json()["detail"]


def test_the_first_event_names_the_league_even_when_the_model_is_down(
    client, two_leagues, dead_model
):
    """A dead Ollama should still say which league you asked about, and hand
    back a conversation_id to retry into rather than orphaning the turn."""
    frames = _frames(client.post("/chat", json={"message": "hi"}))

    assert frames[0][0] == "start"
    assert frames[0][1]["league_id"] == "A"
    assert frames[0][1]["conversation_id"]
    assert frames[1][0] == "error"
    assert "ollama serve" in frames[1][1]["message"]


def test_the_page_offers_a_league_switcher(client):
    page = client.get("/").text
    assert 'id="league"' in page
    assert "/leagues" in page
